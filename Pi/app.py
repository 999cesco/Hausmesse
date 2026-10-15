"""
Webserver auf dem Pi: Dashboard, manuelle Steuerung, KI-Chat und Admin-Bereich.
Start:  ~/venv/bin/python3 app.py   ->   im Browser http://<Pi-IP>:5000

Alle Seiten und Schnittstellen sind passwortgeschuetzt (siehe auth.py).
  nutzer: Dashboard, Steuerung, Chat
  admin:  zusaetzlich Zustand von Pi/ESP/Server, Einstellungen, Benutzerverwaltung
"""

import html
import json
import os
import sys
import threading
import time
from datetime import timedelta

import requests
from flask import Flask, jsonify, redirect, request, session

import auth
import config
import ki
import rechte
import settings
import status_info
import tts
import voice

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

# static_folder=None: keine Datei ist ohne Anmeldung abrufbar (ausser der Login-Seite)
app = Flask(__name__, static_folder=None)
app.secret_key = auth.secret_key()
app.config.update(
    SESSION_COOKIE_NAME="hausmesse_sitzung",
    SESSION_COOKIE_HTTPONLY=True,          # JavaScript kommt nicht an das Cookie
    SESSION_COOKIE_SAMESITE="Strict",      # fremde Seiten koennen keine Befehle unterschieben
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=64 * 1024,
)


