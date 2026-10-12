"""
Webserver auf dem Pi: Dashboard, manuelle Steuerung und KI-Chat.
Start:  python3 app.py   ->   im Browser http://<Pi-IP>:5000
"""

import requests
from flask import Flask, jsonify, request, send_from_directory

import config
import ki

app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/state")
def state():
    try:
        return jsonify({"online": True, **ki.get_state()})
    except requests.RequestException:
        return jsonify({"online": False})


@app.post("/api/command")
def command():
    """Manuelle Steuerung aus dem Dashboard (wird genauso geprueft wie KI-Befehle)."""
    data = request.get_json(silent=True) or {}
    commands = [ki.validate(c, allow_window=True) for c in data.get("commands", [])]
    commands = [c for c in commands if c]
    if not commands:
        return jsonify({"status": "error", "error": "keine gueltigen Befehle"}), 400
    try:
        return jsonify(ki.send_commands(commands))
    except requests.RequestException:
        return jsonify({"status": "error", "error": "ESP nicht erreichbar"}), 502


@app.post("/api/chat")
def chat():
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"antwort": "", "befehle": []}), 400
    return jsonify(ki.handle(text))


@app.post("/api/chat/reset")
def chat_reset():
    ki.reset_history()
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.WEB_PORT, threaded=True)
