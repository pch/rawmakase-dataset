#!/bin/bash
# Classify every sample the database has no results for yet. Resumable.
cd "$(dirname "$0")"
export HF_HOME="$PWD/models/hf" RAWMAKASE_BIN="${RAWMAKASE_BIN:?set RAWMAKASE_BIN to a RAWmakase build}"
# setup.sh's .venv with requirements-describe.txt installed, or PYTHON=<another interpreter>.
PYTHON="${PYTHON:-.venv/bin/python}"
py() { "$PYTHON" -I scripts/dataset.py --dataset "$PWD" "$@"; }
set -e
py verify && py exif && py cameras && py previews && py stats --in-scope && py describe
echo "classify done"
