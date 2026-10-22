"""
Schnellerkennung: Einfache Befehle wie "Licht in der Kueche an" oder "Garage zu" erkennt der Pi
selbst und schaltet sofort - ohne Anfrage an die KI. Alles andere geht wie bisher an die KI.

- Muster mit Platzhaltern {raum} und {zahl} stehen in schnell.db (von Git ignoriert)
  und werden im Admin-Bereich gepflegt.
- Ein Muster greift nur, wenn es den GANZEN Satz abdeckt. "Licht an und wie warm ist es?"
  geht also an die KI.
- Fehlt eine Angabe ("Licht an" ohne Raum), fragt der Pi nach ("In welchem Raum?").
- Saetze, die an die KI gingen und nur einen einfachen Befehl ergaben, werden als Vorschlaege
  gesammelt, damit man sie spaeter mit einem Klick direkt erkennen lassen kann.
"""

import json
import os
import re
import sqlite3
import threading
import time

import config
import ki

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "schnell.db")
_lock = threading.Lock()

# -------------------- Woerter --------------------

ROOMS = {
    "schlafzimmer": ["schlafzimmer"],
    "arbeitszimmer": ["arbeitszimmer", "büro", "buero"],
    "wohnzimmer": ["wohnzimmer", "stube"],
    "kueche": ["küche", "kueche"],
    "garage": ["garage"],
    "alle": ["alle", "überall", "ueberall", "alles"],
}
ROOM_WORDS = {w: room for room, words in ROOMS.items() for w in words}

# Gleichbedeutende Woerter auf eine Form bringen
SYNONYMS = {
    "einschalten": "an", "anschalten": "an", "anmachen": "an", "ein": "an", "anknipsen": "an",
    "ausschalten": "aus", "ausmachen": "aus", "abschalten": "aus", "ausknipsen": "aus",
    "öffnen": "auf", "öffne": "auf", "oeffnen": "auf", "aufmachen": "auf", "hoch": "auf",
    "schließen": "zu", "schließe": "zu", "schliessen": "zu", "schliesse": "zu", "zumachen": "zu", "runter": "zu",
    "garagentor": "garage", "tor": "garage",
    "lichter": "licht", "lampe": "licht", "lampen": "licht", "beleuchtung": "licht",
    "klima": "klimaanlage", "ventilator": "lüfter", "luefter": "lüfter",
    "%": "prozent", "°": "grad", "°c": "grad",
}
# Woerter ohne Bedeutung fuer den Befehl
FILLERS = set("""bitte mal doch kannst könntest koenntest würdest wuerdest du ihr ich möchte moechte will gern
gerne jetzt sofort schnell eben kurz noch einmal der die das den dem des im in ins am vom von zum zur
mach mache machen schalt schalte schalten stell stelle stellen dreh drehe drehen setz setze setzen
dimm dimme dimmen hey hallo okay ok""".split())
CANCEL = {"nein", "abbrechen", "egal", "vergiss", "nichts", "stopp", "stop", "lass"}

_UNITS = {"null": 0, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5, "sechs": 6,
          "sieben": 7, "acht": 8, "neun": 9}
_TEENS = {"zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12, "dreizehn": 13, "vierzehn": 14,
          "fünfzehn": 15, "fuenfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19}
_TENS = {"zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50, "fuenfzig": 50,
         "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90, "hundert": 100, "einhundert": 100}


def _word_number(w):
    if w in _UNITS:
        return _UNITS[w]
    if w in _TEENS:
        return _TEENS[w]
    if w in _TENS:
        return _TENS[w]
    m = re.fullmatch(r"(ein|zwei|drei|vier|fünf|fuenf|sechs|sieben|acht|neun)und(\w+)", w)
    if m and m.group(2) in _TENS:
        unit = 1 if m.group(1) == "ein" else _UNITS[m.group(1)]
        return _TENS[m.group(2)] + unit
    return None


def normalize(text):
    """Macht aus jedem Satz eine einheitliche Kurzform: 'Mach bitte das Licht in der Küche an!' -> 'licht küche an'."""
    t = text.lower().replace("°c", " grad ").replace("°", " grad ").replace("%", " prozent ")
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)              # 22,5 -> 22.5
    t = re.sub(r"[^\w\s.]", " ", t)                    # Satzzeichen weg
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)          # Punkte, die nicht in Zahlen stehen
    out = []
    for w in t.split():
        w = SYNONYMS.get(w, w)
        if w in FILLERS:
            continue
        n = _word_number(w)
        if n is not None:
            w = str(n)
        if w == "komma" and out and out[-1].isdigit():   # "zweiundzwanzig komma fünf"
            out[-1] += "."
            continue
        if out and out[-1].endswith(".") and w.isdigit():
            out[-1] += w
            continue
        out.append(w)
    return " ".join(out)


