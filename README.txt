================================================================================
  YouTube Downloader — Telegram Bot (Render.com Deployment)
  README.txt
================================================================================

A Telegram bot that downloads YouTube videos and sends them directly to you in
Telegram. Deployable to Render.com in 3 steps — no server management needed.

--------------------------------------------------------------------------------
TABLE OF CONTENTS
--------------------------------------------------------------------------------
  1. How It Works
  2. Project Files Overview
  3. Deploy to Render.com (Quick Start)
  4. Local Development Setup
  5. Environment Variables Reference
  6. YouTube Cookies (Optional)
  7. Bot Commands
  8. API Endpoints
  9. Troubleshooting
 10. File Retention & Disk Notes

--------------------------------------------------------------------------------
1. HOW IT WORKS
--------------------------------------------------------------------------------

  User sends YouTube URL to the bot
       |
       v
  Bot fetches video info (title, size estimates)
       |
       v
  Bot shows format selection keyboard:
    [MP4 1080p] [MP4 720p]
    [MP4 480p]  [MP4 360p]
    [MP3 320]   [MP3 256]
    ...
       |
       v
  User taps a quality button
       |
       v
  Bot downloads the video/audio via yt-dlp
       |
       v
  Bot sends the file directly to Telegram
       |
       v
  File is deleted from disk (ephemeral FS friendly)

--------------------------------------------------------------------------------
2. PROJECT FILES OVERVIEW
--------------------------------------------------------------------------------

  CORE APPLICATION
  ─────────────────
  app.py
    Main Flask application. Contains:
      - CONFIG block (all values from environment variables)
      - Auto yt-dlp updater (checks PyPI at startup, upgrades if outdated,
        re-checks every 24 hours — same logic as update_yt_dlp.bat)
      - VideoDownloader class — handles yt-dlp downloads
      - TelegramBot class — sends messages, videos, audio to Telegram
      - Polling loop — listens for Telegram messages in background thread
      - Flask API routes — REST endpoints for manual control
      - _notify_when_done() — waits for download, sends file to Telegram,
        deletes file from disk

  requirements.txt
    Python dependencies:
      flask, flask-cors, yt-dlp, requests, werkzeug, gunicorn

  RENDER.COM DEPLOYMENT
  ──────────────────────
  render.yaml
    Infrastructure-as-Code for Render.com.
    Defines the web service, build command, start command, and environment
    variables. Render reads this file automatically when you connect your repo.

  build.sh
    Shell script that runs during every Render build:
      Step 1 — Install FFmpeg via apt-get (needed for MP3 audio extraction)
      Step 2 — Upgrade pip
      Step 3 — Install Python packages from requirements.txt
      Step 4 — Force install latest yt-dlp

  Procfile
    Fallback process definition. Starts gunicorn with:
      - 1 worker (fits free tier RAM)
      - 4 threads (handles concurrent downloads)
      - 120s timeout (long videos need time to upload to Telegram)

  .env.example
    Template showing all environment variables.
    Copy to .env for local development. Set in Render dashboard for production.

  .gitignore
    Prevents committing secrets, cookies, and downloaded files to GitHub.
    Key exclusions: .env, cookies.txt, cookies_env.txt, downloads/

  UTILITY SCRIPTS (LOCAL USE ONLY)
  ──────────────────────────────────
  update_yt_dlp.bat
    Windows batch script for manual yt-dlp and FFmpeg updates.
    The app.py auto-updater mirrors this logic in Python.

  run_server.bat / run_server.py
    Start the Flask server locally on port 8000.

  start_bot.bat
    One-click launcher for local development.

  export_cookies.py
    Exports Chrome browser cookies to cookies.txt format.
    Only needed locally — not used on Render.

  get_youtube_cookies.bat
    Helper to refresh cookies.txt from Chrome.

  get_chat_id.ps1
    PowerShell script to find your Telegram chat ID.

  test_telegram.py
    Verifies the bot token is valid and the server is reachable.

  test_server.py
    Runs basic API endpoint tests against a running local server.

  test_playlist_info.py
    Tests playlist info extraction.

  DOCUMENTATION
  ──────────────
  README.txt                  ← This file
  QUICK_START_TELEGRAM.md     ← Local quick start guide
  TELEGRAM_INTEGRATION_GUIDE.md
  COMPLETE_SETUP_GUIDE.md
  BOT_NOW_WORKING.md
  CHROME_EXTENSION_SETUP.md
  HOW_TO_RUN.md
  INTEGRATION_COMPLETE.md
  FINAL_STEPS.md
  README_LOCAL.md

  CHROME EXTENSION (separate folder)
  ────────────────────────────────────
  chrome extension/
    manifest.json   — Extension metadata and permissions
    popup.html      — Extension popup UI
    popup.js        — Syncs browser cookies to backend, triggers downloads

