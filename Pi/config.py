# Einstellungen fuer den Smarthome-Pi - hier die eigenen Werte eintragen

ESP_IP = "192.168.178.50"                             # IP des ESP32 (Serieller Monitor)
OLLAMA_URL = "http://192.168.178.20:11434/api/chat"   # IP des KI-Servers (zurzeit Laptop)
MODEL = "llama3.1:8b"                                 # exakter Name aus "ollama list"

WEB_PORT = 5000          # Dashboard: http://<Pi-IP>:5000
HISTORY_MESSAGES = 6     # wie viele vorherige Nachrichten die KI sieht (fuer Rueckfragen)
