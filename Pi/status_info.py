"""
Sammelt den Zustand aller Teile fuer den Admin-Bereich:
Raspberry Pi, ESP, KI-Server (Ollama), Spracherkennung, Sprachausgabe, Sprachsteuerung.
"""

import os
import platform
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config
import tts
import voice

STARTED = time.time()


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _ips():
    """Alle IPv4-Adressen des Pi, z. B. {'wlan0': '192.168.0.106', 'tailscale0': '100.x.y.z'}"""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    ips = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[1] != "lo":
            ips[parts[1]] = parts[3].split("/")[0]
    return ips


def _throttled():
    """Zeigt Unterspannung oder Ueberhitzung an (typisches Pi-Problem bei schwachem Netzteil)."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
        value = int(out.strip().split("=")[1], 16)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None
    problems = []
    if value & 0x1:
        problems.append("Unterspannung jetzt")
    elif value & 0x10000:
        problems.append("Unterspannung seit dem Start")
    if value & 0x4:
        problems.append("Gedrosselt wegen Hitze")
    elif value & 0x40000:
        problems.append("War wegen Hitze gedrosselt")
    return problems


def pi():
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        mem[key] = int(rest.split()[0]) if rest.split() else 0
    temp = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    disk = shutil.disk_usage("/")
    up = _read("/proc/uptime").split()
    return {
        "name": socket.gethostname(),
        "ips": _ips(),
        "temperatur": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "last": round(os.getloadavg()[0], 2),
        "kerne": os.cpu_count(),
        "ram_mb": round(mem.get("MemTotal", 0) / 1024),
        "ram_frei_mb": round(mem.get("MemAvailable", 0) / 1024),
        "speicher_gb": round(disk.total / 1e9, 1),
        "speicher_frei_gb": round(disk.free / 1e9, 1),
        "laufzeit_s": int(float(up[0])) if up else None,
        "app_laufzeit_s": int(time.time() - STARTED),
        "python": platform.python_version(),
        "probleme": _throttled(),
    }


def _timed_get(url, **kwargs):
    start = time.time()
    r = requests.get(url, timeout=4, **kwargs)
    r.raise_for_status()
    return r.json(), round((time.time() - start) * 1000)


def esp():
    try:
        data, ms = _timed_get(f"http://{config.ESP_IP}/api/state")
    except (requests.RequestException, ValueError) as e:
        return {"online": False, "adresse": config.ESP_IP, "fehler": type(e).__name__}
    return {"online": True, "adresse": config.ESP_IP, "antwortzeit_ms": ms,
            "geraet": data.get("deviceId"), "laufzeit_s": int(data.get("uptimeMs", 0) / 1000),
            "wlan_dbm": data.get("rssi"), "sensoren": data.get("sensors", {})}


def ollama():
    base = config.OLLAMA_URL.rsplit("/api/", 1)[0]
    try:
        data, ms = _timed_get(base + "/api/tags", **config.server_args())
    except (requests.RequestException, ValueError) as e:
        return {"online": False, "adresse": base, "fehler": type(e).__name__}
    models = [m.get("name") for m in data.get("models", [])]
    wanted = config.MODEL if ":" in config.MODEL else config.MODEL + ":latest"
    return {"online": True, "adresse": base, "antwortzeit_ms": ms, "modelle": models,
            "modell": config.MODEL, "modell_vorhanden": wanted in models}


def stt():
    if config.STT_MODE == "local":
        return {"online": True, "adresse": "auf dem Pi", "modell": config.STT_LOCAL_MODEL, "geraet": "cpu"}
    base = config.STT_URL.rsplit("/", 1)[0] + "/"
    try:
        data, ms = _timed_get(base, **config.server_args())
    except (requests.RequestException, ValueError) as e:
        return {"online": False, "adresse": base, "fehler": type(e).__name__}
    return {"online": True, "adresse": base, "antwortzeit_ms": ms,
            "modell": data.get("modell"), "geraet": data.get("geraet")}


def all_status():
    # Gleichzeitig abfragen: ist ein Teil offline, wartet man nicht mehrmals auf den Timeout
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {name: pool.submit(fn) for name, fn in
                (("pi", pi), ("esp", esp), ("ollama", ollama), ("stt", stt))}
        results = {name: job.result() for name, job in jobs.items()}
    return {
        **results,
        "tts": {"aktiv": config.TTS_ENABLED, "bereit": tts.is_ready()},
        "sprache": {"aktiv": config.VOICE_ENABLED, "status": voice.status, "stumm": voice.is_muted(),
                    "wakeword": config.WAKEWORD, **voice.last},
    }
