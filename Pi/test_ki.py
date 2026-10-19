"""
Schickt eine echte KI-Anfrage an den Server - von jedem Rechner aus, ohne Pi und ohne ESP.
Benutzt genau denselben Code wie der Pi (ki.py), nur mit einem erfundenen Hauszustand.
Es wird nichts an einen ESP geschickt.

Benoetigt:  pip install requests
Aufruf (im Ordner Pi):
    python test_ki.py "Mir ist kalt"
    python test_ki.py "Ich gehe jetzt schlafen" --url http://100.x.y.z:11434/api/chat
    python test_ki.py "Wie warm ist es?" --modell llama3.1:8b --zeigen
"""

import argparse
import json
import time

import config
import ki

# Erfundener Zustand des Hauses (frei aenderbar)
TEST_STATE = {
    "sensors": {"temperature": 18.5, "humidity": 52, "soilPercent": 40},
    "lights": {"schlafzimmer": 0, "arbeitszimmer": 0, "wohnzimmer": 80, "kueche": 0, "garage": 0},
    "heating": {"on": False, "target": 21.0, "active": False},
    "cooling": {"on": False, "target": 24.0, "active": False},
    "fan": {"on": False, "mode": "auto"},
    "garage": "open",
    "windows": {"schlafzimmer": "open"},
}

p = argparse.ArgumentParser(description="KI-Anfrage testen")
p.add_argument("text", help="Was du der KI sagen willst")
p.add_argument("--url", help="Ollama-Adresse, z. B. http://100.x.y.z:11434/api/chat")
p.add_argument("--modell", help="Modellname, z. B. llama3.1:8b")
p.add_argument("--zeigen", action="store_true", help="komplette Nachricht an die KI anzeigen")
args = p.parse_args()

if args.url:
    config.OLLAMA_URL = args.url
if args.modell:
    config.MODEL = args.modell

ki.get_state = lambda: TEST_STATE
ki.send_commands = lambda cmds: {"status": "ok", "test": True}   # nichts an einen ESP schicken

if args.zeigen:
    print("=== Nachricht an die KI (ohne Systemprompt) ===")
    print(f"Nutzer-Nachricht enthaelt:\n{ki.describe_state(TEST_STATE)}\n")
    print(f"Systemprompt: {len(ki.SYSTEM_PROMPT)} Zeichen\n")

print(f"Sende an {config.OLLAMA_URL} (Modell {config.MODEL}) ...")
start = time.time()
result = ki.handle(args.text)
print(f"\n=== Ergebnis nach {time.time() - start:.1f} s ===")
print(json.dumps(result, ensure_ascii=False, indent=2))
