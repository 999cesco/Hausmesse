"""
Einstellungen, die im Admin-Bereich geaendert werden koennen.

Geaenderte Werte landen in einstellungen.json (von Git ignoriert) und ueberschreiben
die Werte aus config.py. "Auf Standard zuruecksetzen" loescht die Datei wieder.
Jeder Wert wird streng geprueft, bevor er gespeichert wird.
"""

import importlib
import json
import os
import re

import config

BASE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(BASE, "einstellungen.json")

HOST = r"[A-Za-z0-9.\-]{1,253}"
URL = r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?"
DEVICE = r"[A-Za-z0-9_:=,.\-]{1,64}"

# key, Beschriftung, Gruppe, Typ, Zusatz, Neustart noetig
FIELDS = [
    ("ESP_IP", "Adresse des ESP", "Verbindungen", "text", {"pattern": HOST}, False),
    ("OLLAMA_URL", "KI-Server (Ollama)", "Verbindungen", "text", {"pattern": URL}, False),
    ("STT_URL", "Spracherkennung (Whisper)", "Verbindungen", "text", {"pattern": URL}, False),
    ("SERVER_CA", "Zertifikat des Servers (Pfad, sonst leer)", "Verbindungen", "text",
     {"pattern": r"(/[A-Za-z0-9._\-/]+)?"}, False),

    ("MODEL", "Sprachmodell", "KI", "text", {"pattern": r"[A-Za-z0-9._:\-/]{1,80}"}, False),
    ("HISTORY_MESSAGES", "Erinnert sich an so viele Nachrichten", "KI", "int", {"min": 0, "max": 20}, False),

    ("TTS_ENABLED", "Antworten vorlesen", "Sprachausgabe", "bool", {}, True),
    ("TTS_VOICE", "Stimme (Pfad zur .onnx-Datei)", "Sprachausgabe", "text",
     {"pattern": r"/[A-Za-z0-9._\-/]+\.onnx"}, True),
    ("TTS_DEVICE", "Audioausgang", "Sprachausgabe", "text", {"pattern": DEVICE}, False),

    ("VOICE_ENABLED", "Sprachsteuerung aktiv", "Sprachsteuerung", "bool", {}, True),
    ("MIC_DEVICE", "Mikrofon", "Sprachsteuerung", "text", {"pattern": DEVICE}, True),
    ("WAKEWORD_ENGINE", "Wakeword-Erkennung mit", "Sprachsteuerung", "select",
     {"options": ["openwakeword", "porcupine"]}, True),
    ("WAKEWORD", "openWakeWord: Modell (Name oder Pfad zu .onnx)", "Sprachsteuerung", "text",
     {"pattern": r"(hey_jarvis|alexa|hey_mycroft|/[A-Za-z0-9._\-/]+\.onnx)"}, True),
    ("WAKEWORD_THRESHOLD", "openWakeWord: Schwelle (höher = seltener)", "Sprachsteuerung", "float",
     {"min": 0.1, "max": 0.95, "step": 0.05}, False),
    ("PORCUPINE_KEYWORD", "Porcupine: Wakeword-Datei (.ppn)", "Sprachsteuerung", "text",
     {"pattern": r"/[A-Za-z0-9._\-/]+\.ppn"}, True),
    ("PORCUPINE_MODEL", "Porcupine: Sprachmodell (.pv)", "Sprachsteuerung", "text",
     {"pattern": r"(/[A-Za-z0-9._\-/]+\.pv)?"}, True),
    ("PORCUPINE_SENSITIVITY", "Porcupine: Empfindlichkeit (höher = reagiert leichter)", "Sprachsteuerung",
     "float", {"min": 0.1, "max": 0.95, "step": 0.05}, True),
    ("SILENCE_END_MS", "Pause bis Aufnahmeende (ms)", "Sprachsteuerung", "int", {"min": 300, "max": 3000}, False),
    ("SPEECH_FACTOR", "Sprache muss so viel lauter sein als Rauschen", "Sprachsteuerung", "float",
     {"min": 1.2, "max": 6, "step": 0.1}, False),
    ("VAD_LEVEL", "Strenge der Spracherkennung (0-3)", "Sprachsteuerung", "int", {"min": 0, "max": 3}, True),
    ("LISTEN_TIMEOUT_MS", "Wartezeit nach dem Piepen (ms)", "Sprachsteuerung", "int",
     {"min": 1000, "max": 15000}, False),
    ("MAX_RECORD_MS", "Längste Aufnahme (ms)", "Sprachsteuerung", "int", {"min": 3000, "max": 30000}, False),
    ("MAX_FOLLOWUPS", "Rückfragen ohne Wakeword", "Sprachsteuerung", "int", {"min": 0, "max": 5}, False),

    ("SCHNELL_AKTIV", "Einfache Befehle direkt erkennen (ohne KI)", "Schnellerkennung", "bool", {}, False),
    ("SCHNELL_FRAGE_RAUM", "Rückfrage, wenn der Raum fehlt", "Schnellerkennung", "text",
     {"pattern": r"[\wÄÖÜäöüß ,.?!-]{2,60}"}, False),
    ("SCHNELL_FRAGE_PROZENT", "Rückfrage beim Dimmen ohne Wert", "Schnellerkennung", "text",
     {"pattern": r"[\wÄÖÜäöüß ,.?!-]{2,60}"}, False),
    ("SCHNELL_FRAGE_GRAD", "Rückfrage bei Temperatur ohne Wert", "Schnellerkennung", "text",
     {"pattern": r"[\wÄÖÜäöüß ,.?!-]{2,60}"}, False),

    ("STT_MODE", "Spracherkennung läuft auf", "Spracherkennung", "select",
     {"options": ["server", "local"]}, False),
    ("STT_LOCAL_MODEL", "Modell, wenn auf dem Pi", "Spracherkennung", "select",
     {"options": ["tiny", "base", "small"]}, True),
]
_BY_KEY = {f[0]: f for f in FIELDS}


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def describe():
    overrides = _load()
    out = []
    for key, label, group, typ, extra, restart in FIELDS:
        out.append({"key": key, "label": label, "group": group, "type": typ,
                    "value": getattr(config, key, None), "changed": key in overrides,
                    "restart": restart, **extra})
    return out


