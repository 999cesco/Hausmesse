"""
KI-Kern des Smarthome-Assistenten.
Wird vom Web-Chat (app.py) und spaeter vom Sprachassistenten genutzt.

Aufgabenteilung:
  - Die KI versteht den Satz und entscheidet, WAS passieren soll.
  - Dieser Code prueft jeden Befehl, fuehrt Szenen vollstaendig aus und
    erkennt offene Fenster/Tore selbst, damit nichts vergessen wird.
"""

import json
import threading
from collections import deque
from datetime import datetime

import requests

import config

# -------------------- Haus-Definition (muss zum ESP-Code passen) --------------------
LIGHT_ROOMS = ["schlafzimmer", "arbeitszimmer", "wohnzimmer", "kueche", "garage"]
ROOM_NAMES = {
    "schlafzimmer": "Schlafzimmer", "arbeitszimmer": "Arbeitszimmer",
    "wohnzimmer": "Wohnzimmer", "kueche": "Küche", "garage": "Garage", "alle": "alle Räume",
}

# Szenen: feste Abläufe, die immer vollständig ausgeführt werden
SCENES = {
    "gute_nacht": [
        {"device": "light", "room": "alle", "action": "off"},
        {"device": "light", "room": "schlafzimmer", "action": "set", "value": 20},
        {"device": "climate", "action": "heat", "value": 18},
        {"device": "fan", "action": "auto"},
    ],
    "guten_morgen": [
        {"device": "light", "room": "schlafzimmer", "action": "set", "value": 60},
        {"device": "light", "room": "kueche", "action": "on"},
        {"device": "climate", "action": "auto", "value": 21},
    ],
    "alles_aus": [
        {"device": "light", "room": "alle", "action": "off"},
        {"device": "climate", "action": "off"},
        {"device": "fan", "action": "off"},
    ],
}

SYSTEM_PROMPT = """Du bist "Haus", der freundliche Sprachassistent eines Smarthome-Modellhauses.
Du verstehst normale Sätze, denkst mit und handelst selbstständig.

DAS HAUS
Räume mit dimmbarem Licht (0-100 %): schlafzimmer, arbeitszimmer, wohnzimmer, kueche, garage
Klima: Modus off / heat (heizen) / cool (kühlen) / auto, mit Zieltemperatur 10-30 °C.
  Das Haus regelt selbst: es heizt bzw. kühlt, bis die Zieltemperatur erreicht ist.
Lüfter: on / off / auto (läuft automatisch beim Kühlen)
Garagentor: open / close
Fenster: kannst du NICHT steuern, nur daran erinnern.

BEFEHLE (Feld "befehle")
{"device":"light","room":"<raum oder alle>","action":"on"|"off"|"set","value":<0-100 bei set>}
{"device":"climate","action":"off"|"heat"|"cool"|"auto","value":<zieltemperatur, optional>}
{"device":"fan","action":"on"|"off"|"auto"}
{"device":"garage","action":"open"|"close"}

SZENEN (Feld "szene") - nutze sie, wenn sie passen, statt alles einzeln aufzuzählen:
gute_nacht   = alle Lichter aus, Schlafzimmer auf 20 % gedimmt, Heizung auf 18 °C
guten_morgen = Schlafzimmer 60 %, Küche hell, Klima auto auf 21 °C
alles_aus    = alle Lichter, Klima und Lüfter aus
Sonst "szene": "".

REGELN
- Antworte auf Deutsch, natürlich und kurz (1-3 Sätze), die Antwort wird vorgelesen.
- Denk mit: "Mir ist kalt" heißt heizen. Prüfe dafür die aktuelle Temperatur.
- Sag in der Antwort, was du getan hast, mit konkreten Werten.
- Wenn unter HINWEISE etwas steht (offenes Fenster, offenes Garagentor), erwähne es,
  wenn es zur Situation passt (Kälte, Schlafen, Verlassen des Hauses).
  Biete bei einem offenen Garagentor an, es zu schließen, schließe es aber nicht ungefragt.
- Wenn du eine Frage gestellt hast und der Nutzer mit "ja" antwortet, führe das Angebotene aus.
- Bei Bedingungen ("wenn es über 23 Grad hat ...") prüfe die aktuellen Werte und
  handle nur, wenn die Bedingung jetzt erfüllt ist. Sag, was du festgestellt hast.
- Allgemeine Fragen (Wissen, Erklärungen) beantwortest du ganz normal, ohne Befehle.
  Wenn du dir bei Fakten unsicher bist, sag das ehrlich. Erfinde nichts.
- Erfinde keine Messwerte. Steht ein Wert als "kein Messwert", sag das.

BEISPIELE
Zustand: 18 °C, Klima aus. Hinweise: Fenster im Wohnzimmer ist offen.
Nutzer: Mir ist kalt
{"antwort":"Es hat gerade 18 Grad, ich heize auf 22 Grad. Übrigens ist das Fenster im Wohnzimmer noch offen, schließ es lieber, sonst geht die Wärme raus.","szene":"","befehle":[{"device":"climate","action":"heat","value":22}]}

Zustand: Lichter an. Hinweise: Fenster im Wohnzimmer ist offen. Garagentor ist offen.
Nutzer: Ich gehe jetzt schlafen
{"antwort":"Gute Nacht! Ich habe alle Lichter ausgeschaltet, das Schlafzimmer auf 20 Prozent gedimmt und die Heizung auf 18 Grad gestellt. Kurze Erinnerung: Das Fenster im Wohnzimmer ist noch offen, und das Garagentor ist auch offen. Soll ich das Garagentor schließen?","szene":"gute_nacht","befehle":[]}

Nutzer: Ja bitte
{"antwort":"Alles klar, ich schließe das Garagentor.","szene":"","befehle":[{"device":"garage","action":"close"}]}

Nutzer: Mach im Arbeitszimmer Licht, aber nicht so hell
{"antwort":"Ich habe das Licht im Arbeitszimmer auf 40 Prozent gestellt.","szene":"","befehle":[{"device":"light","room":"arbeitszimmer","action":"set","value":40}]}

Nutzer: Wie hoch ist der Mount Everest?
{"antwort":"Der Mount Everest ist etwa 8849 Meter hoch und damit der höchste Berg der Erde.","szene":"","befehle":[]}
"""