--------------------------------------------------------------------------------
3. DEPLOY TO RENDER.COM (QUICK START)
--------------------------------------------------------------------------------

  STEP 1 — Push to GitHub
  ────────────────────────
  Push this project folder to a GitHub (or GitLab) repository.

  IMPORTANT: Make sure .gitignore is present so you don't accidentally
  push your .env or cookies.txt files.

  STEP 2 — Create a Render Web Service
  ──────────────────────────────────────
  1. Go to https://dashboard.render.com
  2. Click "New" → "Web Service"
  3. Connect your GitHub repository
  4. Render auto-detects render.yaml — no manual setup needed

  STEP 3 — Set Your Bot Token
  ────────────────────────────
  In the Render dashboard:
    Environment tab → Add Environment Variable

    Key:   TELEGRAM_BOT_TOKEN
    Value: (your token from @BotFather on Telegram)

  Click "Save Changes" then "Deploy Latest Commit"

  That's it! Your bot will be live at:
    https://yt-telegram-bot.onrender.com

  Check it with: /health endpoint
  Test the bot: send /start in Telegram

--------------------------------------------------------------------------------
4. LOCAL DEVELOPMENT SETUP
--------------------------------------------------------------------------------

  Prerequisites:
    - Python 3.9+
    - pip
    - FFmpeg installed and in PATH (run update_yt_dlp.bat to install it)

  Step 1 — Install dependencies:
    pip install -r requirements.txt

  Step 2 — Set your bot token:
    Option A: Set environment variable
      Windows:  set TELEGRAM_BOT_TOKEN=your-token
      Linux:    export TELEGRAM_BOT_TOKEN=your-token

    Option B: Copy .env.example → .env and fill in your token

  Step 3 — Start the server:
    Double-click: start_bot.bat
    Or run:       python app.py

  Step 4 — Test the bot:
    python test_telegram.py

  Server runs at: http://localhost:8000

--------------------------------------------------------------------------------
5. ENVIRONMENT VARIABLES REFERENCE
--------------------------------------------------------------------------------

  Variable                Required   Default              Description
  ─────────────────────────────────────────────────────────────────────────────
  TELEGRAM_BOT_TOKEN      YES        (none)               Bot token from
                                                          @BotFather. Without
                                                          this the bot won't
                                                          work at all.

  SERVER_URL              No         http://localhost:8000 Public URL of the
                                                          server. Render sets
                                                          this automatically.

  YOUTUBE_COOKIES         No         (none)               Full contents of
                                                          cookies.txt. Needed
                                                          only for age-
                                                          restricted videos.

  DOWNLOADS_DIR           No         ./downloads          Where files are saved
                                                          temporarily.

  FILE_RETENTION_HOURS    No         2                    Hours to keep files
                                                          on disk before
                                                          cleanup. Default is
                                                          short because files
                                                          are sent immediately.

  CLEANUP_INTERVAL_HOURS  No         1                    How often the cleanup
                                                          thread runs.

  PYTHONUNBUFFERED        No         1                    Set to 1 for real-
                                                          time Render logs.

--------------------------------------------------------------------------------
6. YOUTUBE COOKIES (OPTIONAL)
--------------------------------------------------------------------------------

  Do you need cookies.txt on Render?  NO, not for most videos.

  cookies.txt is only needed for:
    - Age-restricted videos
    - Sign-in-required content
    - If YouTube starts blocking the server's IP

  How to use cookies on Render:
    1. Locally, open Chrome and visit youtube.com (while logged in)
    2. Run:  python export_cookies.py
    3. Open the generated cookies.txt and copy ALL its contents
    4. In Render dashboard → Environment → Add:
         Key:   YOUTUBE_COOKIES
         Value: (paste the full file contents)
    5. Redeploy

  The app checks cookies in this priority order:
    1. YOUTUBE_COOKIES env var  (Render / cloud)
    2. cookies.txt file on disk (local development)
    3. No cookies               (still works for most public videos)

