#!/bin/bash
# Startet die Spracherkennung auf dem Linux-Server mit NVIDIA-Grafikkarte.
# Die CUDA-Bibliotheken kommen aus pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)
# und muessen Whisper ueber LD_LIBRARY_PATH bekannt gemacht werden.

cd "$(dirname "$0")"
source ~/hausmesse-venv/bin/activate

# Ordner der CUDA-Bibliotheken (neuere pip-Pakete haben kein __file__, deshalb __path__)
export LD_LIBRARY_PATH=$(python3 -c 'import nvidia.cublas.lib as a, nvidia.cudnn.lib as b; print(list(a.__path__)[0] + ":" + list(b.__path__)[0])'):$LD_LIBRARY_PATH

export STT_DEVICE=${STT_DEVICE:-cuda}
export STT_COMPUTE=${STT_COMPUTE:-auto}    # auto: schnellste Rechenart, die die Grafikkarte kann
export STT_MODEL=${STT_MODEL:-large-v3-turbo}
export STT_HOST=${STT_HOST:-127.0.0.1}     # auf dem Server per Dienst-Einstellung auf die VPN-Adresse setzen

exec python3 stt_server.py
