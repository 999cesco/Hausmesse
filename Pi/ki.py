"""
KI-Kern des Smarthome-Assistenten.
Wird vom Web-Chat (app.py) und spaeter vom Sprachassistenten genutzt.

Aufgabenteilung:
  - Die KI versteht den Satz und entscheidet, WAS passieren soll.
  - Dieser Code prueft jeden Befehl, fuehrt Szenen vollstaendig aus und
    erkennt offene Fenster/Tore selbst, damit nichts vergessen wird.
"""

import json
import re
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
        {"device": "heating", "action": "set", "value": 18},
        {"device": "fan", "action": "auto"},
    ],
    "guten_morgen": [
        {"device": "light", "room": "schlafzimmer", "action": "set", "value": 60},
        {"device": "light", "room": "kueche", "action": "on"},
        {"device": "heating", "action": "set", "value": 21},
    ],
    "alles_aus": [
        {"device": "light", "room": "alle", "action": "off"},
        {"device": "heating", "action": "off"},
        {"device": "cooling", "action": "off"},
        {"device": "fan", "action": "off"},
    ],
}

# Was das Haus bei einer Szene ansagt (vom Code, damit es immer stimmt)
SCENE_TEXT = {
    "gute_nacht": "Ich habe alle Lichter ausgeschaltet, das Schlafzimmer auf 20 Prozent gedimmt "
                  "und die Heizung auf 18 Grad gestellt.",
    "guten_morgen": "Ich habe Licht im Schlafzimmer und in der Küche gemacht und heize auf 21 Grad.",
    "alles_aus": "Ich habe alle Lichter, die Heizung, die Klimaanlage und den Lüfter ausgeschaltet.",
}

