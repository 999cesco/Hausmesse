"""
Spracherkennungs-Dienst fuer den KI-Server (laeuft neben Ollama).

Der Pi schickt eine WAV-Aufnahme an  POST http://<Server>:9000/transcribe
und bekommt  {"text": "..."}  zurueck.

Installation:  pip install faster-whisper flask
Start:         python stt_server.py
Beim ersten Start wird das Whisper-Modell einmalig heruntergeladen.
"""

import io
import time
import wave

import numpy as np
from faster_whisper import WhisperModel
from flask import Flask, jsonify, request

MODEL = "small"     # tiny / base / small / medium - groesser = genauer, aber langsamer
PORT = 9000

VOCAB_PROMPT = ("Smarthome, Heizung, Klimaanlage, Luefter, Garagentor, Schlafzimmer, "
                "Arbeitszimmer, Wohnzimmer, Kueche, Garage, Fenster, Licht, Grad.")

print(f"Lade Whisper-Modell '{MODEL}' ...")
start = time.time()
# device="auto": nutzt eine NVIDIA-Grafikkarte, falls vorhanden, sonst den Prozessor
model = WhisperModel(MODEL, device="auto", compute_type="auto")
print(f"Fertig in {time.time() - start:.1f} s")

app = Flask(__name__)


@app.get("/")
def info():
    return jsonify({"dienst": "stt", "modell": MODEL})


@app.post("/transcribe")
def transcribe():
    start = time.time()
    with wave.open(io.BytesIO(request.get_data()), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            return jsonify({"error": "erwarte 16 kHz, mono, 16 Bit"}), 400
        pcm = w.readframes(w.getnframes())
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio, language="de", beam_size=5,
                                   initial_prompt=VOCAB_PROMPT)
    text = " ".join(s.text for s in segments).strip()
    print(f"({time.time() - start:.1f} s) {text}")
    return jsonify({"text": text})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=False)