# JSON-Schema: zwingt das Modell in genau dieses Antwortformat
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "antwort": {"type": "string"},
        "szene": {"type": "string", "enum": [""] + list(SCENES.keys())},
        "befehle": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "enum": ["light", "climate", "fan", "garage"]},
                    "room": {"type": "string", "enum": LIGHT_ROOMS + ["alle"]},
                    "action": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["device", "action"],
            },
        },
    },
    "required": ["antwort", "szene", "befehle"],
}

_history = deque(maxlen=config.HISTORY_MESSAGES)
_lock = threading.Lock()


# -------------------- ESP --------------------
def get_state():
    r = requests.get(f"http://{config.ESP_IP}/api/state", timeout=4)
    r.raise_for_status()
    return r.json()


def send_commands(commands):
    if not commands:
        return {"status": "ok", "results": []}
    r = requests.post(f"http://{config.ESP_IP}/api/command",
                      json={"commands": commands}, timeout=5)
    return r.json()


# -------------------- Befehle pruefen --------------------
def validate(cmd, allow_window=False):
    """Gibt einen bereinigten Befehl zurueck oder None, wenn er ungueltig ist."""
    if not isinstance(cmd, dict):
        return None
    device, action = cmd.get("device"), cmd.get("action")
    room, value = cmd.get("room"), cmd.get("value")

    if device == "light":
        if room not in LIGHT_ROOMS + ["alle"]:
            return None
        if action == "set":
            if not isinstance(value, (int, float)):
                return None
            return {"device": "light", "room": room, "action": "set", "value": max(0, min(100, int(value)))}
        if action in ("on", "off"):
            return {"device": "light", "room": room, "action": action}
        return None

    if device == "climate" and action in ("off", "heat", "cool", "auto"):
        out = {"device": "climate", "action": action}
        if isinstance(value, (int, float)):
            out["value"] = max(10, min(30, round(float(value), 1)))
        return out

    if device == "fan" and action in ("on", "off", "auto"):
        return {"device": "fan", "action": action}

    if device == "garage" and action in ("open", "close"):
        return {"device": "garage", "action": action}

    if device == "buzzer" and action in ("on", "off") and allow_window:  # nur manuell
        return {"device": "buzzer", "action": action}

    if device == "window" and allow_window and action in ("open", "close"):
        return {"device": "window", "room": room, "action": action}

    return None


