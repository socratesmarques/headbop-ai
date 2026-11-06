#!/bin/sh
set -eu
cd /workspace/headbop-ai
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip check