SYSTEM_PROMPT = """Du bist "Haus", der freundliche Sprachassistent eines Smarthome-Modellhauses.
Du verstehst normale Sätze, denkst mit und handelst selbstständig.

DAS HAUS
Räume mit dimmbarem Licht (0-100 %): schlafzimmer, arbeitszimmer, wohnzimmer, kueche, garage
Heizung (heating): HEIZT nur. on / off / set mit Zieltemperatur 10-30 °C.
Klimaanlage (cooling): KÜHLT nur. on / off / set mit Zieltemperatur 16-30 °C.
  Beide regeln selbst, bis die Zieltemperatur erreicht ist. Es läuft immer nur eins:
  Schaltest du die Heizung ein, geht die Klimaanlage aus und umgekehrt.
Lüfter: on / off / auto (läuft automatisch, wenn die Klimaanlage kühlt)
Garagentor: open / close
Fenster: kannst du NICHT steuern, nur daran erinnern.

BEFEHLE (Feld "befehle")
{"device":"light","room":"<raum oder alle>","action":"on"|"off"|"set","value":<0-100 bei set>}
{"device":"heating","action":"on"|"off"|"set","value":<zieltemperatur, bei set>}
{"device":"cooling","action":"on"|"off"|"set","value":<zieltemperatur, bei set>}
{"device":"fan","action":"on"|"off"|"auto"}
{"device":"garage","action":"open"|"close"}

SZENEN (Feld "szene") - nutze sie, wenn sie passen, statt alles einzeln aufzuzählen:
gute_nacht   = alle Lichter aus, Schlafzimmer auf 20 % gedimmt, Heizung auf 18 °C
guten_morgen = Schlafzimmer 60 %, Küche hell, Heizung auf 21 °C
alles_aus    = alle Lichter, Heizung, Klimaanlage und Lüfter aus
Sonst "szene": "".

ART DER ANFRAGE (Feld "art") - entscheide das ZUERST:
befehl      = der Nutzer will etwas Bestimmtes schalten ("Licht in der Küche an", "Garage öffnen",
              "Heizung auf 22 Grad"). "antwort" bleibt leer.
statusfrage = der Nutzer fragt nach einem Zustand ("Wie warm ist es?", "Ist die Garage zu?",
              "Muss ich gießen?", "Ist das nicht zu wenig?"). Nenne NUR genau das, wonach
              gefragt wurde. Bei einer Statusfrage schaltest du NIE etwas.
situation   = der Nutzer beschreibt eine Lage oder ein Vorhaben ("Mir ist kalt", "Ich gehe ins Bett",
              "Ich gehe aus dem Haus"). NUR hier darfst du mitdenken und passende HINWEISE nennen
              (offene Fenster, offenes Garagentor).
wissen      = allgemeine Frage ohne Bezug zum Haus ("Wie hoch ist der Mount Everest?").
sonstiges   = alles andere (Begrüßung, Danke, Unverständliches).
gemischt    = der Satz hat MEHRERE Teile, z. B. etwas schalten UND etwas fragen
              ("Mach das Licht an und sag mir, wie warm es ist"). Erledige dann ALLE Teile:
              Befehle in "befehle", und in "antwort" NUR die übrigen Teile (Werte, Wissen).

WAS DU SCHALTEST, SAGT DAS SYSTEM SELBST AN.
Schreib in "antwort" NIE, was du einschaltest, ausschaltest oder einstellst
("Ich schalte das Licht ein", "Ich heize auf 22 Grad" usw. weglassen). In "antwort" stehen nur
Werte, Hinweise, Wissen und Rückfragen. Bei "befehl" bleibt "antwort" deshalb leer: "".

REGELN
- Der Text kommt aus einer Spracherkennung und kann falsch erkannte Wörter enthalten
  (z. B. "Kratz" statt "Grad", "Küchen Licht" statt "Küchenlicht"). Rate, was gemeint war.
- Antworte auf Deutsch, natürlich und KURZ (1-2 Sätze pro Teil). Die Antwort wird vorgelesen.
- Lies NIEMALS den Zustand des Hauses vor, wenn nicht danach gefragt wurde.
  Erwähne nie andere Räume oder Geräte als die, um die es geht.
- WICHTIG: Was du schalten willst, MUSS als Befehl in "befehle" (oder als "szene") stehen.
  Nur das wird ausgeführt. Behaupte nie, etwas getan zu haben, ohne den passenden Befehl.
- Es gibt EINE Heizung und EINE Klimaanlage für das ganze Haus, nicht pro Raum.
  Die Heizung heizt, die Klimaanlage kühlt. Die Klimaanlage heizt NIE.
  Die Garage hat nur Licht und das Tor.
- "Garage öffnen/aufmachen" = Garagentor open. "Garage zu" = Garagentor close.
- Denk mit: "Mir ist kalt" heißt Heizung an, "Mir ist warm" heißt Klimaanlage an.
  Prüfe dafür die aktuelle Temperatur.
- HINWEISE (offenes Fenster, offenes Garagentor) erwähnst du NUR bei art "situation"
  und nur, wenn sie dazu passen (Kälte, Schlafen, Verlassen des Hauses).
  Biete bei einem offenen Garagentor an, es zu schließen, schließe es aber nicht ungefragt.
- Stelle KEINE Fragen, außer du bietest eine konkrete Aktion an ("Soll ich das Garagentor
  schließen?"). Frage nie "Wie geht es dir?" oder "Kann ich noch etwas tun?".
  Setze "rueckfrage" nur dann auf true, sonst immer false.
- Wenn du eine Frage gestellt hast und der Nutzer mit "ja" antwortet, führe das Angebotene aus.
  Bei "nein" bestätige kurz und tu nichts.
- Hinter manchen Werten steht in Klammern eine BEWERTUNG des Hauses, z. B.
  "Bodenfeuchte: 12 % (sehr trocken: gießen nötig)". Verlass dich auf diese Bewertung
  und rechne nicht selbst. Wenig Bodenfeuchte heißt TROCKEN, also gießen.
- Bei Bedingungen ("wenn es über 23 Grad hat ...", "wenn ja, dann ...") prüfe die Bedingung
  anhand der Werte und Bewertungen. Ist sie erfüllt, schick den Befehl. Ist sie NICHT erfüllt,
  schick KEINEN Befehl und sag kurz, warum. Sag immer, was du festgestellt hast.
- Rückfragen des Nutzers ("Ist das nicht zu wenig?", "Und jetzt?") beziehen sich auf das,
  worüber ihr gerade gesprochen habt, nicht auf irgendeinen anderen Wert im Haus.
- Allgemeine Fragen (Wissen, Erklärungen) beantwortest du kurz und normal, ohne Befehle.
  Wenn du dir bei Fakten unsicher bist, sag das ehrlich. Erfinde nichts.
- Erfinde keine Messwerte. Steht ein Wert als "kein Messwert", sag das.
- Ergibt der Satz keinen Sinn (z. B. nur ein einzelnes Wort), frag kurz nach, was gemeint ist.

BEISPIELE
Zustand: 18 °C, Heizung aus, Klimaanlage aus. Hinweise: Fenster im Wohnzimmer ist offen.
Nutzer: Mir ist kalt
{"art":"situation","antwort":"Es hat gerade 18 Grad. Übrigens ist das Fenster im Wohnzimmer noch offen, schließ es lieber, sonst geht die Wärme raus.","szene":"","befehle":[{"device":"heating","action":"set","value":22}],"rueckfrage":false}

Zustand: Lichter an. Hinweise: Fenster im Wohnzimmer ist offen. Garagentor ist offen.
Nutzer: Ich gehe jetzt schlafen
{"art":"situation","antwort":"Gute Nacht! Kurze Erinnerung: Das Fenster im Wohnzimmer ist noch offen, und das Garagentor ist auch offen. Soll ich das Garagentor schließen?","szene":"gute_nacht","befehle":[],"rueckfrage":true}

Nutzer: Ja bitte
{"art":"befehl","antwort":"","szene":"","befehle":[{"device":"garage","action":"close"}],"rueckfrage":false}

Nutzer: Mach im Arbeitszimmer Licht, aber nicht so hell
{"art":"befehl","antwort":"","szene":"","befehle":[{"device":"light","room":"arbeitszimmer","action":"set","value":40}],"rueckfrage":false}

Nutzer: Garage öffnen
{"art":"befehl","antwort":"","szene":"","befehle":[{"device":"garage","action":"open"}],"rueckfrage":false}

Zustand: 23 °C, Luftfeuchtigkeit 50 %, alle Lichter an, Heizung und Klimaanlage aus, Fenster im Schlafzimmer offen.
Nutzer: Mach das Licht in der Küche aus
{"art":"befehl","antwort":"","szene":"","befehle":[{"device":"light","room":"kueche","action":"off"}],"rueckfrage":false}

Zustand: 23 °C, Luftfeuchtigkeit 50 %, Lichter an, Heizung und Klimaanlage aus, Fenster im Schlafzimmer offen.
Nutzer: Wie warm ist es?
{"art":"statusfrage","antwort":"Im Haus hat es gerade 23 Grad.","szene":"","befehle":[],"rueckfrage":false}

Zustand: 21 °C, Luftfeuchtigkeit 45 %, Lichter aus.
Nutzer: Mach alle Lichter an und sag mir, wie viel Kratz es hat, und erzähl was über Beethoven
{"art":"gemischt","antwort":"Im Haus hat es 21 Grad. Beethoven war ein deutscher Komponist aus Bonn, der viele seiner berühmtesten Werke schrieb, als er schon fast taub war.","szene":"","befehle":[{"device":"light","room":"alle","action":"on"}],"rueckfrage":false}

Zustand: 27 °C, Heizung aus, Klimaanlage aus.
Nutzer: Mir ist zu warm
{"art":"situation","antwort":"Es hat gerade 27 Grad.","szene":"","befehle":[{"device":"cooling","action":"set","value":23}],"rueckfrage":false}

Zustand: Bodenfeuchte 8 % (sehr trocken: gießen nötig), Licht Arbeitszimmer aus.
Nutzer: Muss ich meine Pflanze gießen? Wenn ja, mach Licht im Arbeitszimmer an
{"art":"gemischt","antwort":"Ja, die Erde ist mit 8 Prozent sehr trocken, du solltest gießen.","szene":"","befehle":[{"device":"light","room":"arbeitszimmer","action":"on"}],"rueckfrage":false}

Zustand: Bodenfeuchte 70 % (gut feucht: nicht gießen), Licht Arbeitszimmer aus.
Nutzer: Muss ich meine Pflanze gießen? Wenn ja, mach Licht im Arbeitszimmer an
{"art":"statusfrage","antwort":"Nein, die Erde ist mit 70 Prozent noch gut feucht. Deshalb lasse ich das Licht aus.","szene":"","befehle":[],"rueckfrage":false}

Nutzer: Ist das nicht zu wenig?
{"art":"statusfrage","antwort":"Nein, 70 Prozent Bodenfeuchte ist für die Pflanze genau richtig.","szene":"","befehle":[],"rueckfrage":false}

Nutzer: Wie hoch ist der Mount Everest?
{"art":"wissen","antwort":"Der Mount Everest ist etwa 8849 Meter hoch und damit der höchste Berg der Erde.","szene":"","befehle":[],"rueckfrage":false}
"""

