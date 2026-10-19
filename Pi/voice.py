"""
Sprachassistent: Wakeword -> Aufnahme -> Spracherkennung -> KI -> Sprachausgabe.

Ablauf:
  1. Das Mikro laeuft dauerhaft und hoert auf das Wakeword - entweder mit openWakeWord
     (fertig oder selbst trainiert) oder mit Picovoice Porcupine (eigenes deutsches Wakeword).
  2. Erkannt -> kurzer Signalton, Aufnahme startet.
  3. webrtcvad erkennt, wann du aufhoerst zu sprechen -> Aufnahme endet (zweiter Ton).
  4. Text an die KI (ki.handle), Antwort wird vorgelesen.
  5. Stellt die KI eine echte Rueckfrage ("Soll ich das Garagentor schliessen?"),
     hoert der Assistent direkt weiter zu - ohne erneutes Wakeword (hoechstens 2x).

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

def _rms(chunk):
    a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a)))


_noise = 200.0   # Lautstaerke des Hintergrundrauschens, wird laufend gemessen


def _update_noise(chunk):
    """Lernt im Leerlauf, wie laut der Raum ohne Sprache ist."""
    global _noise
    level = _rms(chunk)
    if level < _noise * 3:                     # laute Ausreisser (Sprache) nicht einrechnen
        _noise = 0.95 * _noise + 0.05 * level


def _is_speech(vad, chunk):
    """Sprache nur, wenn webrtcvad sie erkennt UND es deutlich lauter ist als das Rauschen."""
    if _rms(chunk) < max(_noise * getattr(config, "SPEECH_FACTOR", 2.5), 120):
        return False
    voiced = sum(vad.is_speech(chunk[i:i + VAD_FRAME * 2], SAMPLE_RATE)
                 for i in range(0, len(chunk), VAD_FRAME * 2))
    return voiced >= 3                         # mind. 60 von 80 ms klingen nach Sprache


def _record_command(vad):
    """Nimmt auf, bis nach dem Sprechen eine kurze Pause kommt. Gibt PCM oder None zurueck."""
    frames = []
    speech_seen = False
    silence_ms = 0
    waited_ms = 0
    while True:
        chunk = _audio.get()
        frames.append(chunk)
        if _is_speech(vad, chunk):
            speech_seen = True
            silence_ms = 0
        else:
            silence_ms += CHUNK_MS
        waited_ms += CHUNK_MS

        if not speech_seen and waited_ms >= config.LISTEN_TIMEOUT_MS:
            return None                        # niemand hat etwas gesagt
        if speech_seen and silence_ms >= config.SILENCE_END_MS:
            print(f"[VOICE] Aufnahme {waited_ms / 1000:.1f} s (Pause erkannt, Rauschpegel {_noise:.0f})")
            return b"".join(frames)
        if waited_ms >= config.MAX_RECORD_MS:
            print(f"[VOICE] Aufnahme abgebrochen nach {waited_ms / 1000:.1f} s - Raum zu laut? "
                  f"Rauschpegel {_noise:.0f}, SPEECH_FACTOR erhoehen")
            return b"".join(frames)


# ---------- Hauptschleife ----------

def _dialog(vad):
    """Ein Gespraechsdurchgang inkl. Rueckfragen."""
    global status
    for _ in range(1 + getattr(config, "MAX_FOLLOWUPS", 2)):
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
        time.sleep(0.4)                        # Nachhall des Lautsprechers abwarten

        # Nur bei einer echten Rueckfrage ohne Wakeword weiter zuhoeren
        if not result.get("rueckfrage"):
            return


# ---------- Wakeword-Erkennung ----------

class _OpenWakeWord:
    """openWakeWord: fertige Wakewords ("hey_jarvis") oder ein selbst trainiertes Modell (.onnx)."""

    def __init__(self):
        from openwakeword.model import Model
        ww = config.WAKEWORD
        if not ww.endswith(".onnx"):           # fertiges Modell -> bei Bedarf herunterladen
            try:
                import openwakeword.utils
                openwakeword.utils.download_models([ww])
            except Exception as e:
                print(f"[VOICE] Wakeword-Modell konnte nicht geladen werden: {e}")
        self.model = Model(wakeword_models=[ww], inference_framework="onnx")
        self.name = ww.rsplit("/", 1)[-1].removesuffix(".onnx")

    def detect(self, chunk):
        score = max(self.model.predict(np.frombuffer(chunk, dtype=np.int16)).values())
        return score >= config.WAKEWORD_THRESHOLD, score

    def reset(self):
        self.model.reset()


class _Porcupine:
    """Picovoice Porcupine: eigenes Wakeword (auch auf Deutsch), in der Picovoice Console erstellt."""

    def __init__(self):
        import pvporcupine
        from geheim import PICOVOICE_KEY       # Schluessel steht nicht im oeffentlichen Repo
        self.handle = pvporcupine.create(
            access_key=PICOVOICE_KEY,
            keyword_paths=[config.PORCUPINE_KEYWORD],
            model_path=config.PORCUPINE_MODEL or None,     # deutsches Modell fuer deutsche Wakewords
            sensitivities=[config.PORCUPINE_SENSITIVITY])
        self.frame = self.handle.frame_length              # 512 Samples
        self.buf = np.zeros(0, dtype=np.int16)
        self.name = config.PORCUPINE_KEYWORD.rsplit("/", 1)[-1].split("_")[0]

    def detect(self, chunk):
        self.buf = np.concatenate([self.buf, np.frombuffer(chunk, dtype=np.int16)])
        found = False
        while len(self.buf) >= self.frame:
            if self.handle.process(self.buf[:self.frame].tolist()) >= 0:
                found = True
            self.buf = self.buf[self.frame:]
        return found, 1.0 if found else 0.0

    def reset(self):
        self.buf = np.zeros(0, dtype=np.int16)


def _make_detector():
    if getattr(config, "WAKEWORD_ENGINE", "openwakeword") == "porcupine":
        return _Porcupine()
    return _OpenWakeWord()


def _loop():
    global status
    import webrtcvad

    stt.load()
    try:
        detector = _make_detector()
    except Exception as e:
        status = "aus"
        print(f"[VOICE] Wakeword-Erkennung konnte nicht starten: {e}")
        return
    vad = webrtcvad.Vad(config.VAD_LEVEL)
    threading.Thread(target=_mic_reader, daemon=True).start()
    print(f"[VOICE] Bereit - sag '{detector.name}'")
    quiet_until = 0

    while True:
        status = "stumm" if _muted else ("spricht" if tts.is_busy() else "bereit")
        if _muted or tts.is_busy():            # stumm oder Pi spricht gerade
            _flush()
            detector.reset()
            time.sleep(0.1)
            quiet_until = time.time() + 0.6    # kurz nach dem Sprechen nicht auf sich selbst reagieren
            continue
        if time.time() < quiet_until:
            _flush()
            time.sleep(0.05)
            continue

        chunk = _audio.get()
        _update_noise(chunk)
        found, score = detector.detect(chunk)
        if found and not _muted:
            print(f"[VOICE] Wakeword erkannt ({score:.2f})")
            try:
                _dialog(vad)
            except Exception as e:
                print(f"[VOICE] Fehler im Dialog: {e}")
            detector.reset()
            _flush()
            quiet_until = time.time() + 0.6


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
