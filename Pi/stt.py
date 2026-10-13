"""
Spracherkennung (Sprache -> Text) mit Whisper.

Zwei Betriebsarten (in config.py: STT_MODE):
  "server" - Aufnahme geht an stt_server.py auf dem KI-Server (schnell, genau)
  "local"  - faster-whisper laeuft direkt auf dem Pi (langsamer, aber ohne Server;
             gut als Notloesung fuer den Vorfuehrungstag)
"""

import io
import time
import wave

import numpy as np
import requests

import config

SAMPLE_RATE = 16000

# Saetze, die Whisper bei Stille oder Rauschen gerne "erfindet" (aus Untertitel-Trainingsdaten)
HALLUCINATIONS = ("untertitel", "zdf", "vielen dank fürs zuschauen", "vielen dank für's zuschauen",
                  "bis zum nächsten mal", "amara.org", "copyright", "swr")


def clean(text):
    """Leert den Text, wenn er wie eine typische Whisper-Erfindung aussieht."""
    low = text.lower()
    if any(h in low for h in HALLUCINATIONS):
        print(f"[STT] Verworfen (typische Halluzination): '{text}'")
        return ""
    return text

_local_model = None


def pcm_to_wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def load():
    """Lokales Modell einmal beim Start laden (nur im Modus "local")."""
    global _local_model
    if config.STT_MODE != "local" or _local_model is not None:
        return
    from faster_whisper import WhisperModel
    start = time.time()
    _local_model = WhisperModel(config.STT_LOCAL_MODEL, device="cpu", compute_type="int8")
    print(f"[STT] Lokales Whisper-Modell '{config.STT_LOCAL_MODEL}' geladen in {time.time() - start:.1f} s")


def transcribe(pcm):
    """Nimmt rohes Audio (16 kHz, 16 Bit, mono) und gibt den erkannten Text zurueck."""
    start = time.time()
    if config.STT_MODE == "local":
        load()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = _local_model.transcribe(audio, language="de", beam_size=1,
                                              vad_filter=True,
                                              condition_on_previous_text=False)
        text = " ".join(s.text for s in segments if s.no_speech_prob < 0.6).strip()
    else:
        r = requests.post(config.STT_URL, data=pcm_to_wav(pcm),
                          headers={"Content-Type": "audio/wav"}, timeout=30)
        r.raise_for_status()
        text = r.json().get("text", "").strip()
    print(f"[STT] ({time.time() - start:.1f} s) '{text}'")
    return clean(text)