# JSON-Schema: zwingt das Modell in genau dieses Antwortformat
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "art": {"type": "string", "enum": ["befehl", "statusfrage", "situation", "wissen",
                                           "gemischt", "sonstiges"]},
        "antwort": {"type": "string"},
        "szene": {"type": "string", "enum": [""] + list(SCENES.keys())},
        "befehle": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "enum": ["light", "heating", "cooling", "fan", "garage"]},
                    "room": {"type": "string", "enum": LIGHT_ROOMS + ["alle"]},
                    "action": {"type": "string", "enum": ["on", "off", "set", "auto", "open", "close"]},
                    "value": {"type": "number"},
                },
                "required": ["device", "action"],
            },
        },
        "rueckfrage": {"type": "boolean"},
    },
    "required": ["art", "antwort", "szene", "befehle", "rueckfrage"],
}

_history = deque(maxlen=config.HISTORY_MESSAGES)
_lock = threading.Lock()

# Wird aufgerufen, wenn die KI einen Satz mit genau EINEM einfachen Befehl geloest hat
# (die Schnellerkennung sammelt daraus Vorschlaege). Funktionen: f(user_text, befehl)
SIMPLE_COMMAND_HOOKS = []


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
    """Gibt einen bereinigten Befehl zurueck oder None, wenn er ungueltig ist.
    Kleine Formfehler der KI werden dabei repariert statt verworfen."""
    if not isinstance(cmd, dict):
        return None
    device = str(cmd.get("device", "")).lower()
    action = str(cmd.get("action", "")).lower()
    room, value = cmd.get("room"), cmd.get("value")
    has_value = isinstance(value, (int, float)) and not isinstance(value, bool)

    if device == "light":
        if room in (None, "", "all", "haus"):      # "alle Lichter an" ohne Raum
            room = "alle"
        if room not in LIGHT_ROOMS + ["alle"]:
            return None
        if action == "set" and not has_value:      # "set" ohne Wert -> einfach an
            action = "on"
        if action == "on" and has_value and 0 <= value < 100:
            action = "set"                         # "on" mit Helligkeit -> dimmen
        if action == "set":
            return {"device": "light", "room": room, "action": "set", "value": max(0, min(100, int(value)))}
        if action in ("on", "off"):
            return {"device": "light", "room": room, "action": action}
        return None

    if device in ("heating", "cooling"):
        lo = 10 if device == "heating" else 16
        if action in ("heat", "cool", "auto"):         # alte Schreibweisen -> einschalten
            action = "on"
        if action == "set" and not has_value:          # "set" ohne Wert -> einfach an
            action = "on"
        if action in ("on", "off", "set"):
            out = {"device": device, "action": action}
            if has_value:
                out["value"] = max(lo, min(30, round(float(value), 1)))
                if action == "on":
                    out["action"] = "set"
            return out
        return None

    # Alte Form "climate" (falls die KI sie doch benutzt): heizen -> Heizung, kuehlen -> Klimaanlage
    if device == "climate":
        if action in ("heat", "set", "on", "auto"):
            return validate({**cmd, "device": "heating", "action": "set" if has_value else "on"})
        if action == "cool":
            return validate({**cmd, "device": "cooling", "action": "set" if has_value else "on"})
        if action == "off":
            return {"device": "heating", "action": "off"}
        return None

    if device == "fan":
        if action == "set":
            action = "on"
        if action in ("on", "off", "auto"):
            return {"device": "fan", "action": action}
        return None

    if device == "garage":
        action = {"on": "open", "off": "close"}.get(action, action)
        if action in ("open", "close"):
            return {"device": "garage", "action": action}
        return None

    if device == "buzzer" and action in ("on", "off") and allow_window:  # nur manuell
        return {"device": "buzzer", "action": action}

    if device == "window" and allow_window and action in ("open", "close"):
        return {"device": "window", "room": room, "action": action}

    return None