# -------------------- Zustand fuer die KI aufbereiten --------------------
def describe_state(s):
    """Macht aus dem ESP-JSON einen gut lesbaren Text (hilft kleinen Modellen)."""
    sens = s.get("sensors", {})
    t, h = sens.get("temperature"), sens.get("humidity")
    lines = [
        f"Temperatur: {t:.1f} °C" if t is not None else "Temperatur: kein Messwert",
        f"Luftfeuchtigkeit: {h:.0f} %" if h is not None else "Luftfeuchtigkeit: kein Messwert",
        f"Bodenfeuchte der Pflanze: {sens.get('soilPercent', 0)} %",
    ]
    lights = ", ".join(f"{ROOM_NAMES[r]} {v} %" for r, v in s.get("lights", {}).items())
    lines.append(f"Licht: {lights}")

    c = s.get("climate", {})
    mode = {"off": "aus", "heat": "heizen", "cool": "kühlen", "auto": "automatisch"}.get(c.get("mode"), "?")
    activ = "heizt gerade" if c.get("heating") else "kühlt gerade" if c.get("cooling") else "inaktiv"
    lines.append(f"Klima: {mode}, Ziel {c.get('target', 0):.1f} °C, {activ}")

    f = s.get("fan", {})
    lines.append(f"Lüfter: {'läuft' if f.get('on') else 'aus'} (Modus {f.get('mode')})")

    garage = {"open": "offen", "closed": "geschlossen", "opening": "öffnet gerade",
              "closing": "schließt gerade"}.get(s.get("garage"), "?")
    lines.append(f"Garagentor: {garage}")

    windows = ", ".join(f"{ROOM_NAMES.get(r, r)} {'offen' if v == 'open' else 'zu'}"
                        for r, v in s.get("windows", {}).items())
    lines.append(f"Fenster: {windows}")
    return "\n".join(lines)


def find_hints(s):
    """Dinge, an die erinnert werden sollte - vom Code erkannt, nicht von der KI."""
    hints = []
    for room, v in s.get("windows", {}).items():
        if v == "open":
            hints.append(f"Das Fenster im {ROOM_NAMES.get(room, room)} ist offen.")
    if s.get("garage") in ("open", "opening"):
        hints.append("Das Garagentor ist offen.")
    if s.get("sensors", {}).get("temperature") is None:
        hints.append("Der Temperatursensor liefert gerade keinen Wert.")
    return hints


# -------------------- Ollama --------------------
def _ask_model(user_text, state):
    hints = find_hints(state)
    d = datetime.now()
    weekday = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][d.weekday()]
    now = f"{weekday}, {d.strftime('%d.%m.%Y, %H:%M')} Uhr"
    content = (
        f"Datum/Uhrzeit: {now}\n\n"
        f"AKTUELLER ZUSTAND\n{describe_state(state)}\n\n"
        f"HINWEISE\n{chr(10).join(hints) if hints else 'keine'}\n\n"
        f"Nutzer: {user_text}"
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *list(_history),
                {"role": "user", "content": content}]

    r = requests.post(config.OLLAMA_URL, json={
        "model": config.MODEL,
        "messages": messages,
        "format": RESPONSE_SCHEMA,
        "stream": False,
        "keep_alive": "30m",                  # Modell im Speicher halten -> schnellere Antworten
        "options": {"temperature": 0.3, "num_ctx": 8192},  # genug Kontext fuer Prompt + Verlauf
    }, timeout=120)
    r.raise_for_status()
    raw = r.json()["message"]["content"]
    return raw, json.loads(raw)


def handle(user_text):
    """Hauptfunktion: Text rein -> Antwort + ausgefuehrte Befehle raus."""
    with _lock:
        try:
            state = get_state()
        except requests.RequestException:
            return {"antwort": "Ich erreiche das Haus gerade nicht. Ist der ESP eingeschaltet und im WLAN?",
                    "befehle": [], "fehler": "esp"}

        try:
            raw, result = _ask_model(user_text, state)
        except (requests.RequestException, ValueError, KeyError):
            return {"antwort": "Der KI-Server antwortet gerade nicht.", "befehle": [], "fehler": "ki"}

        # Szene zuerst, danach Einzelbefehle (die koennen die Szene ergaenzen)
        commands = list(SCENES.get(result.get("szene", ""), []))
        ignored = []
        for c in result.get("befehle", []):
            v = validate(c)
            if v:
                commands.append(v)
            else:
                ignored.append(c)

        esp_result = None
        if commands:
            try:
                esp_result = send_commands(commands)
            except requests.RequestException:
                esp_result = {"status": "error", "error": "ESP nicht erreichbar"}

        # Verlauf merken (ohne den langen Zustand), damit Rueckfragen wie "ja" funktionieren
        _history.append({"role": "user", "content": f"Nutzer: {user_text}"})
        _history.append({"role": "assistant", "content": raw})

        return {
            "antwort": result.get("antwort", ""),
            "szene": result.get("szene", ""),
            "befehle": commands,
            "ignoriert": ignored,
            "esp": esp_result,
        }


def reset_history():
    with _lock:
        _history.clear()
