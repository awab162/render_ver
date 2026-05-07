#!/usr/bin/env bash
# =============================================================================
# build.sh — Render.com build script
# Runs automatically during every Render build.
# Installs FFmpeg (needed for MP3 audio extraction) and Python dependencies.
# =============================================================================
set -e  # exit immediately on any error

echo "========================================"
echo "  Build Step 1: Install FFmpeg"
echo "========================================"

# Render runs on Ubuntu — install FFmpeg via apt-get
if command -v ffmpeg &>/dev/null; then
    echo "[OK] FFmpeg already available: $(ffmpeg -version 2>&1 | head -1)"
else
    echo "[INFO] Installing FFmpeg..."
    apt-get update -qq
    apt-get install -y -qq ffmpeg
    echo "[OK] FFmpeg installed: $(ffmpeg -version 2>&1 | head -1)"
fi

echo ""
echo "========================================"
echo "  Build Step 2: Upgrade pip"
echo "========================================"
pip install --upgrade pip --quiet

echo ""
echo "========================================"
echo "  Build Step 3: Install Python packages"
echo "========================================"
pip install -r requirements.txt --quiet

echo ""
echo "========================================"
echo "  Build Step 4: Force latest yt-dlp"
echo "========================================"
# Always install the latest yt-dlp at build time.
# The app also auto-updates it at runtime every 24h.
pip install --upgrade yt-dlp --quiet
echo "[OK] yt-dlp version: $(python -m yt_dlp --version)"

echo ""
echo "========================================"
echo "  Build complete!"
echo "========================================"
