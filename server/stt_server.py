"""
Spracherkennungs-Dienst fuer den KI-Server (laeuft neben Ollama).

Der Pi schickt eine WAV-Aufnahme an  POST http://<Server>:9000/transcribe
und bekommt  {"text": "..."}  zurueck.

Installation:  pip install faster-whisper flask
Start:         python stt_server.py   (auf dem Linux-Server: ./start_stt.sh bzw. als Dienst)
Beim ersten Start wird das Whisper-Modell einmalig heruntergeladen.

Einstellungen koennen auch per Umgebungsvariable gesetzt werden (fuer den Dienst):
  STT_MODEL, STT_DEVICE, STT_COMPUTE, STT_HOST, STT_PORT
"""

import io
import os
import time
import wave

import numpy as np
from faster_whisper import WhisperModel
from flask import Flask, jsonify, request

# tiny / base / small / medium / large-v3-turbo - groesser = genauer, aber langsamer
MODEL = os.environ.get("STT_MODEL", "small")
DEVICE = os.environ.get("STT_DEVICE", "cpu")      # "cpu" oder "cuda" (NVIDIA-Grafikkarte)
COMPUTE = os.environ.get("STT_COMPUTE", "int8")   # bei "cuda" besser "float16"
HOST = os.environ.get("STT_HOST", "0.0.0.0")      # auf dem Server: nur die Tailscale-IP
PORT = int(os.environ.get("STT_PORT", "9000"))

print(f"Lade Whisper-Modell '{MODEL}' ...")
start = time.time()
model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE)
print(f"Fertig in {time.time() - start:.1f} s")

app = Flask(__name__)


@app.get("/")
def info():
    return jsonify({"dienst": "stt", "modell": MODEL, "geraet": DEVICE})


@app.post("/transcribe")
def transcribe():
    start = time.time()
    with wave.open(io.BytesIO(request.get_data()), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            return jsonify({"error": "erwarte 16 kHz, mono, 16 Bit"}), 400
        pcm = w.readframes(w.getnframes())
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    # vad_filter schneidet Stille weg -> Whisper erfindet bei Rauschen keine Woerter
    segments, _ = model.transcribe(audio, language="de", beam_size=5, vad_filter=True,
                                   condition_on_previous_text=False)
    text = " ".join(s.text for s in segments if s.no_speech_prob < 0.6).strip()
    print(f"({time.time() - start:.1f} s) {text}")
    return jsonify({"text": text})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=False)