# -------------------- Kurze Bestaetigungen (vom Code, nicht von der KI) --------------------
ROOM_IN = {"schlafzimmer": "im Schlafzimmer", "arbeitszimmer": "im Arbeitszimmer",
           "wohnzimmer": "im Wohnzimmer", "kueche": "in der Küche", "garage": "in der Garage"}


def _num(v):
    return f"{v:g}".replace(".", ",")


def confirm_text(cmd):
    d, a, v = cmd["device"], cmd["action"], cmd.get("value")
    if d == "light":
        wo = "Alle Lichter sind" if cmd["room"] == "alle" else f"Das Licht {ROOM_IN[cmd['room']]} ist"
        if a == "set":
            return f"{wo} auf {v} Prozent." if v > 0 else f"{wo} aus."
        return f"{wo} {'an' if a == 'on' else 'aus'}."
    if d == "heating":
        if a == "off":
            return "Die Heizung ist aus."
        return f"Die Heizung heizt auf {_num(v)} Grad." if v is not None else "Die Heizung ist an."
    if d == "cooling":
        if a == "off":
            return "Die Klimaanlage ist aus."
        return f"Die Klimaanlage kühlt auf {_num(v)} Grad." if v is not None else "Die Klimaanlage ist an."
    if d == "fan":
        return {"on": "Der Lüfter ist an.", "off": "Der Lüfter ist aus.",
                "auto": "Der Lüfter läuft jetzt automatisch."}[a]
    if d == "garage":
        return "Das Garagentor öffnet sich." if a == "open" else "Das Garagentor schließt sich."
    return ""