# -------------------- Aktionen --------------------

def _num(s):
    return float(s.replace(",", "."))


AKTIONEN = {
    "licht_an": ("Licht an", ["raum"], lambda s: [{"device": "light", "room": s["raum"], "action": "on"}]),
    "licht_aus": ("Licht aus", ["raum"], lambda s: [{"device": "light", "room": s["raum"], "action": "off"}]),
    "licht_dimmen": ("Licht dimmen (Prozent)", ["raum", "zahl"],
                     lambda s: [{"device": "light", "room": s["raum"], "action": "set", "value": int(_num(s["zahl"]))}]),
    "garage_auf": ("Garagentor öffnen", [], lambda s: [{"device": "garage", "action": "open"}]),
    "garage_zu": ("Garagentor schließen", [], lambda s: [{"device": "garage", "action": "close"}]),
    "heizung_an": ("Heizung an", [], lambda s: [{"device": "heating", "action": "on"}]),
    "heizung_aus": ("Heizung aus", [], lambda s: [{"device": "heating", "action": "off"}]),
    "heizung_temp": ("Heizung auf Temperatur", ["zahl"],
                     lambda s: [{"device": "heating", "action": "set", "value": _num(s["zahl"])}]),
    "klima_an": ("Klimaanlage an", [], lambda s: [{"device": "cooling", "action": "on"}]),
    "klima_aus": ("Klimaanlage aus", [], lambda s: [{"device": "cooling", "action": "off"}]),
    "klima_temp": ("Klimaanlage auf Temperatur", ["zahl"],
                   lambda s: [{"device": "cooling", "action": "set", "value": _num(s["zahl"])}]),
    "luefter_an": ("Lüfter an", [], lambda s: [{"device": "fan", "action": "on"}]),
    "luefter_aus": ("Lüfter aus", [], lambda s: [{"device": "fan", "action": "off"}]),
    "luefter_auto": ("Lüfter automatisch", [], lambda s: [{"device": "fan", "action": "auto"}]),
    "alles_aus": ("Alles aus (Szene)", [], lambda s: list(ki.SCENES["alles_aus"])),
}

STANDARD = [
    ("licht {raum} an", "licht_an"), ("{raum} licht an", "licht_an"), ("licht an", "licht_an"),
    ("licht {raum} aus", "licht_aus"), ("{raum} licht aus", "licht_aus"), ("licht aus", "licht_aus"),
    ("licht {raum} auf {zahl} prozent", "licht_dimmen"), ("licht {raum} {zahl} prozent", "licht_dimmen"),
    ("{raum} licht auf {zahl} prozent", "licht_dimmen"), ("{raum} auf {zahl} prozent", "licht_dimmen"),
    ("garage auf", "garage_auf"), ("garage zu", "garage_zu"), ("auf garage", "garage_auf"), ("zu garage", "garage_zu"),
    ("heizung an", "heizung_an"), ("heizung aus", "heizung_aus"),
    ("heizung auf {zahl} grad", "heizung_temp"), ("heizung {zahl} grad", "heizung_temp"),
    ("klimaanlage an", "klima_an"), ("klimaanlage aus", "klima_aus"),
    ("klimaanlage auf {zahl} grad", "klima_temp"), ("klimaanlage {zahl} grad", "klima_temp"),
    ("lüfter an", "luefter_an"), ("lüfter aus", "luefter_aus"), ("lüfter auto", "luefter_auto"),
    ("alles aus", "alles_aus"),
]

# -------------------- Datenbank --------------------


