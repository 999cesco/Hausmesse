"""
Sprachausgabe mit Piper.

Das Sprachmodell wird EINMAL beim Start im Hintergrund geladen (~7 s auf dem Pi 4)
und bleibt danach im Speicher. Jede Antwort ist dann in unter einer Sekunde hoerbar.

Texte werden in eine Warteschlange gelegt und nacheinander vorgelesen,
damit sich zwei Antworten nie gegenseitig ueberlagern.
"""

import io
import queue
import subprocess
import threading
import time
import wave

import config

_voice = None
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


def _speak(text):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        _voice.synthesize_wav(text, wav_file)
    # WAV direkt aus dem Speicher an aplay geben, ohne Datei auf der SD-Karte
    subprocess.run(
        ["aplay", "-q", "-D", config.TTS_DEVICE, "-"],
        input=buf.getvalue(),
        check=False,
    )


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
        text = _queue.get()
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