def confirm(commands):
    """Feste, kurze Bestaetigung fuer einfache Schaltbefehle - die KI kann hier nicht abschweifen."""
    if len(commands) > 3:
        return "Erledigt."
    return " ".join(confirm_text(c) for c in commands)


# -------------------- Zustand fuer die KI aufbereiten --------------------
def rate_temperature(t):
    if t < 18:
        return "kalt"
    if t < 20:
        return "kühl"
    if t <= 24:
        return "angenehm"
    if t <= 26:
        return "warm"
    return "heiß"


def rate_soil(p):
    """Bewertung der Bodenfeuchte. Kleine Modelle verwechseln sonst, ob wenig Prozent gut oder schlecht ist."""
    if p <= 0:
        return "sehr trocken: gießen nötig - oder der Sensor ist nicht angeschlossen"
    if p < 30:
        return "sehr trocken: gießen nötig"
    if p < 50:
        return "etwas trocken: bald gießen"
    if p <= 80:
        return "gut feucht: nicht gießen"
    return "sehr nass: auf keinen Fall gießen"


# Saetze, in denen das Modell behauptet, selbst etwas zu schalten. Die Ansage macht der Code -
# solche Saetze stimmen oft nicht mit dem ueberein, was wirklich passiert, und werden entfernt.
_ACTION_CLAIM = re.compile(
    r"\bich (schalte|mache|stelle|dimme|heize|kühle|öffne|schließe|drehe)\b"
    r"|\bhabe\b.*\b(eingeschaltet|ausgeschaltet|angeschaltet|angemacht|ausgemacht|gestellt|"
    r"gedimmt|geöffnet|geschlossen|aufgedreht|eingestellt)\b", re.IGNORECASE)