def _db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init():
    with _lock, _db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS muster (id INTEGER PRIMARY KEY, muster TEXT UNIQUE,
                       aktion TEXT, werte TEXT DEFAULT '{}', aktiv INTEGER DEFAULT 1, treffer INTEGER DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS vorschlaege (satz TEXT PRIMARY KEY, befehl TEXT,
                       anzahl INTEGER DEFAULT 1, zuletzt REAL)""")
        if not con.execute("SELECT 1 FROM muster LIMIT 1").fetchone():
            con.executemany("INSERT INTO muster (muster, aktion) VALUES (?, ?)", STANDARD)
    _reload()


_compiled = []


def _pattern_regex(muster):
    room_alt = "|".join(sorted(map(re.escape, ROOM_WORDS), key=len, reverse=True))
    parts = []
    for w in normalize(muster.replace("{raum}", " RAUMPLATZ ").replace("{zahl}", " ZAHLPLATZ ")).split():
        if w == "raumplatz":
            parts.append(f"(?P<raum>{room_alt})")
        elif w == "zahlplatz":
            parts.append(r"(?P<zahl>\d+(?:\.\d+)?)")
        else:
            parts.append(re.escape(w))
    return re.compile("^" + " ".join(parts) + "$")


def _reload():
    global _compiled
    with _db() as con:
        rows = con.execute("SELECT * FROM muster WHERE aktiv = 1").fetchall()
    _compiled = [(r["id"], _pattern_regex(r["muster"]), r["aktion"], json.loads(r["werte"] or "{}")) for r in rows]


def match(text):
    """Gibt (muster_id, aktion, slots) zurueck oder None."""
    norm = normalize(text)
    for mid, rx, aktion, werte in _compiled:
        m = rx.match(norm)
        if m:
            slots = dict(werte)
            for k, v in m.groupdict().items():
                if v is not None:
                    slots[k] = ROOM_WORDS.get(v, v) if k == "raum" else v
            return mid, aktion, slots
    return None


# -------------------- Ausfuehren --------------------

_pending = {}     # quelle -> offene Rueckfrage
STATS = {"direkt": 0, "ki": 0}


def _question(aktion, slot):
    if slot == "raum":
        return config.SCHNELL_FRAGE_RAUM
    return config.SCHNELL_FRAGE_PROZENT if aktion == "licht_dimmen" else config.SCHNELL_FRAGE_GRAD


def _missing(aktion, slots):
    return [s for s in AKTIONEN[aktion][1] if s not in slots]


def _execute(text, aktion, slots, pruefen):
    cmds = [ki.validate(c) for c in AKTIONEN[aktion][2](slots)]
    if not cmds or not all(cmds):
        return None                                # z. B. ungueltiger Wert -> lieber die KI fragen
    denied, msg = [], ""
    if pruefen:
        cmds, denied, msg = pruefen(cmds)
    if not cmds:
        antwort = msg
    else:
        try:
            esp = ki.send_commands(cmds)
        except Exception:
            esp = {"status": "error"}
        antwort = ki.SCENE_TEXT["alles_aus"] if aktion == "alles_aus" and not denied else ki.confirm(cmds)
        if denied and msg:
            antwort += " " + msg
        if esp.get("status") == "error":
            antwort += " Allerdings hat das Haus den Befehl nicht angenommen."
    ki.remember(text, antwort, cmds)
    return {"antwort": antwort, "befehle": cmds, "art": "befehl", "rueckfrage": False, "quelle": "schnell"}


def _ask(quelle, text, aktion, slots, versuche=0, prefix=""):
    fehlt = _missing(aktion, slots)[0]
    _pending[quelle] = {"aktion": aktion, "slots": slots, "fehlt": fehlt, "text": text,
                        "zeit": time.time(), "versuche": versuche}
    return {"antwort": (prefix + _question(aktion, fehlt)).strip(), "befehle": [], "art": "rueckfrage",
            "rueckfrage": True, "quelle": "schnell"}


def _fill(slot, text):
    norm = normalize(text)
    if slot == "raum":
        for w in norm.split():
            if w in ROOM_WORDS:
                return ROOM_WORDS[w]
    else:
        m = re.search(r"\d+(?:\.\d+)?", norm)
        if m:
            return m.group(0)
    return None


def cancel(quelle):
    """Offene Rueckfrage verwerfen. True, wenn es eine gab."""
    return _pending.pop(quelle, None) is not None


def try_handle(text, quelle, pruefen=None):
    """Versucht den Satz direkt zu erledigen. Gibt (ergebnis, text_fuer_ki) zurueck:
    ergebnis = fertiges Ergebnis oder None; text_fuer_ki = was die KI bekommen soll, falls None."""
    if not getattr(config, "SCHNELL_AKTIV", True):
        return None, text

    timeout = 25 if quelle == "sprache" else 120
    p = _pending.get(quelle)
    if p and time.time() - p["zeit"] > timeout:
        _pending.pop(quelle, None)
        p = None

    if p:
        if set(normalize(text).split()) & CANCEL:
            _pending.pop(quelle, None)
            return {"antwort": "Okay, dann nicht.", "befehle": [], "art": "sonstiges",
                    "rueckfrage": False, "quelle": "schnell"}, text
        if not match(text):                                 # sonst: neuer, vollstaendiger Befehl
            value = _fill(p["fehlt"], text)
            if value is not None:
                _pending.pop(quelle, None)
                slots = {**p["slots"], p["fehlt"]: value}
                full = f"{p['text']} - {text}"
                if _missing(p["aktion"], slots):
                    return _ask(quelle, full, p["aktion"], slots), text
                return _execute(full, p["aktion"], slots, pruefen), text
            if p["versuche"] < 1:
                return _ask(quelle, p["text"], p["aktion"], p["slots"], 1, "Das habe ich nicht verstanden. "), text
            _pending.pop(quelle, None)
            return None, f"{p['text']} ({text})"            # zweimal unklar -> die KI soll es versuchen
        _pending.pop(quelle, None)

    m = match(text)
    if not m:
        return None, text
    mid, aktion, slots = m
    with _lock, _db() as con:
        con.execute("UPDATE muster SET treffer = treffer + 1 WHERE id = ?", (mid,))
    if _missing(aktion, slots):
        return _ask(quelle, text, aktion, slots), text
    return _execute(text, aktion, slots, pruefen), text


def antworte(text, quelle, pruefen=None):
    """Einstieg fuer Chat und Sprache: erst Schnellerkennung, sonst KI."""
    start = time.time()
    result, ki_text = try_handle(text, quelle, pruefen)
    if result is None:
        result = ki.handle(ki_text, pruefen)
        result["quelle"] = "ki"
        STATS["ki"] += 1
    else:
        STATS["direkt"] += 1
    result["dauer_ms"] = int((time.time() - start) * 1000)
    print(f"[SCHNELL] {result['quelle']} in {result['dauer_ms']} ms: '{text}' -> '{result.get('antwort', '')}'")
    return result


# -------------------- Vorschlaege (Saetze, die die KI mit einem einfachen Befehl geloest hat) ----------

def _command_to_action(c):
    d, a, v = c.get("device"), c.get("action"), c.get("value")
    if d == "light":
        if a == "set":
            return "licht_dimmen", {"raum": c["room"], "zahl": str(v)}
        return ("licht_an" if a == "on" else "licht_aus"), {"raum": c["room"]}
    if d == "garage":
        return ("garage_auf" if a == "open" else "garage_zu"), {}
    if d in ("heating", "cooling"):
        pre = "heizung" if d == "heating" else "klima"
        if a == "off":
            return pre + "_aus", {}
        return (pre + "_temp", {"zahl": str(v)}) if v is not None else (pre + "_an", {})
    if d == "fan":
        return {"on": "luefter_an", "off": "luefter_aus", "auto": "luefter_auto"}[a], {}
    return None


def record_candidate(text, cmd):
    norm = normalize(text)
    if not norm or len(norm) > 80 or not _command_to_action(cmd):
        return
    with _lock, _db() as con:
        con.execute("""INSERT INTO vorschlaege (satz, befehl, anzahl, zuletzt) VALUES (?, ?, 1, ?)
                       ON CONFLICT(satz) DO UPDATE SET anzahl = anzahl + 1, befehl = excluded.befehl,
                       zuletzt = excluded.zuletzt""", (norm, json.dumps(cmd), time.time()))


ki.SIMPLE_COMMAND_HOOKS.append(record_candidate)


# -------------------- Verwaltung (Admin-Bereich) --------------------

def describe(aktion, slots):
    label = AKTIONEN[aktion][0]
    extra = []
    if "raum" in slots:
        extra.append("überall" if slots["raum"] == "alle" else ki.ROOM_NAMES.get(slots["raum"], slots["raum"]))
    if "zahl" in slots:
        extra.append(slots["zahl"].replace(".", ","))
    return label + (f" ({', '.join(extra)})" if extra else "")


def list_all():
    with _db() as con:
        muster = [dict(r) for r in con.execute("SELECT * FROM muster ORDER BY aktion, muster")]
        vorschl = [dict(r) for r in con.execute(
            "SELECT * FROM vorschlaege ORDER BY anzahl DESC, zuletzt DESC LIMIT 30")]
    for m in muster:
        m["werte"] = json.loads(m["werte"] or "{}")
        m["label"] = describe(m["aktion"], m["werte"])
    for v in vorschl:
        act = _command_to_action(json.loads(v["befehl"]))
        v["label"] = describe(*act) if act else "?"
    return {"muster": muster, "vorschlaege": vorschl, "statistik": STATS,
            "aktionen": [{"key": k, "label": a[0], "slots": a[1]} for k, a in AKTIONEN.items()]}


def add(muster, aktion, werte=None):
    muster = " ".join(str(muster).split())[:80]
    werte = werte or {}
    if aktion not in AKTIONEN:
        raise ValueError("Unbekannte Aktion.")
    if not normalize(muster.replace("{raum}", "x").replace("{zahl}", "1")):
        raise ValueError("Das Muster ist leer.")
    if "{raum}" in muster and "raum" in werte or "{zahl}" in muster and "zahl" in werte:
        raise ValueError("Ein Wert steht doppelt im Muster und in den festen Werten.")
    if "raum" in werte and werte["raum"] not in ROOMS:
        raise ValueError("Unbekannter Raum.")
    try:
        _pattern_regex(muster)
    except re.error:
        raise ValueError("Das Muster ist ungültig.")
    with _lock, _db() as con:
        try:
            con.execute("INSERT INTO muster (muster, aktion, werte) VALUES (?, ?, ?)",
                        (muster, aktion, json.dumps(werte)))
        except sqlite3.IntegrityError:
            raise ValueError("Dieses Muster gibt es schon.")
    _reload()


def set_active(mid, aktiv):
    with _lock, _db() as con:
        con.execute("UPDATE muster SET aktiv = ? WHERE id = ?", (1 if aktiv else 0, int(mid)))
    _reload()


def delete(mid):
    with _lock, _db() as con:
        con.execute("DELETE FROM muster WHERE id = ?", (int(mid),))
    _reload()


def accept_suggestion(satz):
    with _db() as con:
        row = con.execute("SELECT * FROM vorschlaege WHERE satz = ?", (satz,)).fetchone()
    if not row:
        raise ValueError("Diesen Vorschlag gibt es nicht mehr.")
    aktion, werte = _command_to_action(json.loads(row["befehl"]))
    add(satz, aktion, werte)
    dismiss_suggestion(satz)


def dismiss_suggestion(satz):
    with _lock, _db() as con:
        con.execute("DELETE FROM vorschlaege WHERE satz = ?", (satz,))


def test(text):
    """Fuer das Testfeld im Admin-Bereich: was wuerde mit diesem Satz passieren?"""
    m = match(text)
    norm = normalize(text)
    if not m:
        return {"direkt": False, "normalisiert": norm, "beschreibung": "geht an die KI"}
    _, aktion, slots = m
    fehlt = _missing(aktion, slots)
    return {"direkt": True, "normalisiert": norm, "beschreibung": describe(aktion, slots),
            "rueckfrage": _question(aktion, fehlt[0]) if fehlt else ""}


# -------------------- Saetze, die die Sprachausgabe vorab speichert --------------------

def prewarm_texts():
    texts = [config.SCHNELL_FRAGE_RAUM, config.SCHNELL_FRAGE_PROZENT, config.SCHNELL_FRAGE_GRAD,
             "Okay, dann nicht.", "Das habe ich nicht verstanden. " + config.SCHNELL_FRAGE_RAUM,
             ki.SCENE_TEXT["alles_aus"]]
    for room in ki.LIGHT_ROOMS + ["alle"]:
        for a in ("on", "off"):
            texts.append(ki.confirm([{"device": "light", "room": room, "action": a}]))
    for c in ({"device": "garage", "action": "open"}, {"device": "garage", "action": "close"},
              {"device": "heating", "action": "on"}, {"device": "heating", "action": "off"},
              {"device": "cooling", "action": "on"}, {"device": "cooling", "action": "off"},
              {"device": "fan", "action": "on"}, {"device": "fan", "action": "off"}):
        texts.append(ki.confirm([c]))
    return texts
