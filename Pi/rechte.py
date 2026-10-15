"""
Rechte fuer normale Nutzer (Administratoren duerfen immer alles).

Pro Nutzer wird festgelegt:
  ziele       - welche Geraete er schalten darf (einzelne Lichter, Klima, Luefter, Garage, Fenster)
  chat        - ob er mit der KI schreiben darf
  pro_minute  - hoechstens so viele Aktionen pro Minute (0 = unbegrenzt)

Jeder Befehl wird auf dem Pi geprueft - egal ob er aus dem Dashboard oder ueber die KI kommt.
"""

import threading
import time
from collections import deque

LIGHT_ROOMS = ["schlafzimmer", "arbeitszimmer", "wohnzimmer", "kueche", "garage"]

TARGETS = [
    ("licht:schlafzimmer", "Licht Schlafzimmer"),
    ("licht:arbeitszimmer", "Licht Arbeitszimmer"),
    ("licht:wohnzimmer", "Licht Wohnzimmer"),
    ("licht:kueche", "Licht Küche"),
    ("licht:garage", "Licht Garage"),
    ("klima", "Heizung und Klima"),
    ("luefter", "Lüfter"),
    ("garage", "Garagentor"),
    ("fenster", "Fenster (simuliert)"),
]
TARGET_KEYS = [k for k, _ in TARGETS]
MAX_PER_MINUTE = 60

_lock = threading.Lock()
_actions = {}   # Benutzer -> Zeitpunkte seiner letzten Aktionen


def default():
    return {"ziele": list(TARGET_KEYS), "chat": True, "pro_minute": 0}


def normalize(r):
    """Prueft und bereinigt Rechte aus dem Browser. Wirft ValueError bei Unsinn."""
    if not isinstance(r, dict):
        raise ValueError("Ungültige Rechte")
    ziele = r.get("ziele", [])
    if not isinstance(ziele, list) or any(z not in TARGET_KEYS for z in ziele):
        raise ValueError("Unbekanntes Gerät in den Rechten")
    try:
        pro_minute = int(r.get("pro_minute", 0))
    except (TypeError, ValueError):
        raise ValueError("Aktionen pro Minute: bitte eine Zahl eingeben")
    if not 0 <= pro_minute <= MAX_PER_MINUTE:
        raise ValueError(f"Aktionen pro Minute: erlaubt sind 0 bis {MAX_PER_MINUTE}")
    return {"ziele": [k for k in TARGET_KEYS if k in ziele],    # feste Reihenfolge
            "chat": bool(r.get("chat", False)), "pro_minute": pro_minute}


def target_of(cmd):
    d = cmd.get("device")
    if d == "light":
        return "licht:" + str(cmd.get("room"))
    return {"climate": "klima", "fan": "luefter", "garage": "garage",
            "buzzer": "garage", "window": "fenster"}.get(d)


def split(commands, rechte):
    """Teilt Befehle in erlaubte und verbotene. "Alle Lichter" wird auf die erlaubten Raeume begrenzt."""
    allowed, denied = [], []
    ziele = set(rechte["ziele"])
    for c in commands:
        if c.get("device") == "light" and c.get("room") == "alle":
            rooms = [r for r in LIGHT_ROOMS if "licht:" + r in ziele]
            if len(rooms) == len(LIGHT_ROOMS):
                allowed.append(c)
            else:
                allowed += [{**c, "room": r} for r in rooms]
                if len(rooms) < len(LIGHT_ROOMS):
                    denied.append(c)
        elif target_of(c) in ziele:
            allowed.append(c)
        else:
            denied.append(c)
    return allowed, denied


def take_action(user, rechte):
    """Zaehlt eine Aktion. Gibt False zurueck, wenn das Limit pro Minute erreicht ist."""
    limit = rechte.get("pro_minute", 0)
    if not limit:
        return True
    now = time.time()
    with _lock:
        q = _actions.setdefault(user, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def filter_for(user, rechte):
    """Liefert eine Pruef-Funktion fuer ki.handle: (befehle) -> (erlaubt, verboten, meldung)."""
    def check(commands):
        allowed, denied = split(commands, rechte)
        if not allowed:
            return [], denied, "Dafür hast du keine Berechtigung."
        if not take_action(user, rechte):
            return [], commands, (f"Du hast dein Limit von {rechte['pro_minute']} Aktionen "
                                  "pro Minute erreicht. Versuch es gleich noch einmal.")
        msg = "Einen Teil davon darfst du nicht schalten." if denied else ""
        return allowed, denied, msg
    return check