--------------------------------------------------------------------------------
7. BOT COMMANDS
--------------------------------------------------------------------------------

  /start    — Welcome message and usage instructions
  /help     — Shows supported URL formats and features

  MAIN USAGE:
  Send any YouTube URL and the bot replies with a format selector:

    Video formats:
      MP4 1080p, 720p, 480p, 360p, 240p, 144p  (H.264)
      HEVC 720p, 480p                           (H.265, if available)

    Audio formats:
      MP3 320kbps, 256kbps, 192kbps, 128kbps

  Tap a button → bot downloads → bot sends file → file deleted from disk.

  SIZE LIMITS:
    Telegram limits file uploads to 50 MB.
    If the file is larger, the bot will warn you and suggest a lower quality.

--------------------------------------------------------------------------------
8. API ENDPOINTS
--------------------------------------------------------------------------------

  GET  /health
    Returns server health status.

  GET  /api/video_info/<video_id>
    Returns video title, duration, and estimated file sizes for all formats.

  POST /api/download_video
    Body: { "videoId": "...", "resolution": "720", "title": "...", "codec": "h264" }
    Starts a video download. Returns a requestId.

  POST /api/download_audio
    Body: { "videoId": "...", "quality": "192", "title": "..." }
    Starts an audio download. Returns a requestId.

  GET  /api/download_status/<request_id>
    Returns the current status of a download (pending/processing/complete/failed).

  GET  /api/status
    Returns all active download statuses.

  GET  /download/<filename>
    Serves a downloaded file (local use only).

  POST /api/delete_file
    Body: { "filename": "..." }
    Deletes a file from the downloads folder.

  POST /api/sync-cookies
    Body: { "cookies": "<netscape cookie string>" }
    Receives cookies from Chrome extension and saves to cookies.txt.

  GET  /api/telegram/bot_info
    Returns Telegram bot information.

  POST /api/telegram/send_message
    Body: { "chat_id": "...", "text": "..." }
    Sends a text message to a Telegram chat.

  POST /api/telegram/send_file
    Body: { "chat_id": "...", "filename": "...", "type": "video|audio|document" }
    Sends a downloaded file to a Telegram chat.

  POST /api/telegram/webhook
    Telegram webhook endpoint (not used in polling mode).

  POST /api/playlist_info
    Body: { "playlistId": "..." }
    Returns info about a YouTube playlist.

  POST /api/download_playlist
    Body: { "playlistId": "...", "resolution": "720", "title": "..." }
    Downloads an entire playlist.

--------------------------------------------------------------------------------
9. TROUBLESHOOTING
--------------------------------------------------------------------------------

  PROBLEM: Bot doesn't respond to messages
  FIX:     Check TELEGRAM_BOT_TOKEN is set correctly.
           Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getMe
           Should return your bot's info.

  PROBLEM: "Sign in to confirm you're not a bot" error
  FIX:     Set YOUTUBE_COOKIES env var with your cookies.txt content.
           See Section 6 above.

  PROBLEM: Audio downloads fail / no MP3 output
  FIX:     FFmpeg is not installed. On Render, build.sh handles this.
           Locally, run: update_yt_dlp.bat (installs FFmpeg via winget)

  PROBLEM: File too large for Telegram (>50 MB)
  FIX:     Choose a lower resolution or audio quality.
           1080p videos longer than ~10 minutes often exceed 50 MB.

  PROBLEM: yt-dlp errors / "HTTP Error 403" or format not found
  FIX:     yt-dlp may be outdated. The app auto-updates every 24h.
           Force update now: pip install --upgrade yt-dlp

  PROBLEM: Render service sleeps after 15 minutes (free tier)
  FIX:     Free tier services spin down when inactive.
           Use UptimeRobot (https://uptimerobot.com) to ping /health
           every 5 minutes to keep the service awake — it's free.

  PROBLEM: Server starts but bot doesn't listen
  FIX:     Check Render logs for "Telegram polling started".
           If missing, check that TELEGRAM_BOT_TOKEN is set.

--------------------------------------------------------------------------------
10. FILE RETENTION & DISK NOTES
--------------------------------------------------------------------------------

  On Render's free tier, the disk is EPHEMERAL:
    - Files in downloads/ are lost on every restart/redeploy
    - This is why the bot sends the file to Telegram immediately after
      download and then deletes it — no persistent storage needed

  Cleanup settings (configurable via env vars):
    FILE_RETENTION_HOURS = 2   (files older than 2h are deleted)
    CLEANUP_INTERVAL_HOURS = 1 (cleanup runs every 1 hour)

  The auto-updater also keeps yt-dlp current:
    - Checks PyPI for the latest version at startup (after 5s delay)
    - Upgrades via pip if a newer version is found
    - Re-checks every 24 hours
    - Mirrors the logic of update_yt_dlp.bat

================================================================================
  End of README
================================================================================
