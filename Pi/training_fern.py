"""
Pi-Seite des KI-Trainings. Das eigentliche Training laeuft auf dem Server (server/trainingsdienst.py).

- Herzschlag: Jede Minute meldet sich der Pi beim Server und schickt dabei seine Live-Zusatzregeln mit.
  Solange der Pi sich meldet, trainiert der Server nicht automatisch.
- busy(): True, solange auf dem Server ein Test oder Training laeuft -> dann ist die KI fuer Nutzer gesperrt.
- Ist kein Server eingetragen (TRAINING_URL leer), laeuft das Training wie frueher direkt auf dem Pi.
"""

import hashlib
import threading
import time

import requests

import config
import ki
import training as lokal

_cache = {"zeit": 0, "status": None}


def _url():
    return (getattr(config, "TRAINING_URL", "") or "").rstrip("/")


def _call(method, path, body=None, timeout=4):
    r = requests.request(method, _url() + path, json=body, timeout=timeout)
    data = r.json()
    if r.status_code >= 400:
        raise ValueError(data.get("error", f"Fehler {r.status_code}"))
    return data


def status():
    if not _url():
        return {**lokal.status(), "auto": False, "server": False}
    try:
        s = _call("GET", "/status", timeout=3)
        s["server"] = True
    except (requests.RequestException, ValueError):
        s = {"server": False, "nicht_erreichbar": True, "laeuft": False, "log": [], "live": None,
             "kandidat": None, "live_zusatz": ki.load_zusatz()}
    _cache.update(zeit=time.time(), status=s)
    return s


def busy():
    """Wird bei jeder KI-Anfrage gefragt - deshalb hoechstens alle 3 Sekunden wirklich nachsehen."""
    if not _url():
        return lokal.busy()
    if time.time() - _cache["zeit"] > 3:
        status()
    return bool(_cache["status"] and _cache["status"].get("laeuft"))


def start(nur_testen, runden):
    if not _url():
        return lokal.start("test" if nur_testen else "training", runden)
    _call("POST", "/start", {"nur_testen": nur_testen, "runden": runden})
    _cache["zeit"] = 0


def stop():
    if not _url():
        return lokal.stop()
    _call("POST", "/stop", {})


def kandidat(annehmen):
    if not _url():
        return lokal.accept() if annehmen else lokal.discard()
    data = _call("POST", "/kandidat", {"annehmen": annehmen}, timeout=10)
    if annehmen and data.get("zusatz") is not None:
        with open(ki.ZUSATZ_FILE, "w", encoding="utf-8") as f:     # ab jetzt live auf dem Pi
            f.write(data["zusatz"])


def zuruecksetzen():
    if _url():
        _call("POST", "/zuruecksetzen", {})
    lokal.reset_rules()


def einstellungen(d):
    if not _url():
        raise ValueError("Die Automatik gibt es nur mit Trainingsdienst auf dem Server.")
    _call("POST", "/einstellungen", d)


def _herzschlag():
    while True:
        if _url():
            try:
                _call("POST", "/herzschlag", {
                    "prompt_hash": hashlib.sha1(ki.SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12],
                    "live_zusatz": ki.load_zusatz()})
            except (requests.RequestException, ValueError):
                pass                                    # Server gerade nicht erreichbar - spaeter nochmal
        time.sleep(60)


def start_herzschlag():
    threading.Thread(target=_herzschlag, daemon=True).start()
