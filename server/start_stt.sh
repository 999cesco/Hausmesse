#!/bin/bash
# Startet die Spracherkennung auf dem Linux-Server mit NVIDIA-Grafikkarte.
# Die CUDA-Bibliotheken kommen aus pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)
# und muessen Whisper ueber LD_LIBRARY_PATH bekannt gemacht werden.

cd "$(dirname "$0")"
source ~/hausmesse-venv/bin/activate

export LD_LIBRARY_PATH=$(python3 -c 'import os, nvidia.cublas.lib, nvidia.cudnn.lib; print(os.path.dirname(nvidia.cublas.lib.__file__) + ":" + os.path.dirname(nvidia.cudnn.lib.__file__))')

export STT_DEVICE=cuda
export STT_COMPUTE=float16
export STT_MODEL=${STT_MODEL:-large-v3-turbo}
export STT_HOST=${STT_HOST:-127.0.0.1}   # Standard: nur lokal (HTTPS ueber Caddy). Bei VPN: Tailscale-IP per Dienst-Einstellung

exec python3 stt_server.py
