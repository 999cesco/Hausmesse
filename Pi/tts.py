"""
Sprachausgabe mit Piper.

Das Sprachmodell wird EINMAL beim Start im Hintergrund geladen (~7 s auf dem Pi 4)
und bleibt danach im Speicher. Jede Antwort ist dann in unter einer Sekunde hoerbar.

Texte werden in eine Warteschlange gelegt und nacheinander vorgelesen,
damit sich zwei Antworten nie gegenseitig ueberlagern.

Jeder vorgelesene Satz wird als WAV-Datei in tts_cache/ gespeichert. Kommt derselbe Satz wieder
("Das Garagentor schliesst sich."), wird die Datei sofort abgespielt statt neu erzeugt.
Haeufige Saetze werden nach dem Start im Hintergrund schon vorab erzeugt (prewarm).
"""

import hashlib
import io
import os
import queue
import subprocess
import threading
import time
import wave

import config

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_cache")
CACHE_MAX = 400               # so viele Saetze werden hoechstens gespeichert

_voice = None
_prewarm = []
_ready = threading.Event()
_failed = False
_queue = queue.Queue()


def _load_voice():
    global _voice
    from piper import PiperVoice  # erst hier importieren, damit app.py ohne Piper startet

    start = time.time()
    _voice = PiperVoice.load(config.TTS_VOICE)
    _ready.set()
    print(f"[TTS] Stimme geladen in {time.time() - start:.1f} s")


def _cache_path(text):
    key = hashlib.sha1(f"{config.TTS_VOICE}|{text}".encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, key + ".wav")


def _render(text):
    """Gibt die WAV-Daten fuer den Text zurueck - aus dem Speicher oder frisch erzeugt."""
    path = _cache_path(text)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        _voice.synthesize_wav(text, wav_file)
    data = buf.getvalue()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        files = sorted((os.path.join(CACHE_DIR, n) for n in os.listdir(CACHE_DIR)), key=os.path.getmtime)
        for old in files[:max(0, len(files) - CACHE_MAX)]:      # aelteste loeschen
            os.remove(old)
    except OSError as e:
        print(f"[TTS] Konnte Satz nicht speichern: {e}")
    return data


def _speak(text):
    start = time.time()
    data = _render(text)
    print(f"[TTS] bereit nach {int((time.time() - start) * 1000)} ms: '{text}'")
    subprocess.run(["aplay", "-q", "-D", config.TTS_DEVICE, "-"], input=data, check=False)


def _worker():
    global _failed
    try:
        _load_voice()
    except Exception as e:  # z. B. Stimme nicht gefunden
        _failed = True
        print(f"[TTS] Stimme konnte nicht geladen werden: {e}")
        while True:  # Warteschlange leeren, damit niemand ewig wartet
            _queue.get()
            _queue.task_done()
    while True:
        try:
            text = _queue.get(timeout=0.5)
        except queue.Empty:
            # Nichts vorzulesen -> haeufige Saetze vorab erzeugen
            if _prewarm:
                try:
                    _render(_prewarm.pop(0))
                except Exception as e:
                    print(f"[TTS] Vorab-Erzeugung fehlgeschlagen: {e}")
            continue
        try:
            _speak(text)
        except Exception as e:
            print(f"[TTS] Fehler beim Vorlesen: {e}")
        finally:
            _queue.task_done()


def start():
    """Beim Start von app.py einmal aufrufen."""
    if config.TTS_ENABLED:
        threading.Thread(target=_worker, daemon=True).start()


def say(text):
    """Text zum Vorlesen einreihen (kehrt sofort zurueck)."""
    if config.TTS_ENABLED and not _failed and text and text.strip():
        _queue.put(text.strip())


def is_ready():
    return _ready.is_set()


def is_busy():
    """True, solange noch etwas vorgelesen wird oder in der Warteschlange steht."""
    return _queue.unfinished_tasks > 0


def wait_done(timeout=60):
    """Wartet, bis alles vorgelesen ist (damit das Mikro sich nicht selbst hoert)."""
    end = time.time() + timeout
    while is_busy() and time.time() < end:
        time.sleep(0.05)


def prewarm(texts):
    """Saetze im Hintergrund vorab erzeugen, damit sie schon beim ersten Mal sofort kommen."""
    if config.TTS_ENABLED:
        _prewarm.extend(t for t in texts if t and not os.path.exists(_cache_path(t)))
