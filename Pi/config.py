# Einstellungen fuer den Smarthome-Pi - hier die eigenen Werte eintragen

ESP_IP = "192.168.0.103"                              # IP des ESP32 (Serieller Monitor)
OLLAMA_URL = "http://192.168.0.105:11434/api/chat"    # IP des KI-Servers (zurzeit Laptop)
MODEL = "llama3.1:8b"                                 # exakter Name aus "ollama list"

WEB_PORT = 5000          # Dashboard: http://<Pi-IP>:5000
HISTORY_MESSAGES = 6     # wie viele vorherige Nachrichten die KI sieht (fuer Rueckfragen)

# Sprachausgabe (Piper)
TTS_ENABLED = True
TTS_VOICE = "/home/admin/piper/de_DE-thorsten-medium.onnx"   # Pfad zur geladenen Stimme
TTS_DEVICE = "default"   # Audioausgang fuer aplay

# Sprachsteuerung (Wakeword + Spracherkennung)
VOICE_ENABLED = True
MIC_DEVICE = "plughw:CARD=Q91,DEV=0"    # Mikro per Name statt Nummer (Nummer kann wechseln)
WAKEWORD = "hey_jarvis"                 # oder "alexa", "hey_mycroft"
WAKEWORD_THRESHOLD = 0.5                # hoeher = seltener faelschlich ausgeloest
VAD_LEVEL = 2                           # 0-3, hoeher = strengere Spracherkennung bei Laerm
SILENCE_END_MS = 700                    # so lange Pause = du bist fertig mit Sprechen
SPEECH_FACTOR = 2.5                     # so viel lauter als das Rauschen muss Sprache sein
LISTEN_TIMEOUT_MS = 5000                # so lange wird nach dem Piepen auf Sprache gewartet
MAX_RECORD_MS = 12000                   # laengste Aufnahme
MAX_FOLLOWUPS = 2                       # wie oft nach einer Rueckfrage ohne Wakeword weitergehoert wird

STT_MODE = "server"                     # "server" oder "local" (Whisper direkt auf dem Pi)
STT_URL = "http://192.168.0.105:9000/transcribe"    # stt_server.py auf dem KI-Server
STT_LOCAL_MODEL = "base"                # nur fuer "local": tiny / base / small