def _page(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


# ==================== Sicherheit fuer alle Anfragen ====================

@app.before_request
def require_json_for_changes():
    """Aendernde API-Aufrufe muessen JSON sein. Formulare fremder Webseiten koennen das nicht."""
    if request.method in ("POST", "PUT") and request.path.startswith("/api/") and not request.is_json:
        return jsonify({"error": "Erwarte JSON"}), 415


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ==================== Anmeldung ====================

@app.get("/login")
def login_page():
    if auth.current_user():
        return redirect("/")
    return _page("login.html")


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    ok, msg = auth.login(str(data.get("name", ""))[:64], str(data.get("passwort", ""))[:200])
    if not ok:
        return jsonify({"error": msg}), 401
    return jsonify({"ok": True})


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# ==================== Seiten ====================

TOPBAR = """
<nav class="topbar" aria-label="Konto">
  <span class="who"><span class="pre">Angemeldet als </span><b>{name}</b></span>
  {admin}
  <button type="button" class="linkbtn" id="logoutBtn">Abmelden</button>
</nav>
<script>
  window.RECHTE = {rechte};   // null = darf alles; nur fuer die Anzeige, geprueft wird auf dem Pi
  // Sitzung abgelaufen -> zur Anmeldung
  const _fetch = window.fetch;
  window.fetch = async (...a) => {{
    const r = await _fetch(...a);
    if (r.status === 401) location.href = "/login";
    return r;
  }};
  document.getElementById("logoutBtn").onclick = async () => {{
    await _fetch("/api/logout", {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: "{{}}" }});
    location.href = "/login";
  }};
</script>
"""


@app.get("/")
@auth.login_required
def index():
    user = auth.current_user()
    # Der Einstellungs-Link kommt nur fuer Admins ueberhaupt in die Seite
    admin = '<a class="linkbtn" href="/admin">Einstellungen</a>' if user["rolle"] == "admin" else ""
    bar = TOPBAR.format(name=html.escape(user["name"]), admin=admin,
                        rechte=json.dumps(user["rechte"]).replace("<", "\\u003c"))
    return _page("index.html").replace("<!--TOPBAR-->", bar)


@app.get("/admin")
@auth.login_required
def admin_page():
    user = auth.current_user()
    if user["rolle"] != "admin":
        return redirect("/")
    return _page("admin.html").replace("{{NAME}}", html.escape(user["name"]))


# ==================== Haus (alle angemeldeten Benutzer) ====================

@app.get("/api/state")
@auth.login_required
def state():
    try:
        return jsonify({"online": True, **ki.get_state()})
    except requests.RequestException:
        return jsonify({"online": False})


@app.post("/api/command")
@auth.login_required
def command():
    """Manuelle Steuerung aus dem Dashboard (wird genauso geprueft wie KI-Befehle)."""
    data = request.get_json(silent=True) or {}
    commands = [ki.validate(c, allow_window=True) for c in data.get("commands", [])[:20]]
    commands = [c for c in commands if c]
    if not commands:
        return jsonify({"status": "error", "error": "keine gueltigen Befehle"}), 400
    user = auth.current_user()
    if user["rechte"] is not None:                 # normaler Nutzer -> Rechte pruefen
        commands, denied, msg = rechte.filter_for(user["name"], user["rechte"])(commands)
        if not commands:
            return jsonify({"status": "error", "error": msg}), 403
    try:
        return jsonify(ki.send_commands(commands))
    except requests.RequestException:
        return jsonify({"status": "error", "error": "ESP nicht erreichbar"}), 502


@app.post("/api/chat")
@auth.login_required
def chat():
    user = auth.current_user()
    if user["rechte"] is not None and not user["rechte"]["chat"]:
        return jsonify({"error": "Du darfst den KI-Chat nicht benutzen."}), 403
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()[:500]
    if not text:
        return jsonify({"antwort": "", "befehle": []}), 400
    pruefen = None if user["rechte"] is None else rechte.filter_for(user["name"], user["rechte"])
    result = ki.handle(text, pruefen)
    tts.say(result.get("antwort", ""))
    return jsonify(result)


@app.post("/api/chat/reset")
@auth.login_required
def chat_reset():
    user = auth.current_user()
    if user["rechte"] is not None and not user["rechte"]["chat"]:
        return jsonify({"error": "Du darfst den KI-Chat nicht benutzen."}), 403
    ki.reset_history()
    return jsonify({"status": "ok"})


@app.get("/api/voice")
@auth.login_required
def voice_status():
    return jsonify({"status": voice.status, "stumm": voice.is_muted(), **voice.last})


@app.post("/api/voice/mute")
@auth.admin_required
def voice_mute():
    data = request.get_json(silent=True) or {}
    voice.set_muted(data.get("stumm", not voice.is_muted()))
    return jsonify({"stumm": voice.is_muted()})


# ==================== Admin ====================

@app.get("/api/admin/status")
@auth.admin_required
def admin_status():
    return jsonify(status_info.all_status())


@app.get("/api/admin/config")
@auth.admin_required
def admin_config():
    return jsonify({"felder": settings.describe()})


@app.post("/api/admin/config")
@auth.admin_required
def admin_config_save():
    try:
        restart = settings.save((request.get_json(silent=True) or {}).get("werte"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    print(f"[ADMIN] Einstellungen geaendert von '{auth.current_user()['name']}'")
    return jsonify({"ok": True, "neustart": restart})


@app.post("/api/admin/config/reset")
@auth.admin_required
def admin_config_reset():
    settings.reset()
    return jsonify({"ok": True, "neustart": ["Alle Einstellungen"]})


@app.post("/api/admin/restart")
@auth.admin_required
def admin_restart():
    def later():
        time.sleep(1)                       # Antwort erst noch an den Browser schicken
        print("[ADMIN] Neustart der Pi-Anwendung")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=later, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/admin/tts-test")
@auth.admin_required
def admin_tts_test():
    if not tts.is_ready():
        return jsonify({"error": "Die Stimme ist nicht geladen."}), 409
    tts.say("Das ist ein Test der Sprachausgabe.")
    return jsonify({"ok": True})


@app.get("/api/admin/users")
@auth.admin_required
def admin_users():
    return jsonify({"benutzer": auth.list_users(), "ich": auth.current_user()["name"],
                    "ziele": [{"key": k, "label": l} for k, l in rechte.TARGETS]})


@app.post("/api/admin/users/<name>")
@auth.admin_required
def admin_user_update(name):
    d = request.get_json(silent=True) or {}
    if name == auth.current_user()["name"] and (d.get("gesperrt") or d.get("rolle") == "nutzer"):
        return jsonify({"error": "Du kannst dich nicht selbst sperren oder herabstufen."}), 400
    try:
        auth.update_user(name, rolle=d.get("rolle"), gesperrt=d.get("gesperrt"),
                         gueltig_bis=d.get("gueltig_bis"), rechte_neu=d.get("rechte"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    print(f"[ADMIN] Benutzer '{name}' geaendert von '{auth.current_user()['name']}'")
    return jsonify({"ok": True})


@app.post("/api/admin/users")
@auth.admin_required
def admin_user_add():
    d = request.get_json(silent=True) or {}
    try:
        auth.add_user(str(d.get("name", "")).strip().lower(), d.get("passwort"), d.get("rolle"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.post("/api/admin/users/<name>/passwort")
@auth.admin_required
def admin_user_password(name):
    try:
        auth.set_password(name, (request.get_json(silent=True) or {}).get("passwort"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if session.get("user") == name:          # eigenes Passwort -> angemeldet bleiben
        session["version"] = auth.load_users()[name]["version"]
    return jsonify({"ok": True})


@app.delete("/api/admin/users/<name>")
@auth.admin_required
def admin_user_delete(name):
    if name == auth.current_user()["name"]:
        return jsonify({"error": "Du kannst dich nicht selbst löschen."}), 400
    try:
        auth.delete_user(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


if __name__ == "__main__":
    if not auth.load_users():
        print("\n[AUTH] Es gibt noch keinen Benutzer! Lege zuerst einen Admin an:")
        print("       ~/venv/bin/python3 auth.py anlegen <name> admin\n")
    tts.start()    # Stimme einmal laden, bleibt danach im Speicher
    voice.start()  # Wakeword + Spracherkennung im Hintergrund
    app.run(host="0.0.0.0", port=config.WEB_PORT, threaded=True)
