"""
Sprachassistent: Wakeword -> Aufnahme -> Spracherkennung -> KI -> Sprachausgabe.

Ablauf:
  1. Das Mikro laeuft dauerhaft, openWakeWord hoert auf das Wakeword ("Hey Jarvis").
  2. Erkannt -> kurzer Signalton, Aufnahme startet.
  3. webrtcvad erkennt, wann du aufhoerst zu sprechen -> Aufnahme endet (zweiter Ton).
  4. Text an die KI (ki.handle), Antwort wird vorgelesen.
  5. Endet die Antwort mit einer Frage ("Soll ich das schliessen?"), hoert der
     Assistent direkt weiter zu - ohne erneutes Wakeword.

Waehrend der Pi selbst spricht, wird das Mikro ignoriert, damit er sich nicht selbst hoert.
"""

import queue
import subprocess
import threading
import time

import numpy as np

import config
import ki
import stt
import tts

SAMPLE_RATE = 16000
CHUNK = 1280                 # 80 ms - Blockgroesse, die openWakeWord erwartet
VAD_FRAME = 320              # 20 ms - Blockgroesse fuer webrtcvad
CHUNK_MS = 80

_audio = queue.Queue(maxsize=200)
_muted = False
status = "aus"               # aus | bereit | hoert_zu | denkt | spricht | stumm
last = {"frage": "", "antwort": ""}


# ---------- Mikrofon ----------

def _mic_reader():
    """Liest dauerhaft vom Mikro (ueber arecord) und legt 80-ms-Bloecke in die Warteschlange."""
    while True:
        proc = subprocess.Popen(
            ["arecord", "-q", "-D", config.MIC_DEVICE, "-f", "S16_LE",
             "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE)
        while True:
            data = proc.stdout.read(CHUNK * 2)
            if len(data) < CHUNK * 2:
                break
            try:
                _audio.put_nowait(data)
            except queue.Full:       # niemand liest gerade -> aeltesten Block verwerfen
                try:
                    _audio.get_nowait()
                    _audio.put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass
        print("[VOICE] Mikrofon getrennt - neuer Versuch in 2 s")
        time.sleep(2)


def _flush():
    try:
        while True:
            _audio.get_nowait()
    except queue.Empty:
        pass


# ---------- Signaltoene ----------

def _tone(freqs, ms=90):
    parts = []
    for f in freqs:
        t = np.arange(int(SAMPLE_RATE * ms / 1000)) / SAMPLE_RATE
        fade = np.minimum(1, np.minimum(t, t[-1] - t) * 200)   # weiche Kanten, kein Knacken
        parts.append(0.3 * np.sin(2 * np.pi * f * t) * fade)
    return stt.pcm_to_wav((np.concatenate(parts) * 32767).astype(np.int16).tobytes())


SOUND_START = _tone([660, 880])     # aufsteigend: "Ich hoere zu"
SOUND_END = _tone([880, 660])       # absteigend: "Verstanden"


def _beep(sound):
    subprocess.run(["aplay", "-q", "-D", config.TTS_DEVICE, "-"], input=sound, check=False)


# ---------- Aufnahme bis Stille ----------

def _record_command(vad):
    """Nimmt auf, bis nach dem Sprechen eine kurze Pause kommt. Gibt PCM oder None zurueck."""
    frames = []
    speech_seen = False
    silence_ms = 0
    waited_ms = 0
    while True:
        chunk = _audio.get()
        frames.append(chunk)
        voiced = sum(vad.is_speech(chunk[i:i + VAD_FRAME * 2], SAMPLE_RATE)
                     for i in range(0, len(chunk), VAD_FRAME * 2))
        if voiced >= 2:                       # mind. 40 ms Sprache in diesem Block
            speech_seen = True
            silence_ms = 0
        else:
            silence_ms += CHUNK_MS
        waited_ms += CHUNK_MS

        if not speech_seen and waited_ms >= config.LISTEN_TIMEOUT_MS:
            return None                        # niemand hat etwas gesagt
        if speech_seen and silence_ms >= config.SILENCE_END_MS:
            return b"".join(frames)
        if waited_ms >= config.MAX_RECORD_MS:
            return b"".join(frames)


# ---------- Hauptschleife ----------

def _dialog(vad):
    """Ein Gespraechsdurchgang inkl. Rueckfragen."""
    global status
    while True:
        status = "hoert_zu"
        _flush()
        _beep(SOUND_START)
        _flush()                               # den eigenen Piepton nicht mitaufnehmen
        pcm = _record_command(vad)
        if pcm is None:
            print("[VOICE] Nichts gehoert")
            return
        _beep(SOUND_END)

        status = "denkt"
        try:
            text = stt.transcribe(pcm)
        except Exception as e:
            print(f"[VOICE] Spracherkennung fehlgeschlagen: {e}")
            tts.say("Die Spracherkennung ist gerade nicht erreichbar.")
            tts.wait_done()
            return
        if len(text) < 2:
            tts.say("Das habe ich leider nicht verstanden.")
            tts.wait_done()
            return

        result = ki.handle(text)
        antwort = result.get("antwort", "")
        last.update(frage=text, antwort=antwort)

        status = "spricht"
        tts.say(antwort)
        time.sleep(0.2)
        tts.wait_done()

        # Rueckfrage? Dann ohne Wakeword direkt weiter zuhoeren
        if not antwort.rstrip().endswith("?"):
            return


def _loop():
    global status
    import webrtcvad
    from openwakeword.model import Model

    stt.load()
    model = Model(wakeword_models=[config.WAKEWORD], inference_framework="onnx")
    vad = webrtcvad.Vad(config.VAD_LEVEL)
    threading.Thread(target=_mic_reader, daemon=True).start()
    print(f"[VOICE] Bereit - sag '{config.WAKEWORD}'")

    while True:
        status = "stumm" if _muted else ("spricht" if tts.is_busy() else "bereit")
        if _muted or tts.is_busy():            # stumm oder Pi spricht gerade
            _flush()
            model.reset()
            time.sleep(0.1)
            continue

        chunk = _audio.get()
        scores = model.predict(np.frombuffer(chunk, dtype=np.int16))
        score = max(scores.values())
        if score >= config.WAKEWORD_THRESHOLD and not _muted:
            print(f"[VOICE] Wakeword erkannt ({score:.2f})")
            try:
                _dialog(vad)
            except Exception as e:
                print(f"[VOICE] Fehler im Dialog: {e}")
            model.reset()
            _flush()


def start():
    """Beim Start von app.py einmal aufrufen."""
    if config.VOICE_ENABLED:
        threading.Thread(target=_loop, daemon=True).start()


def set_muted(value):
    """Fuer den Stummschalt-Knopf spaeter."""
    global _muted
    _muted = bool(value)
    print(f"[VOICE] Mikrofon {'stumm' if _muted else 'aktiv'}")


def is_muted():
    return _muted