def strip_action_claims(text):
    """Entfernt Behauptungen wie 'Ich schalte das Licht ein.' - Fragen ('Soll ich ...?') bleiben."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [x for x in sentences if x.endswith("?") or not _ACTION_CLAIM.search(x)]
    return " ".join(kept).strip()


def describe_state(s):
    """Macht aus dem ESP-JSON einen gut lesbaren Text mit Bewertungen.
    Die Bewertungen rechnet der Code aus - so muss das Modell keine Zahlen deuten."""
    sens = s.get("sensors", {})
    t, h = sens.get("temperature"), sens.get("humidity")
    soil = sens.get("soilPercent")
    lines = [
        f"Temperatur: {t:.1f} °C ({rate_temperature(t)})" if t is not None else "Temperatur: kein Messwert",
        f"Luftfeuchtigkeit: {h:.0f} %" if h is not None else "Luftfeuchtigkeit: kein Messwert",
        (f"Bodenfeuchte der Pflanze: {soil} % ({rate_soil(soil)})" if soil is not None
         else "Bodenfeuchte der Pflanze: kein Messwert"),
    ]
    # "aus" statt "0 %", damit 0-Werte nicht mit anderen Prozentangaben verwechselt werden
    lights = ", ".join(f"{ROOM_NAMES[r]} {'aus' if v == 0 else f'{v} %'}"
                       for r, v in s.get("lights", {}).items())
    lines.append(f"Licht: {lights}")

    for key, name, verb in (("heating", "Heizung", "heizt gerade"), ("cooling", "Klimaanlage", "kühlt gerade")):
        t = s.get(key, {})
        if t.get("on"):
            lines.append(f"{name}: an, Ziel {t.get('target', 0):.1f} °C, "
                         f"{verb if t.get('active') else 'Ziel erreicht'}")
        else:
            lines.append(f"{name}: aus")

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
        "options": {"temperature": 0.2, "num_ctx": 8192},  # genug Kontext fuer Prompt + Verlauf
    }, timeout=120, **config.server_args())
    r.raise_for_status()
    raw = r.json()["message"]["content"]
    return raw, json.loads(raw)


def handle(user_text, pruefen=None):
    """Hauptfunktion: Text rein -> Antwort + ausgefuehrte Befehle raus.
    pruefen: optionale Rechte-Pruefung fuer den angemeldeten Nutzer (siehe rechte.filter_for)."""
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

        # Fragen schalten nie etwas - auch wenn das Modell trotzdem Befehle mitschickt
        if result.get("art") in ("statusfrage", "wissen") and (commands or result.get("szene")):
            ignored += commands
            commands = []
            result["szene"] = ""
            print("[KI] Frage erkannt -> Befehle verworfen")

        # Rechte des Nutzers: Verbotenes wird nicht ausgefuehrt
        denied, deny_msg = [], ""
        if pruefen and commands:
            commands, denied, deny_msg = pruefen(commands)

        esp_result = None
        if commands:
            try:
                esp_result = send_commands(commands)
            except (requests.RequestException, ValueError):
                esp_result = {"status": "error", "error": "ESP nicht erreichbar"}

        art = result.get("art", "")
        szene = result.get("szene", "")
        antwort = strip_action_claims(result.get("antwort", ""))
        rueckfrage = bool(result.get("rueckfrage", False))

        # Ansage, was geschaltet wurde - kommt IMMER vom Code, damit sie zur Wirklichkeit passt
        if commands:
            extra = commands[len(SCENES[szene]):] if szene in SCENE_TEXT and not denied else commands
            if szene in SCENE_TEXT and not denied:
                action_txt = (SCENE_TEXT[szene] + " " + confirm(extra)).strip() if extra else SCENE_TEXT[szene]
            else:
                action_txt = confirm(commands)
            text = "" if art == "befehl" else antwort
            antwort = f"{action_txt} {text}".strip()
            if art == "befehl":
                rueckfrage = False
        # Nichts davon durfte der Nutzer -> ehrlich sagen statt die KI-Antwort vorzulesen
        elif denied:
            rest = antwort if art == "gemischt" else ""
            antwort = f"{deny_msg} {rest}".strip()
            rueckfrage = False
        # Die KI wollte etwas schalten, aber nichts davon war gueltig -> nicht luegen
        elif ignored and art in ("befehl", "gemischt", "situation"):
            hinweis = "Das Schalten konnte ich leider nicht ausführen."
            antwort = hinweis if art == "befehl" or not antwort else f"{hinweis} {antwort}"
            rueckfrage = False

        if denied and commands and deny_msg:
            antwort = f"{antwort} {deny_msg}".strip()
        if esp_result and esp_result.get("status") == "error":
            antwort += " Allerdings hat das Haus den Befehl nicht angenommen."

        # Alles im Terminal ausgeben -> Fehlersuche wird einfach
        print(f"[KI] Nutzer:   {user_text}")
        print(f"[KI] Modell:   {raw}")
        print(f"[KI] Befehle:  {commands}" + (f"  IGNORIERT: {ignored}" if ignored else "")
              + (f"  NICHT ERLAUBT: {denied}" if denied else ""))
        print(f"[KI] ESP:      {esp_result}")

        # Verlauf merken (ohne den langen Zustand), damit Rueckfragen wie "ja" funktionieren
        _history.append({"role": "user", "content": f"Nutzer: {user_text}"})
        # Im Verlauf steht, was wirklich gesagt und getan wurde - nicht der Entwurf des Modells
        _history.append({"role": "assistant", "content": json.dumps(
            {**result, "antwort": antwort, "befehle": commands}, ensure_ascii=False)})

        # Einfacher Befehl, sauber ausgefuehrt -> Kandidat fuer die Schnellerkennung
        if (result.get("art") == "befehl" and len(commands) == 1 and not denied and not ignored
                and not result.get("szene")):
            for hook in SIMPLE_COMMAND_HOOKS:
                try:
                    hook(user_text, commands[0])
                except Exception as e:
                    print(f"[KI] Vorschlag konnte nicht gespeichert werden: {e}")

        return {
            "antwort": antwort,
            "art": result.get("art", ""),
            "rueckfrage": rueckfrage,
            "szene": result.get("szene", ""),
            "befehle": commands,
            "ignoriert": ignored,
            "esp": esp_result,
        }


def remember(user_text, antwort, commands):
    """Traegt einen Befehl, den die Schnellerkennung erledigt hat, in den Gespraechsverlauf ein,
    damit die KI bei Rueckfragen danach weiss, was passiert ist."""
    with _lock:
        _history.append({"role": "user", "content": f"Nutzer: {user_text}"})
        _history.append({"role": "assistant", "content": json.dumps(
            {"art": "befehl", "antwort": antwort, "szene": "", "befehle": commands, "rueckfrage": False},
            ensure_ascii=False)})


def reset_history():
    with _lock:
        _history.clear()
