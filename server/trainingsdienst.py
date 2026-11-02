"""
KI-Training als Dienst auf dem Server.

Laeuft dauerhaft neben Ollama und trainiert die Zusatzregeln der KI - auch wenn der Pi aus ist.
Der Pi steuert ihn ueber das VPN (Admin-Bereich -> KI-Training) und meldet sich jede Minute
("Herzschlag"). Daran erkennt der Dienst, ob gerade jemand das Smarthome benutzt:

  - Automatik: Hat sich der Pi seit AUTO_PAUSE_MIN Minuten nicht gemeldet, startet der Dienst
    selbst ein Training. Meldet sich der Pi wieder, wird es nach dem aktuellen Satz angehalten.
    Gefundene Verbesserungen warten im Admin-Bereich auf Uebernahme; beim naechsten Mal wird
    von dort aus weiter verbessert.
  - Von Hand gestartet (Admin-Bereich): laeuft, auch wenn der Pi an ist. Dann ist die KI fuer
    Nutzer gesperrt, bis es fertig ist.

Benutzt denselben Code wie der Pi (Ordner Pi/ im Repo): Systemprompt, Testfaelle, Auswertung.
Nach einem git pull also auch diesen Dienst neu starten.

Start:   ~/hausmesse-venv/bin/python3 ~/Hausmesse/server/trainingsdienst.py
Umgebung: TRAINING_HOST (z. B. 192.168.95.1), TRAINING_PORT (9100), OLLAMA_URL, OLLAMA_MODEL
"""

import hashlib
import json
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "Pi"))          # Code des Pi verwenden

import config                                               # noqa: E402

config.OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
config.MODEL = os.environ.get("OLLAMA_MODEL", config.MODEL)
config.API_KEY = ""                                         # direkt am Server, kein Schluessel noetig
config.SERVER_CA = ""

import ki                                                   # noqa: E402
import training                                             # noqa: E402
from flask import Flask, jsonify, request                   # noqa: E402

HOST = os.environ.get("TRAINING_HOST", "127.0.0.1")
PORT = int(os.environ.get("TRAINING_PORT", "9100"))
SETTINGS_FILE = os.path.join(BASE, "trainingsdienst.json")

settings = {"auto": True, "pause_min": 10, "runden": 10, "pi_herzschlag": 0, "pi_prompt": "",
            "letzter_auto_hash": "", "letzter_auto_ohne_erfolg": 0}


def _load():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            settings.update(json.load(f))
    except (FileNotFoundError, ValueError):
        pass


def _save():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=1)


def prompt_hash():
    return hashlib.sha1(ki.SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


def inputs_hash():
    """Aendert sich, sobald Prompt, Testfaelle oder Live-Regeln anders sind."""
    with open(training.CASES_FILE, "rb") as f:
        cases = f.read()
    return hashlib.sha1(ki.SYSTEM_PROMPT.encode() + cases + ki.load_zusatz().encode()).hexdigest()[:12]


def pi_active():
    return time.time() - settings["pi_herzschlag"] < settings["pause_min"] * 60


# -------------------- Automatik --------------------

def auto_tick():
    """Wird alle 30 Sekunden aufgerufen: automatisch starten oder anhalten."""
    st = training.STATE
    # Pi meldet sich wieder -> automatisches Training anhalten
    if st["laeuft"] and st["quelle"] == "automatisch" and pi_active():
        training._log("Pi ist wieder an - automatisches Training wird angehalten.")
        training.stop()
        return
    if st["laeuft"] or not settings["auto"] or pi_active():
        return
    # Nichts Neues und letztes Mal keine Verbesserung? Dann hoechstens alle 6 Stunden neu probieren
    h = inputs_hash()
    if h == settings["letzter_auto_hash"] and time.time() - settings["letzter_auto_ohne_erfolg"] < 6 * 3600:
        return
    base = st["kandidat"]["zusatz"] if st["kandidat"] else None
    try:
        training.start("training", settings["runden"], quelle="automatisch", base=base)
    except ValueError:
        return
    threading.Thread(target=_after_auto, args=(h,), daemon=True).start()


def _after_auto(h):
    while training.STATE["laeuft"]:
        time.sleep(2)
    if not training.STATE.get("verbessert"):
        settings.update(letzter_auto_hash=h, letzter_auto_ohne_erfolg=time.time())
        _save()


def auto_loop():
    while True:
        time.sleep(30)
        try:
            auto_tick()
        except Exception as e:
            print(f"[TRAINING] Automatik-Fehler: {e}")


# -------------------- Schnittstelle fuer den Pi --------------------

app = Flask(__name__)


@app.get("/status")
def status():
    s = training.status()
    s.update(auto=settings["auto"], pause_min=settings["pause_min"], auto_runden=settings["runden"],
             pi_aktiv=pi_active(), gleicher_stand=(settings["pi_prompt"] in ("", prompt_hash())))
    return jsonify(s)


@app.post("/herzschlag")
def herzschlag():
    d = request.get_json(silent=True) or {}
    settings["pi_herzschlag"] = time.time()
    settings["pi_prompt"] = d.get("prompt_hash", "")
    if "live_zusatz" in d:
        training.set_live(d["live_zusatz"])
    _save()
    # Automatisches Training sofort anhalten, wenn der Pi wieder da ist
    if training.STATE["laeuft"] and training.STATE["quelle"] == "automatisch":
        training._log("Pi ist wieder an - automatisches Training wird angehalten.")
        training.stop()
    return jsonify({"ok": True})


@app.post("/start")
def start():
    d = request.get_json(silent=True) or {}
    try:
        runden = max(1, min(30, int(d.get("runden", 10))))
        training.start("test" if d.get("nur_testen") else "training", runden, quelle="manuell")
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.post("/stop")
def stop():
    training.stop()
    return jsonify({"ok": True})


@app.post("/kandidat")
def kandidat():
    d = request.get_json(silent=True) or {}
    try:
        if d.get("annehmen"):
            zusatz = training.STATE["kandidat"]["zusatz"] if training.STATE["kandidat"] else None
            training.accept()
            return jsonify({"ok": True, "zusatz": zusatz})
        training.discard()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.post("/zuruecksetzen")
def reset():
    if training.busy():
        return jsonify({"error": "Erst das laufende Training stoppen."}), 409
    training.reset_rules()
    return jsonify({"ok": True})


@app.post("/einstellungen")
def einstellungen():
    d = request.get_json(silent=True) or {}
    if "auto" in d:
        settings["auto"] = bool(d["auto"])
    if "pause_min" in d:
        settings["pause_min"] = max(2, min(240, int(d["pause_min"])))
    if "runden" in d:
        settings["runden"] = max(1, min(30, int(d["runden"])))
    settings["letzter_auto_hash"] = ""                      # neue Einstellung -> wieder probieren
    _save()
    return jsonify({"ok": True})


if __name__ == "__main__":
    _load()
    print(f"[TRAINING] Dienst auf {HOST}:{PORT}, Ollama: {config.OLLAMA_URL}, Modell: {config.MODEL}")
    threading.Thread(target=auto_loop, daemon=True).start()
    app.run(host=HOST, port=PORT, threaded=True)
