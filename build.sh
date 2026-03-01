#!/bin/bash
set -e

echo "=== Installing ffmpeg ==="
apt-get update -qq
apt-get install -y ffmpeg

echo "=== Installing Python packages ==="
pip install -r requirements.txt

echo "=== Build complete ==="
