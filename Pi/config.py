# Einstellungen fuer den Smarthome-Pi - hier die eigenen Werte eintragen

ESP_IP = "192.168.0.103"                              # IP des ESP32 (Serieller Monitor)
MODEL = "llama3.1:8b"                                 # exakter Name aus "ollama list"

# ---- KI-Server ----
# Firmenserver ueber VPN (aktuell):
#   OLLAMA_URL = "http://192.168.95.1:11434/api/chat"
# Laptop im selben WLAN (Plan B):
#   OLLAMA_URL = "http://192.168.0.105:11434/api/chat"
#   STT_URL    = "http://192.168.0.105:9000/transcribe"
# Firmenserver ueber HTTPS (Caddy):
#   OLLAMA_URL = "https://SERVERADRESSE/ollama/api/chat"
#   STT_URL    = "https://SERVERADRESSE/stt/transcribe"
OLLAMA_URL = "http://192.168.95.1:11434/api/chat"     # Server ueber VPN
STT_URL = "http://192.168.95.1:9000/transcribe"

# Nur fuer HTTPS ohne eigene Domain: Pfad zum Zertifikat des Servers, sonst leer lassen
SERVER_CA = ""            # z. B. "/home/admin/Hausmesse/Pi/server_ca.crt"

# Der Schluessel steht NICHT hier (das Repo ist oeffentlich), sondern in geheim.py:
#   API_KEY = "..."
# geheim.py wird von Git ignoriert und muss auf dem Pi von Hand angelegt werden.
try:
    from geheim import API_KEY
except ImportError:
    API_KEY = ""


def server_args():
    """Zusatzangaben fuer jede Anfrage an den KI-Server (Schluessel + Zertifikat)."""
    args = {}
    if API_KEY:
        args["headers"] = {"X-API-Key": API_KEY}
    if SERVER_CA:
        args["verify"] = SERVER_CA
    return args

WEB_PORT = 5000          # Dashboard: http://<Pi-IP>:5000
HISTORY_MESSAGES = 6     # wie viele vorherige Nachrichten die KI sieht (fuer Rueckfragen)

# Sprachausgabe (Piper)
TTS_ENABLED = True
TTS_VOICE = "/home/admin/piper/de_DE-thorsten-medium.onnx"   # Pfad zur geladenen Stimme
TTS_DEVICE = "default"   # Audioausgang fuer aplay

# Sprachsteuerung (Wakeword + Spracherkennung)
VOICE_ENABLED = True
MIC_DEVICE = "plughw:CARD=Q91,DEV=0"    # Mikro per Name statt Nummer (Nummer kann wechseln)
WAKEWORD_ENGINE = "openwakeword"        # "openwakeword" oder "porcupine" (eigenes deutsches Wakeword)

# openWakeWord: fertiges Modell ("hey_jarvis", "alexa", "hey_mycroft") oder Pfad zu eigener .onnx-Datei
WAKEWORD = "hey_jarvis"
WAKEWORD_THRESHOLD = 0.5                # hoeher = seltener faelschlich ausgeloest

# Porcupine: Dateien aus der Picovoice Console; der Schluessel steht in geheim.py (PICOVOICE_KEY = "...")
PORCUPINE_KEYWORD = "/home/admin/wakeword/wakeword.ppn"         # dein Wakeword
PORCUPINE_MODEL = "/home/admin/wakeword/porcupine_params_de.pv"  # deutsches Sprachmodell
PORCUPINE_SENSITIVITY = 0.5             # hoeher = reagiert leichter, aber auch oefter faelschlich
VAD_LEVEL = 2                           # 0-3, hoeher = strengere Spracherkennung bei Laerm
SILENCE_END_MS = 700                    # so lange Pause = du bist fertig mit Sprechen
SPEECH_FACTOR = 2.5                     # so viel lauter als das Rauschen muss Sprache sein
LISTEN_TIMEOUT_MS = 5000                # so lange wird nach dem Piepen auf Sprache gewartet
MAX_RECORD_MS = 12000                   # laengste Aufnahme
MAX_FOLLOWUPS = 2                       # wie oft nach einer Rueckfrage ohne Wakeword weitergehoert wird

STT_MODE = "server"                     # "server" oder "local" (Whisper direkt auf dem Pi)
STT_LOCAL_MODEL = "base"                # nur fuer "local": tiny / base / small


# ---- Einstellungen aus dem Admin-Bereich ----
# Was im Browser unter "Einstellungen" geaendert wird, steht in einstellungen.json
# (von Git ignoriert) und ueberschreibt die Werte oben.
import json as _json
import os as _os

try:
    with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "einstellungen.json"),
              encoding="utf-8") as _f:
        for _k, _v in _json.load(_f).items():
            if _k.isupper() and _k in globals() and _k != "API_KEY":
                globals()[_k] = _v
except (FileNotFoundError, ValueError):
    pass
