#!/bin/zsh
set -euo pipefail

cd /Volumes/File/proj/qqbot
source .venv/bin/activate
exec /Volumes/File/proj/qqbot/.venv/bin/python src/qqbot_bridge.py