def _convert(key, raw):
    _, label, _, typ, extra, _ = _BY_KEY[key]
    if typ == "bool":
        if not isinstance(raw, bool):
            raise ValueError(f"{label}: ungültiger Wert")
        return raw
    if typ in ("int", "float"):
        try:
            value = int(raw) if typ == "int" else round(float(raw), 3)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: bitte eine Zahl eingeben")
        if not extra["min"] <= value <= extra["max"]:
            raise ValueError(f"{label}: erlaubt sind {extra['min']} bis {extra['max']}")
        return value
    value = str(raw).strip()
    if typ == "select":
        if value not in extra["options"]:
            raise ValueError(f"{label}: ungültige Auswahl")
        return value
    if not re.fullmatch(extra["pattern"], value):
        raise ValueError(f"{label}: ungültiges Format")
    return value


def save(values):
    """Prueft alles zuerst und speichert nur, wenn jeder Wert gueltig ist."""
    if not isinstance(values, dict):
        raise ValueError("Ungültige Daten")
    clean = {}
    for key, raw in values.items():
        if key not in _BY_KEY:
            raise ValueError(f"Unbekannte Einstellung: {key}")
        clean[key] = _convert(key, raw)

    restart = []
    overrides = _load()
    for key, value in clean.items():
        if getattr(config, key, None) != value:
            if _BY_KEY[key][5]:
                restart.append(_BY_KEY[key][1])
            setattr(config, key, value)       # sofort wirksam
        overrides[key] = value

    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FILE)
    return restart


def reset():
    """Alle Aenderungen verwerfen -> wieder die Werte aus config.py."""
    if os.path.exists(FILE):
        os.remove(FILE)
    importlib.reload(config)
