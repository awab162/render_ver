import os
import uuid
import threading
import time
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
import yt_dlp
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# ---------------------------------------------------------------------------
# Configuration — all sensitive values come from environment variables.
# Set these in your Render dashboard (or a local .env file).
#
#   TELEGRAM_BOT_TOKEN  — required. Get from @BotFather on Telegram.
#   SERVER_URL          — optional. Defaults to http://localhost:8000.
#                         On Render this is auto-set to your public URL.
#   YOUTUBE_COOKIES     — optional. Paste the full contents of cookies.txt
#                         here if you need to download age-restricted videos.
# ---------------------------------------------------------------------------
CONFIG = {
    'DOWNLOADS_DIR': os.environ.get('DOWNLOADS_DIR', './downloads'),
    'CLEANUP_INTERVAL_HOURS': int(os.environ.get('CLEANUP_INTERVAL_HOURS', '24')),
    'FILE_RETENTION_HOURS': int(os.environ.get('FILE_RETENTION_HOURS', '2')),  # short on ephemeral FS
    'LOCAL_SERVER_URL': os.environ.get('SERVER_URL', 'http://localhost:8000'),
    'TELEGRAM_BOT_TOKEN': os.environ.get('TELEGRAM_BOT_TOKEN', ''),
    'TELEGRAM_API_URL': 'https://api.telegram.org/bot',
    # Optional: raw Netscape-format cookie string to bypass bot detection
    'YOUTUBE_COOKIES_ENV': os.environ.get('YOUTUBE_COOKIES', ''),
}

if not CONFIG['TELEGRAM_BOT_TOKEN']:
    print("WARNING: TELEGRAM_BOT_TOKEN environment variable is not set! Bot will not work.")

download_status = {}
download_lock = threading.Lock()
final_filenames_store = {}


# ---------------------------------------------------------------------------
# Auto-updater — mirrors the logic in update_yt_dlp.bat.
# Runs once at startup in a background thread so it never blocks the server.
# If yt-dlp is outdated (a common cause of download failures on YouTube),
# it is automatically upgraded via pip.
# ---------------------------------------------------------------------------
def _auto_update_yt_dlp():
    """Check if yt-dlp is up to date and upgrade it if necessary."""
    try:
        # Get currently installed version
        old_version = yt_dlp.version.__version__
        print(f"[yt-dlp updater] Current version: {old_version}")

        # Ask PyPI for the latest released version
        try:
            resp = requests.get('https://pypi.org/pypi/yt-dlp/json', timeout=10)
            latest_version = resp.json()['info']['version']
        except Exception as e:
            print(f"[yt-dlp updater] Could not reach PyPI: {e}. Skipping update check.")
            return

        if old_version == latest_version:
            print(f"[yt-dlp updater] Already up to date ({old_version}). ✓")
            return

        print(f"[yt-dlp updater] New version available: {latest_version}. Upgrading…")
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--upgrade', '--quiet', 'yt-dlp'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            # Reload the module so the running process uses the new version
            import importlib
            importlib.reload(yt_dlp)
            new_version = yt_dlp.version.__version__
            print(f"[yt-dlp updater] ✅ Upgraded yt-dlp: {old_version} → {new_version}")
        else:
            print(f"[yt-dlp updater] ❌ pip upgrade failed:\n{result.stderr}")
    except Exception as e:
        print(f"[yt-dlp updater] Unexpected error: {e}")


def _auto_update_loop():
    """Run the updater once at startup, then every 24 h to keep yt-dlp fresh."""
    # Small delay so gunicorn workers are fully initialised first
    time.sleep(5)
    while True:
        _auto_update_yt_dlp()
        time.sleep(24 * 3600)  # re-check every 24 hours


_updater_thread = threading.Thread(target=_auto_update_loop, daemon=True)
_updater_thread.start()
print("[yt-dlp updater] Background update checker started.")

class VideoDownloader:
    def __init__(self):
        self.downloads_dir = Path(CONFIG['DOWNLOADS_DIR'])
        self.downloads_dir.mkdir(exist_ok=True)
        self.final_filenames = final_filenames_store
        
        cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        cleanup_thread.start()

    def _get_cookie_opts(self):
        """Return yt-dlp cookie options to bypass YouTube bot detection.

        Priority order:
        1. YOUTUBE_COOKIES env var  — paste your cookies.txt content here on Render.
        2. cookies.txt file on disk — generated locally by the Chrome extension.
        3. No cookies              — works fine for most public videos.
        """
        opts = {}

        # 1. Env-var cookies (Render / cloud deployments)
        cookies_env = CONFIG.get('YOUTUBE_COOKIES_ENV', '')
        if cookies_env and len(cookies_env.strip()) > 10:
            # Write to a temp file because yt-dlp expects a file path
            tmp_cookies = Path(__file__).parent / 'cookies_env.txt'
            try:
                tmp_cookies.write_text(cookies_env, encoding='utf-8')
                opts['cookiefile'] = str(tmp_cookies)
                print("COOKIES: Using YOUTUBE_COOKIES environment variable.")
                return opts
            except Exception as e:
                print(f"COOKIES: Could not write env cookies to file: {e}")

        # 2. Local cookies.txt file (Chrome extension sync)
        cookies_file = Path(__file__).parent / 'cookies.txt'
        if cookies_file.exists() and cookies_file.stat().st_size > 10:
            opts['cookiefile'] = str(cookies_file)
            print("COOKIES: Using local cookies.txt file.")
            return opts

        # 3. No cookies — still works for most public YouTube videos
        print("COOKIES: No cookies found — proceeding without authentication.")
        return opts

    def _cleanup_loop(self):
        while True:
            try:
                self._cleanup_old_files()
                self._cleanup_old_status()
                time.sleep(CONFIG['CLEANUP_INTERVAL_HOURS'] * 3600)
            except Exception as e:
                print(f"Cleanup error: {e}")
                time.sleep(3600)

    def _cleanup_old_files(self):
        cutoff = datetime.now() - timedelta(hours=CONFIG['FILE_RETENTION_HOURS'])
        cleaned_count = 0
        for file_path in self.downloads_dir.glob('*'):
            if file_path.is_file():
                try:
                    file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if file_time < cutoff:
                        file_path.unlink()
                        cleaned_count += 1
                except Exception as e:
                    print(f"Error cleaning up file {file_path}: {e}")
        if cleaned_count > 0:
            print(f"Cleaned up {cleaned_count} old file(s).")

    def _cleanup_old_status(self):
        cutoff = datetime.now() - timedelta(hours=CONFIG['FILE_RETENTION_HOURS'])
        cleaned_count = 0
        with download_lock:
            to_remove = [
                request_id for request_id, status_data in download_status.items()
                if datetime.fromisoformat(status_data.get('created_at', '1970-01-01T00:00:00')) < cutoff
            ]
            for request_id in to_remove:
                download_status.pop(request_id, None)
                cleaned_count +=1
        if cleaned_count > 0:
            print(f"Cleaned up {cleaned_count} old status entries.")

    def _get_random_user_agent(self):
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0'
        ]
        return random.choice(user_agents)

    def _calculate_dynamic_timeouts(self, duration):
        """Calculate dynamic timeouts based on video duration"""
        base_timeout = 60  # 1 minute base
        
        # Scale timeout based on duration
        if duration <= 300:  # 5 minutes or less
            info_timeout = base_timeout
            sim_timeout = 90
        elif duration <= 900:  # 15 minutes or less
            info_timeout = base_timeout * 1.5
            sim_timeout = 120
        elif duration <= 1800:  # 30 minutes or less
            info_timeout = base_timeout * 2
            sim_timeout = 180
        elif duration <= 3600:  # 1 hour or less
            info_timeout = base_timeout * 2.5
            sim_timeout = 240
        elif duration <= 7200:  # 2 hours or less
            info_timeout = base_timeout * 3
            sim_timeout = 300
        else:  # Very long videos
            info_timeout = base_timeout * 4
            sim_timeout = 360  # 6 minutes for very long videos
        
        return int(info_timeout), int(sim_timeout)

    def _improved_estimation(self, resolution, duration):
        """Much more accurate estimation with better resolution differentiation"""
        # More realistic bitrates with better resolution differentiation
        base_bitrates = {
            144: 200,
            240: 300,
            360: 450,   # Slightly increased to create more separation
            480: 850,   # Bigger jump from 360p
            720: 1300,  # More realistic for 720p
            1080: 2800  # More realistic for 1080p
        }
        
        # Duration-based minimal scaling
        duration_factor = 1.0
        if duration > 1800:  # 30+ minutes
            duration_factor = 0.98
        elif duration > 3600:  # 1+ hour
            duration_factor = 0.96
        elif duration > 7200:  # 2+ hours
            duration_factor = 0.94
        
        # More realistic compression efficiency
        compression_factor = 0.90
        
        effective_bitrate = base_bitrates[resolution] * duration_factor * compression_factor
        estimated_size = (effective_bitrate * duration * 1000) / 8
        
        return {'filesize': int(estimated_size), 'estimated': True}

    def _improved_audio_estimation(self, quality, duration):
        """More accurate audio size estimation"""
        # More realistic audio efficiency factors
        efficiency_factors = {128: 0.88, 192: 0.90, 256: 0.92, 320: 0.94}
        
        duration_factor = 1.0
        if duration > 1800:  # 30+ minutes
            duration_factor = 0.98
        
        effective_bitrate = quality * efficiency_factors[quality] * duration_factor
        estimated_size = (effective_bitrate * duration * 1000) / 8
        
        return {'filesize': int(estimated_size), 'estimated': True}

    def _get_video_format_spec(self, resolution):
        """Get format specification for size simulation. 
        NOTE: No ultimate 'best' fallback here — if the specific resolution isn't found,
        the caller falls back to _improved_estimation() for proper per-resolution estimates.
        """
        if resolution == 480:
            return (
                f'bestvideo[height=480][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
                f'bestvideo[height<=480][height>360][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
                f'bestvideo[height=480][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<=480][height>360][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<=480][vcodec^=avc1]+bestaudio/'
                f'bestvideo[height<=480]+bestaudio/'
                f'best[height=480][ext=mp4]/'
                f'best[height<=480][height>360]'
            )
        elif resolution == 360:
            return (
                f'bestvideo[height=360][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
                f'bestvideo[height<=360][height>240][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
                f'bestvideo[height=360][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<=360][height>240][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<=360][vcodec^=avc1]+bestaudio/'
                f'bestvideo[height<=360]+bestaudio/'
                f'best[height=360][ext=mp4]/'
                f'best[height<=360][height>240]'
            )
        else:
            return (
                f'bestvideo[height<={resolution}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={resolution}][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={resolution}][vcodec^=avc1]+bestaudio/'
                f'bestvideo[height<={resolution}]+bestaudio/'
                f'bestvideo[height<={resolution}][ext=webm]+bestaudio[ext=opus]/'
                f'bestvideo[height<={resolution}][ext=webm]+bestaudio/'
                f'best[height<={resolution}][ext=mp4]/'
                f'best[height<={resolution}]'
            )

    def _calculate_total_filesize(self, sim_info):
        """Calculate total filesize from simulation info"""
        total_filesize = 0
        
        # Check for combined formats first (video + audio)
        requested_formats = sim_info.get('requested_formats')
        if requested_formats:
            for fmt_req in requested_formats:
                size = fmt_req.get('filesize') or fmt_req.get('filesize_approx')
                if size:
                    total_filesize += size
        else:
            # Single format
            total_filesize = sim_info.get('filesize') or sim_info.get('filesize_approx') or 0
        
        return total_filesize

    def _detect_and_handle_duplicate_sizes(self, video_formats):
        """Detect when resolutions have the same size and adjust accordingly"""
        sizes = {}
        duplicates = []
        
        # Group resolutions by file size
        for resolution, format_info in video_formats.items():
            size = format_info.get('filesize', 0)
            if size in sizes:
                sizes[size].append(resolution)
            else:
                sizes[size] = [resolution]
        
        # Find duplicates
        for size, resolutions in sizes.items():
            if len(resolutions) > 1:
                duplicates.extend(resolutions)
        
        # If 480p and 360p have the same size, apply intelligent adjustment
        if 480 in duplicates and 360 in duplicates:
            if video_formats[480]['filesize'] == video_formats[360]['filesize']:
                # Apply a small realistic difference (480p typically 10-20% larger)
                base_size = video_formats[360]['filesize']
                video_formats[480]['filesize'] = int(base_size * 1.15)  # 15% larger
                video_formats[480]['adjusted'] = True
                print(f"VID_INFO: Adjusted 480p size from {base_size} to {video_formats[480]['filesize']} (15% increase)")
        
        return video_formats

    def _aggressive_size_simulation(self, url, duration, info):
        """Fast parallel simulation with reduced retries"""
        try:
            # Drastically reduced timeouts for speed
            info_timeout = 20
            sim_timeout = 30
            
            print(f"VID_INFO: Using fast timeouts - Info: {info_timeout}s, Simulation: {sim_timeout}s")
            
            video_formats_out = {}
            audio_formats_out = {}

            base_sim_opts = {
                'quiet': True,
                'no_warnings': True,
                'simulate': True,
                'skip_download': True,
                'socket_timeout': sim_timeout,
                'http_headers': {
                    'User-Agent': self._get_random_user_agent(),
                    'Accept-Language': 'en-US,en;q=0.5',
                },
                'extract_flat': False,
                'no_check_certificate': True,
                'retries': 2, # Reduced from 10
                'fragment_retries': 2,
                **self._get_cookie_opts(),
            }

            def check_video_size(resolution):
                try:
                    format_spec = self._get_video_format_spec(resolution)
                    current_sim_opts = {**base_sim_opts, 'format': format_spec}
                    
                    with yt_dlp.YoutubeDL(current_sim_opts) as sim_ydl:
                        sim_info = sim_ydl.extract_info(url, download=False)
                    
                    total_filesize = self._calculate_total_filesize(sim_info)
                    if total_filesize > 0:
                        return resolution, {'filesize': int(total_filesize), 'estimated': False}, None
                except Exception as e:
                    pass
                # Fallback to estimation
                return resolution, self._improved_estimation(resolution, duration), None

            def check_audio_size(quality_kbps):
                try:
                    format_spec = f'bestaudio[abr<={quality_kbps}][ext=m4a]/bestaudio[abr<={quality_kbps}]/bestaudio[ext=m4a]/bestaudio'
                    current_sim_opts = {**base_sim_opts, 'format': format_spec}
                    
                    with yt_dlp.YoutubeDL(current_sim_opts) as sim_ydl:
                        sim_info = sim_ydl.extract_info(url, download=False)
                    
                    filesize = sim_info.get('filesize') or sim_info.get('filesize_approx')
                    if filesize and filesize > 0:
                         return quality_kbps, {'filesize': int(filesize), 'estimated': False}, None
                except Exception:
                    pass
                return quality_kbps, self._improved_audio_estimation(quality_kbps, duration), None

            # Execute all checks in parallel
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                video_futures = [executor.submit(check_video_size, res) for res in [720, 1080, 480, 360]]
                audio_futures = [executor.submit(check_audio_size, q) for q in [128, 192, 256, 320]]
                
                for future in concurrent.futures.as_completed(video_futures + audio_futures):
                    try:
                        res_or_qual, result, _ = future.result()
                        if res_or_qual in [720, 1080, 480, 360]:
                            video_formats_out[res_or_qual] = result
                        else:
                            audio_formats_out[res_or_qual] = result
                    except Exception as e:
                        print(f"VID_INFO: Parallel determination error: {e}")

            # Fill missing with estimation (should ideally be covered by fallback in helper)
            for res in [720, 1080, 480, 360]:
                if res not in video_formats_out:
                    video_formats_out[res] = self._improved_estimation(res, duration)
            for q in [128, 192, 256, 320]:
                if q not in audio_formats_out:
                    audio_formats_out[q] = self._improved_audio_estimation(q, duration)

            # Detect and handle duplicate sizes
            video_formats_out = self._detect_and_handle_duplicate_sizes(video_formats_out)

            print(f"VID_INFO: Fast parallel simulation completed")
            
            return {
                'success': True,
                'title': info.get('title', 'Unknown Title'),
                'duration': duration,
                'video_formats': video_formats_out,
                'audio_formats': audio_formats_out,
                'thumbnail': info.get('thumbnail'),
                'estimated_only': False,
                'actual_sizes_count': {'video': 4, 'audio': 4}, # Assuming successful or fallback
                'format_debug': None
            }

        except Exception as e:
            print(f"VID_INFO: Parallel simulation failed: {e}")
            return None

    def get_video_info(self, video_id):
        try:
            url = f'https://www.youtube.com/watch?v={video_id}'
            
            # Start with extended timeout for initial info
            base_ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False, 
                'skip_download': True,
                'no_check_certificate': True,
                'socket_timeout': 120,  # 2 minutes for initial info
                'http_headers': {
                    'User-Agent': self._get_random_user_agent(),
                    'Accept-Language': 'en-US,en;q=0.5',
                },
                'retries': 5,
                'fragment_retries': 5,
                **self._get_cookie_opts(),
            }
            
            print(f"VID_INFO: Getting initial info for {video_id} with extended timeout...")
            start_time = time.time()
            
            with yt_dlp.YoutubeDL(base_ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False) 
            
            duration = info.get('duration', 0)
            initial_time = time.time() - start_time
            print(f"VID_INFO: Initial info for {video_id} (duration: {duration}s) fetched in {initial_time:.2f}s")

            if not duration:
                print(f"VID_INFO: Could not determine duration for {video_id}. Using improved estimations.")
                # Use improved estimation with default 10-minute duration
                default_duration = 600
                video_formats = {res: self._improved_estimation(res, default_duration) for res in [360, 480, 720, 1080]}
                audio_formats = {qual: self._improved_audio_estimation(qual, default_duration) for qual in [128, 192, 256, 320]}
                
                return {
                    'success': True,
                    'title': info.get('title', 'Unknown Title'),
                    'duration': default_duration,
                    'video_formats': video_formats,
                    'audio_formats': audio_formats,
                    'thumbnail': info.get('thumbnail'),
                    'estimated_only': True,
                    'message': 'Could not determine duration, using improved estimation'
                }

            # No duration limit - try aggressive simulation for ALL videos
            print(f"VID_INFO: Attempting aggressive simulation for {video_id} ({duration}s duration)...")
            actual_sizes = self._aggressive_size_simulation(url, duration, info)
            
            if actual_sizes:
                total_time = time.time() - start_time
                print(f"VID_INFO: Aggressive simulation completed for {video_id} in {total_time:.2f}s total")
                actual_sizes['processing_time_seconds'] = round(total_time, 2)
                return actual_sizes
            
            # If even aggressive simulation fails, use improved estimation
            print(f"VID_INFO: Aggressive simulation failed, using improved estimation for {video_id}")
            video_formats = {res: self._improved_estimation(res, duration) for res in [360, 480, 720, 1080]}
            audio_formats = {qual: self._improved_audio_estimation(qual, duration) for qual in [128, 192, 256, 320]}
            
            fallback_result = {
                'success': True,
                'title': info.get('title', 'Unknown Title'),
                'duration': duration,
                'video_formats': video_formats,
                'audio_formats': audio_formats,
                'thumbnail': info.get('thumbnail'),
                'estimated_only': True,
                'message': 'Aggressive simulation failed, using improved estimation'
            }
            fallback_result['processing_time_seconds'] = round(time.time() - start_time, 2)
            return fallback_result

        except Exception as e_main:
            print(f"VID_INFO: Major error getting video info for {video_id}: {e_main}")
            # Fallback response with improved estimation
            video_formats = {res: self._improved_estimation(res, 600) for res in [360, 480, 720, 1080]}
            audio_formats = {qual: self._improved_audio_estimation(qual, 600) for qual in [128, 192, 256, 320]}
            
            return {
                'success': True,
                'title': f'Video {video_id}',
                'duration': 600,
                'video_formats': video_formats,
                'audio_formats': audio_formats,
                'thumbnail': None,
                'estimated_only': True,
                'message': f'Error occurred: {str(e_main)[:50]}...'
            }

    def start_audio_download(self, video_id, quality, title):
        request_id = str(uuid.uuid4())
        with download_lock:
            download_status[request_id] = {
                'status': 'pending',
                'message': 'Audio download request received',
                'created_at': datetime.now().isoformat(),
                'video_id': video_id,
                'quality': quality,
                'title': title,
                'type': 'audio',
                'download_url': None,
                'file_size_mb': None,
                'updated_at': datetime.now().isoformat(),
            }
        
        download_thread = threading.Thread(
            target=self._download_audio,
            args=(request_id, video_id, quality, title),
            daemon=True
        )
        download_thread.start()
        return request_id

    def _download_audio(self, request_id, video_id, quality, title):
        final_downloaded_file_path = None
        progress_hook_key = f"{request_id}_progress_{str(uuid.uuid4())[:8]}"

        try:
            self._update_status(request_id, 'processing', 'Initializing audio download...')

            safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip().replace(" ", "_")
            safe_title = safe_title[:60]
            
            filename_template_str = f"{safe_title}_{video_id}_{quality}kbps.%(ext)s"
            output_template_path = self.downloads_dir / filename_template_str
            
            url = f'https://www.youtube.com/watch?v={video_id}'
            
            ydl_opts = {
                # Simple & permissive: grab ANY audio stream, FFmpeg converts to MP3
                'format': 'bestaudio/best',
                'outtmpl': str(output_template_path),
                'noplaylist': True,
                'writethumbnail': False,
                'writeinfojson': False,
                'no_check_certificate': True,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': str(quality),
                }],
                'http_headers': {
                    'User-Agent': self._get_random_user_agent(),
                    'Accept-Language': 'en-US,en;q=0.5',
                },
                'retries': 5,
                'fragment_retries': 5,
                'socket_timeout': 60,
                'no_warnings': True,
                'ignoreerrors': False,
                'verbose': False,
                'progress_hooks': [lambda d: self._ydl_progress_hook(d, progress_hook_key)],
                **self._get_cookie_opts(),
            }
            
            self._update_status(request_id, 'processing', 'Starting audio download with yt-dlp...')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self._update_status(request_id, 'processing', 'Extracting audio...')
                ydl.download([url])

            final_filename_from_hook = self.final_filenames.pop(progress_hook_key, None)

            if final_filename_from_hook and Path(final_filename_from_hook).exists():
                final_downloaded_file_path = Path(final_filename_from_hook)
            else:
                expected_final_filename = f"{safe_title}_{video_id}_{quality}kbps.mp3"
                potential_file = self.downloads_dir / expected_final_filename
                if potential_file.exists() and potential_file.is_file():
                    final_downloaded_file_path = potential_file
                else:
                    glob_pattern = f"{safe_title}_{video_id}_{quality}kbps.*"
                    found_files = list(self.downloads_dir.glob(glob_pattern))
                    if found_files:
                        final_downloaded_file_path = found_files[0] 
            
            if not final_downloaded_file_path or not final_downloaded_file_path.exists():
                raise Exception("Downloaded audio file not found after yt-dlp execution.")

            file_size_mb = final_downloaded_file_path.stat().st_size / (1024 * 1024)
            if file_size_mb == 0:
                if final_downloaded_file_path.exists(): final_downloaded_file_path.unlink(missing_ok=True)
                raise Exception("Downloaded audio file is empty.")

            self._update_status(request_id, 'processing', 'Preparing local download link...')
            download_url = f"{CONFIG['LOCAL_SERVER_URL']}/download/{final_downloaded_file_path.name}"
            status_message = 'Audio download complete. File available locally.'

            self._update_status(
                request_id, 'complete', status_message,
                download_url=download_url, file_size_mb=file_size_mb
            )

        except Exception as e:
            error_msg = str(e)
            specific_msg = f"Audio download failed: {error_msg}"
            self._update_status(request_id, 'failed', specific_msg)
        finally:
            self.final_filenames.pop(progress_hook_key, None)

    def start_download(self, video_id, resolution, title, codec=None):
        request_id = str(uuid.uuid4())
        with download_lock:
            download_status[request_id] = {
                'status': 'pending',
                'message': 'Download request received',
                'created_at': datetime.now().isoformat(),
                'video_id': video_id,
                'resolution': resolution,
                'title': title,
                'codec': codec or 'h264',
                'type': 'video',
                'download_url': None,
                'file_size_mb': None,
                'updated_at': datetime.now().isoformat(),
            }
        
        download_thread = threading.Thread(
            target=self._download_video,
            args=(request_id, video_id, resolution, title, codec),
            daemon=True
        )
        download_thread.start()
        return request_id

    def _ydl_progress_hook(self, d, progress_hook_key):
        if d['status'] == 'finished':
            self.final_filenames[progress_hook_key] = d.get('filename') or d.get('info_dict', {}).get('_filename')
        elif d['status'] == 'error':
            print(f"yt-dlp reported an error for {progress_hook_key}: {d.get('error')}")

    def _get_ydl_options(self, output_template_path, resolution, progress_hook_key, codec=None):
        # codec: None/'h264' (default) or 'hevc' (h265). HEVC may not be available; fallbacks included
        if codec == 'hevc':
            # Prefer HEVC in MP4 if available, then fallback to any HEVC, then generic best
            format_spec = (
                f'bestvideo[height<={resolution}][ext=mp4][vcodec^=hev1]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={resolution}][ext=mp4][vcodec^=hvc1]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={resolution}][vcodec^=hev1]+bestaudio/'
                f'bestvideo[height<={resolution}][vcodec^=hvc1]+bestaudio/'
                f'bestvideo[height<={resolution}][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={resolution}]+bestaudio/'
                f'best[height<={resolution}][ext=mp4]/'
                f'best[height<={resolution}]/'
                f'bestvideo+bestaudio/best'
            )
        else:
            # Default: target H.264/AVC in MP4 with fallbacks
            format_spec = (
                f'bestvideo[height<={resolution}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={resolution}][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={resolution}][vcodec^=avc1]+bestaudio/'
                f'bestvideo[height<={resolution}]+bestaudio/'
                f'bestvideo[height<={resolution}][ext=webm]+bestaudio[ext=opus]/'
                f'bestvideo[height<={resolution}][ext=webm]+bestaudio/'
                f'best[height<={resolution}][ext=mp4]/'
                f'best[height<={resolution}]/'
                f'bestvideo+bestaudio/best'
            )
        
        return {
            'format': format_spec,
            'outtmpl': str(output_template_path),
            'noplaylist': True,
            'merge_output_format': 'mp4',
            'writethumbnail': False,
            'writeinfojson': False,
            'http_headers': {
                'User-Agent': self._get_random_user_agent(),
                'Accept-Language': 'en-US,en;q=0.5',
            },
            'retries': 5,
            'fragment_retries': 5,
            'socket_timeout': 60,
            'no_warnings': True,
            'ignoreerrors': False,
            'verbose': False,
            'progress_hooks': [lambda d: self._ydl_progress_hook(d, progress_hook_key)],
            **self._get_cookie_opts(),
        }

    def _download_video(self, request_id, video_id, resolution, title, codec=None):
        final_downloaded_file_path = None
        progress_hook_key = f"{request_id}_progress_{str(uuid.uuid4())[:8]}"

        try:
            self._update_status(request_id, 'processing', 'Initializing download...')

            safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip().replace(" ", "_")
            safe_title = safe_title[:60]
            
            codec_suffix = "_h265" if (codec == 'hevc') else ""
            filename_template_str = f"{safe_title}_{video_id}_{resolution}p{codec_suffix}.%(ext)s"
            output_template_path = self.downloads_dir / filename_template_str
            
            url = f'https://www.youtube.com/watch?v={video_id}'
            
            ydl_opts = self._get_ydl_options(output_template_path, resolution, progress_hook_key, codec)
            
            self._update_status(request_id, 'processing', 'Starting download with yt-dlp...')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self._update_status(request_id, 'processing', 'Downloading video...')
                ydl.download([url])

            final_filename_from_hook = self.final_filenames.pop(progress_hook_key, None)

            if final_filename_from_hook and Path(final_filename_from_hook).exists():
                final_downloaded_file_path = Path(final_filename_from_hook)
            else:
                expected_final_filename = f"{safe_title}_{video_id}_{resolution}p{codec_suffix}.mp4"
                potential_file = self.downloads_dir / expected_final_filename
                if potential_file.exists() and potential_file.is_file():
                    final_downloaded_file_path = potential_file
                else:
                    glob_pattern = f"{safe_title}_{video_id}_{resolution}p{codec_suffix}.*"
                    found_files = list(self.downloads_dir.glob(glob_pattern))
                    if found_files:
                        final_downloaded_file_path = found_files[0] 
            
            if not final_downloaded_file_path or not final_downloaded_file_path.exists():
                raise Exception("Downloaded file not found after yt-dlp execution.")

            file_size_mb = final_downloaded_file_path.stat().st_size / (1024 * 1024)
            if file_size_mb == 0:
                if final_downloaded_file_path.exists(): final_downloaded_file_path.unlink(missing_ok=True)
                raise Exception("Downloaded file is empty.")

            self._update_status(request_id, 'processing', 'Preparing local download link...')
            download_url = f"{CONFIG['LOCAL_SERVER_URL']}/download/{final_downloaded_file_path.name}"
            status_message = 'Download complete. File available locally.'

            self._update_status(
                request_id, 'complete', status_message,
                download_url=download_url, file_size_mb=file_size_mb
            )

        except yt_dlp.utils.DownloadError as de:
            error_msg = str(de).split('\n')[-1]
            user_message = f"Download failed (yt-dlp): {error_msg}"
            if "Unsupported URL" in str(de): user_message = "The video URL is unsupported or invalid."
            elif "Video unavailable" in str(de): user_message = "This video is unavailable."
            elif "Private video" in str(de): user_message = "This video is private."
            elif "HTTP Error 403" in str(de): user_message = "Access denied (403 Forbidden)."
            elif "HTTP Error 404" in str(de): user_message = "Video not found (404)."
            elif "HTTP Error 429" in str(de): user_message = "Too many requests (429)."
            self._update_status(request_id, 'failed', user_message)
        except Exception as e:
            error_msg = str(e)
            specific_msg = f"An unexpected error occurred: {error_msg}"
            if "File too large" in error_msg: specific_msg = error_msg
            elif "Downloaded file is empty" in error_msg: specific_msg = "Download resulted in an empty file."
            elif "Downloaded file not found" in error_msg: specific_msg = "Could not locate the video file after download process."
            self._update_status(request_id, 'failed', specific_msg)
        finally:
            self.final_filenames.pop(progress_hook_key, None)

    def _update_status(self, request_id, status, message, download_url=None, file_size_mb=None):
        with download_lock:
            if request_id and request_id in download_status:
                entry = download_status[request_id]
                entry['status'] = status
                entry['message'] = message
                entry['updated_at'] = datetime.now().isoformat()
                if download_url: entry['download_url'] = download_url
                if file_size_mb is not None: entry['file_size_mb'] = file_size_mb
                if 'type' not in entry:
                    if 'resolution' in entry: entry['type'] = 'video'
                    elif 'quality' in entry: entry['type'] = 'audio'

    def get_status(self, request_id):
        with download_lock:
            return download_status.get(request_id)

    def get_all_status(self):
        with download_lock:
            return dict(download_status)

    def get_playlist_info(self, playlist_id):
        try:
            url = f'https://www.youtube.com/playlist?list={playlist_id}'
            ydl_opts = {
                'extract_flat': True, 
                'quiet': True,
                'no_warnings': True,
                'http_headers': {
                    'User-Agent': self._get_random_user_agent(),
                },
                **self._get_cookie_opts(),
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            
            entries = info.get('entries', [])
            total_duration = 0
            valid_video_count = 0
            
            for entry in entries:
                if entry.get('duration'):
                    total_duration += entry['duration']
                    valid_video_count += 1
            
            # Estimate sizes based on total duration and resolution
            # Using same improved estimation logic as single video
            estimated_sizes = {}
            for res in [1080, 720, 480]:
                est = self._improved_estimation(res, total_duration)
                estimated_sizes[res] = est['filesize']
                
            return {
                'success': True,
                'title': info.get('title', 'Unknown Playlist'),
                'video_count': len(entries),
                'total_duration': total_duration,
                'estimated_sizes': estimated_sizes,
                'thumbnail': info.get('thumbnails', [{}])[0].get('url') if info.get('thumbnails') else None
            }
        except Exception as e:
             return {'success': False, 'message': str(e)}

    def start_playlist_download(self, playlist_id, resolution, title):
        request_id = str(uuid.uuid4())
        with download_lock:
            download_status[request_id] = {
                'status': 'pending', 
                'message': 'Playlist download request received',
                'created_at': datetime.now().isoformat(),
                'playlist_id': playlist_id,
                'resolution': resolution,
                'title': title,
                'type': 'playlist',
                'updated_at': datetime.now().isoformat(),
                'download_url': None # Playlist doesn't have a single file URL
            }
        
        thread = threading.Thread(
            target=self._download_playlist,
            args=(request_id, playlist_id, resolution, title),
            daemon=True
        )
        thread.start()
        return request_id

    def _download_playlist(self, request_id, playlist_id, resolution, title):
        try:
            self._update_status(request_id, 'processing', 'Initializing playlist download...')
            
            safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip().replace(" ", "_")
            playlist_dir = self.downloads_dir / safe_title
            playlist_dir.mkdir(exist_ok=True)
            
            url = f'https://www.youtube.com/playlist?list={playlist_id}'
            
            # Calculate format spec (reusing logic but simplified)
            if resolution == 480:
                format_spec = f'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/bestvideo+bestaudio/best'
            elif resolution == 360:
                format_spec = f'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/bestvideo+bestaudio/best'
            else:
                format_spec = f'bestvideo[height<={resolution}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]/bestvideo+bestaudio/best'

            ydl_opts = {
                'format': format_spec,
                'outtmpl': str(playlist_dir / '%(title)s.%(ext)s'),
                'noplaylist': False,
                'writethumbnail': False, 
                'http_headers': {
                    'User-Agent': self._get_random_user_agent(),
                },
                'retries': 5,
                'ignoreerrors': True,
                'no_warnings': True,
                **self._get_cookie_opts(),
            }
            
            self._update_status(request_id, 'processing', f'Downloading playlist "{title}"...')
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
                
            self._update_status(request_id, 'complete', f'Playlist "{title}" downloaded successfully.')
            
        except Exception as e:
            self._update_status(request_id, 'failed', f'Playlist download failed: {str(e)}')

class TelegramBot:
    def __init__(self, bot_token):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    def send_message(self, chat_id, text, parse_mode='HTML'):
        """Send a text message to a Telegram chat"""
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': text,
                'parse_mode': parse_mode
            }
            response = requests.post(url, json=data, timeout=10)
            response.raise_for_status()
            return {'success': True, 'response': response.json()}
        except Exception as e:
            print(f"Telegram send message error: {e}")
            return {'success': False, 'error': str(e)}

    def send_message_with_keyboard(self, chat_id, text, keyboard_rows, parse_mode='HTML'):
        """Send a message with inline keyboard (list of rows; each row list of {text, callback_data})"""
        try:
            url = f"{self.base_url}/sendMessage"
            reply_markup = {
                'inline_keyboard': keyboard_rows
            }
            data = {
                'chat_id': chat_id,
                'text': text,
                'parse_mode': parse_mode,
                'reply_markup': reply_markup
            }
            response = requests.post(url, json=data, timeout=10)
            response.raise_for_status()
            return {'success': True, 'response': response.json()}
        except Exception as e:
            print(f"Telegram send message with keyboard error: {e}")
            return {'success': False, 'error': str(e)}

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        try:
            url = f"{self.base_url}/answerCallbackQuery"
            data = {
                'callback_query_id': callback_query_id
            }
            if text:
                data['text'] = text
            if show_alert:
                data['show_alert'] = True
            response = requests.post(url, data=data, timeout=10)
            response.raise_for_status()
            return {'success': True}
        except Exception as e:
            print(f"Telegram answerCallbackQuery error: {e}")
            return {'success': False, 'error': str(e)}
    
    def send_video(self, chat_id, video_path, caption=None):
        """Send a video file to a Telegram chat"""
        try:
            if not Path(video_path).exists():
                return {'success': False, 'error': 'File not found'}
            
            url = f"{self.base_url}/sendVideo"
            with open(video_path, 'rb') as video_file:
                files = {'video': video_file}
                data = {
                    'chat_id': chat_id,
                    'supports_streaming': True
                }
                if caption:
                    data['caption'] = caption
                
                response = requests.post(url, files=files, data=data, timeout=300)
                response.raise_for_status()
                return {'success': True, 'response': response.json()}
        except Exception as e:
            print(f"Telegram send video error: {e}")
            return {'success': False, 'error': str(e)}
    
    def send_audio(self, chat_id, audio_path, caption=None, title=None, performer=None):
        """Send an audio file to a Telegram chat"""
        try:
            if not Path(audio_path).exists():
                return {'success': False, 'error': 'File not found'}
            
            url = f"{self.base_url}/sendAudio"
            with open(audio_path, 'rb') as audio_file:
                files = {'audio': audio_file}
                data = {
                    'chat_id': chat_id
                }
                if caption:
                    data['caption'] = caption
                if title:
                    data['title'] = title
                if performer:
                    data['performer'] = performer
                
                response = requests.post(url, files=files, data=data, timeout=300)
                response.raise_for_status()
                return {'success': True, 'response': response.json()}
        except Exception as e:
            print(f"Telegram send audio error: {e}")
            return {'success': False, 'error': str(e)}
    
    def send_document(self, chat_id, document_path, caption=None):
        """Send a document file to a Telegram chat"""
        try:
            if not Path(document_path).exists():
                return {'success': False, 'error': 'File not found'}
            
            url = f"{self.base_url}/sendDocument"
            with open(document_path, 'rb') as doc_file:
                files = {'document': doc_file}
                data = {'chat_id': chat_id}
                if caption:
                    data['caption'] = caption
                
                response = requests.post(url, files=files, data=data, timeout=300)
                response.raise_for_status()
                return {'success': True, 'response': response.json()}
        except Exception as e:
            print(f"Telegram send document error: {e}")
            return {'success': False, 'error': str(e)}
    
    def get_me(self):
        """Get bot information"""
        try:
            url = f"{self.base_url}/getMe"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return {'success': True, 'response': response.json()}
        except Exception as e:
            print(f"Telegram getMe error: {e}")
            return {'success': False, 'error': str(e)}

# Initialize Telegram bot
telegram_bot = TelegramBot(CONFIG['TELEGRAM_BOT_TOKEN'])

# Add polling for Telegram updates
last_update_id = 0

def telegram_polling_loop():
    """Poll Telegram for updates and respond to messages"""
    global last_update_id
    while True:
        try:
            url = f"{telegram_bot.base_url}/getUpdates"
            params = {'offset': last_update_id + 1, 'timeout': 30}
            response = requests.get(url, params=params, timeout=35)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('ok') and data.get('result'):
                    for update in data['result']:
                        last_update_id = update['update_id']
                        
                        # Handle button callbacks
                        if 'callback_query' in update:
                            cq = update['callback_query']
                            cq_id = cq.get('id')
                            msg = cq.get('message', {})
                            chat = msg.get('chat', {})
                            chat_id = chat.get('id')
                            data_str = cq.get('data', '') or ''
                            try:
                                if data_str.startswith('v:'):
                                    # v:RES:codec:VIDEOID
                                    _, res_str, codec, vid = data_str.split(':', 3)
                                    res = res_str
                                    # Fetch title
                                    try:
                                        info = downloader.get_video_info(vid)
                                        title = info.get('title') or f"Video_{vid}"
                                    except Exception:
                                        title = f"Video_{vid}"
                                    request_id = downloader.start_download(vid, res, title, codec=codec)
                                    codec_label = 'H.265' if codec == 'hevc' else 'H.264'
                                    telegram_bot.answer_callback_query(cq_id, text='Download started')
                                    telegram_bot.send_message(chat_id, (
                                        f"🎬 Starting MP4 {res}p ({codec_label}) for {vid}\n"
                                        f"Status: http://localhost:8000/api/download_status/{request_id}"
                                    ))
                                    threading.Thread(target=_notify_when_done, args=(request_id, chat_id), daemon=True).start()
                                elif data_str.startswith('a:'):
                                    # a:QUALITY:VIDEOID
                                    _, q_str, vid = data_str.split(':', 2)
                                    quality = q_str
                                    try:
                                        info = downloader.get_video_info(vid)
                                        title = info.get('title') or f"Video_{vid}"
                                    except Exception:
                                        title = f"Video_{vid}"
                                    request_id = downloader.start_audio_download(vid, quality, title)
                                    telegram_bot.answer_callback_query(cq_id, text='Audio download started')
                                    telegram_bot.send_message(chat_id, (
                                        f"🎵 Starting MP3 {quality}kbps for {vid}\n"
                                        f"Status: http://localhost:8000/api/download_status/{request_id}"
                                    ))
                                    threading.Thread(target=_notify_when_done, args=(request_id, chat_id), daemon=True).start()
                                else:
                                    telegram_bot.answer_callback_query(cq_id)
                            except Exception as e:
                                print(f"Callback handling error: {e}")
                                if cq_id:
                                    telegram_bot.answer_callback_query(cq_id, text='Error', show_alert=False)
                            continue

                        # Process message
                        if 'message' in update:
                            message = update['message']
                            chat_id = message.get('chat', {}).get('id')
                            text = message.get('text', '').lower().strip()
                            
                            # Respond to /start
                            if text == '/start':
                                welcome_msg = (
                                    "🎉 Welcome to YouTube Downloader Bot!\n\n"
                                    "I can download YouTube videos and send them directly to you.\n\n"
                                    "📋 Available Commands:\n"
                                    "/start - Show this welcome message\n"
                                    "/help - Show help information\n\n"
                                    "⚙️ How to use:\n"
                                    "1. Send me a YouTube video URL\n"
                                    "2. I'll download it for you\n"
                                    "3. The file will be sent to you\n\n"
                                    "Let's get started! 🚀"
                                )
                                telegram_bot.send_message(chat_id, welcome_msg)
                                print(f"Sent welcome message to chat {chat_id}")
                            
                            # Respond to /help
                            elif text == '/help':
                                help_msg = (
                                    "📖 Help - YouTube Downloader Bot\n\n"
                                    "🔗 Supported Format:\n"
                                    "Send me a YouTube URL like:\n"
                                    "https://www.youtube.com/watch?v=VIDEO_ID\n\n"
                                    "📥 What I do:\n"
                                    "• Download videos in multiple qualities\n"
                                    "• Extract audio from videos\n"
                                    "• Send files directly to you\n\n"
                                    "⚙️ Commands:\n"
                                    "/start - Start using the bot\n"
                                    "/help - Show this help\n\n"
                                    "Ready to download! 🎬"
                                )
                                telegram_bot.send_message(chat_id, help_msg)
                                print(f"Sent help message to chat {chat_id}")
                            
                            # Handle YouTube URLs
                            elif 'youtube.com' in message.get('text', '') or 'youtu.be' in message.get('text', ''):
                                url_text = message.get('text', '')
                                # Extract video ID
                                video_id_match = re.search(r'(?:v=|\/)([a-zA-Z0-9_-]{11})', url_text)
                                if video_id_match:
                                    video_id = video_id_match.group(1)
                                    # Fetch info to compute sizes and title
                                    try:
                                        info = downloader.get_video_info(video_id)
                                        title = info.get('title') or f"Video_{video_id}"
                                        duration = info.get('duration', 0) or 0
                                        vf = info.get('video_formats', {}) or {}
                                        af = info.get('audio_formats', {}) or {}
                                    except Exception:
                                        info = None
                                        title = f"Video_{video_id}"
                                        duration = 0
                                        vf, af = {}, {}

                                    # Prepare sizes for video resolutions
                                    res_list = [1080, 720, 480, 360, 240, 144]
                                    def fmt_mb(sz):
                                        try:
                                            return f"{max(1, int(round(sz/1024/1024)))}MB"
                                        except Exception:
                                            return "~MB"
                                    video_sizes = {}
                                    for r in res_list:
                                        if r in vf and 'filesize' in vf[r]:
                                            video_sizes[r] = fmt_mb(vf[r]['filesize'])
                                        else:
                                            # estimate if missing
                                            try:
                                                est = downloader._improved_estimation(r, duration or 600)
                                                video_sizes[r] = fmt_mb(est['filesize'])
                                            except Exception:
                                                video_sizes[r] = "~MB"

                                    # Prepare sizes for audio bitrates
                                    audio_list = [320, 256, 192, 128]
                                    audio_sizes = {}
                                    for q in audio_list:
                                        if q in af and 'filesize' in af[q]:
                                            audio_sizes[q] = fmt_mb(af[q]['filesize'])
                                        else:
                                            try:
                                                est = downloader._improved_audio_estimation(q, duration or 600)
                                                audio_sizes[q] = fmt_mb(est['filesize'])
                                            except Exception:
                                                audio_sizes[q] = "~MB"

                                    # Build inline keyboard
                                    kb = []
                                    # H.264 row(s)
                                    kb.append([
                                        { 'text': f"MP4 1080p {video_sizes[1080]}", 'callback_data': f"v:1080:h264:{video_id}" },
                                        { 'text': f"MP4 720p {video_sizes[720]}", 'callback_data': f"v:720:h264:{video_id}" }
                                    ])
                                    kb.append([
                                        { 'text': f"MP4 480p {video_sizes[480]}", 'callback_data': f"v:480:h264:{video_id}" },
                                        { 'text': f"MP4 360p {video_sizes[360]}", 'callback_data': f"v:360:h264:{video_id}" }
                                    ])
                                    kb.append([
                                        { 'text': f"MP4 240p {video_sizes[240]}", 'callback_data': f"v:240:h264:{video_id}" },
                                        { 'text': f"MP4 144p {video_sizes[144]}", 'callback_data': f"v:144:h264:{video_id}" }
                                    ])
                                    # H.265 row(s)
                                    kb.append([
                                        { 'text': f"HEVC 720p {video_sizes[720]}", 'callback_data': f"v:720:hevc:{video_id}" },
                                        { 'text': f"HEVC 480p {video_sizes[480]}", 'callback_data': f"v:480:hevc:{video_id}" }
                                    ])
                                    # Audio row(s)
                                    kb.append([
                                        { 'text': f"MP3 320 {audio_sizes[320]}", 'callback_data': f"a:320:{video_id}" },
                                        { 'text': f"MP3 256 {audio_sizes[256]}", 'callback_data': f"a:256:{video_id}" }
                                    ])
                                    kb.append([
                                        { 'text': f"MP3 192 {audio_sizes[192]}", 'callback_data': f"a:192:{video_id}" },
                                        { 'text': f"MP3 128 {audio_sizes[128]}", 'callback_data': f"a:128:{video_id}" }
                                    ])

                                    text_msg = (
                                        f"🎬 Choose format for: <b>{title}</b>\n\n"
                                        f"Video (MP4 H.264 or HEVC) and Audio (MP3). Sizes are estimates."
                                    )
                                    telegram_bot.send_message_with_keyboard(chat_id, text_msg, kb, parse_mode='HTML')
                                    print(f"Presented selection keyboard for {video_id} to chat {chat_id}")
                                else:
                                    telegram_bot.send_message(chat_id, "❌ Could not extract video ID. Please send a valid YouTube URL.")
                            
                            else:
                                # Unknown command
                                unknown_msg = (
                                    "❓ I don't understand that command.\n\n"
                                    "Send /help to see what I can do.\n\n"
                                    "Or send me a YouTube URL to download!"
                                )
                                telegram_bot.send_message(chat_id, unknown_msg)
                        
        except requests.exceptions.Timeout:
            # Timeout is normal, just continue polling
            continue
        except Exception as e:
            print(f"Telegram polling error: {e}")
            time.sleep(5)

# Start polling in background thread
polling_thread = threading.Thread(target=telegram_polling_loop, daemon=True)
polling_thread.start()
print("Telegram polling started - bot is now listening for messages!")

downloader = VideoDownloader()

# ---------------------------------------------------------------------------
# Helper: wait for a download to finish, send the file to Telegram, then
# delete it from disk.  This is the key strategy for Render's ephemeral FS:
# files are gone after a restart anyway, so we ship them immediately.
# ---------------------------------------------------------------------------
def _notify_when_done(request_id, chat_id):
    """Wait for download to complete, send file to Telegram, delete from disk."""
    try:
        start_time = time.time()
        timeout_seconds = 2 * 60 * 60  # 2-hour safety cap

        while time.time() - start_time < timeout_seconds:
            status = downloader.get_status(request_id)

            if status and status.get('status') == 'failed':
                err = status.get('message', 'Unknown error')
                telegram_bot.send_message(chat_id, f"❌ Download failed: {err}")
                return

            if status and status.get('status') == 'complete':
                # Resolve the local file path from the stored download URL
                url = status.get('download_url') or ''
                filename = url.rsplit('/', 1)[-1] if '/' in url else ''
                size_mb = status.get('file_size_mb')
                size_str = f" ({size_mb:.1f} MB)" if isinstance(size_mb, (int, float)) else ''

                if not filename:
                    telegram_bot.send_message(chat_id, "✅ Download complete but could not locate file.")
                    return

                file_path = downloader.downloads_dir / filename

                if not file_path.exists():
                    telegram_bot.send_message(
                        chat_id,
                        f"✅ Download complete but file not found on disk: {filename}{size_str}"
                    )
                    return

                # Telegram 50 MB upload limit
                TELEGRAM_LIMIT_MB = 49
                if size_mb and size_mb > TELEGRAM_LIMIT_MB:
                    telegram_bot.send_message(
                        chat_id,
                        f"⚠️ File is too large to send via Telegram ({size_mb:.1f} MB > {TELEGRAM_LIMIT_MB} MB).\n"
                        f"📁 File: <code>{filename}</code>",
                    )
                    return

                # Notify user we're uploading
                telegram_bot.send_message(
                    chat_id,
                    f"📤 Uploading <b>{filename}</b>{size_str} to Telegram…",
                 )

                # Send the file (video or audio)
                dl_type = status.get('type', 'video')
                caption = f"✅ {filename}{size_str}"

                if dl_type == 'audio' or filename.endswith('.mp3'):
                    result = telegram_bot.send_audio(
                        chat_id, str(file_path),
                        caption=caption,
                        title=status.get('title', filename),
                    )
                else:
                    result = telegram_bot.send_video(
                        chat_id, str(file_path),
                        caption=caption,
                    )

                if result.get('success'):
                    print(f"[Telegram] Sent {filename} to chat {chat_id}")
                else:
                    telegram_bot.send_message(
                        chat_id,
                        f"⚠️ Could not upload file: {result.get('error')}"
                    )

                # Delete the file from disk to free space (ephemeral FS strategy)
                try:
                    file_path.unlink(missing_ok=True)
                    print(f"[Cleaner] Deleted {filename} after Telegram upload.")
                except Exception as del_err:
                    print(f"[Cleaner] Could not delete {filename}: {del_err}")

                return

            time.sleep(3)

        telegram_bot.send_message(
            chat_id,
            "⏱️ Download timed out after 2 hours. Please try again with a shorter video."
        )
    except Exception as e:
        print(f"[Notifier] Error: {e}")

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route('/api/video_info/<video_id>', methods=['GET'])
def api_get_video_info(video_id):
    try:
        if not video_id or not video_id.replace('-', '').replace('_', '').isalnum() or len(video_id) > 15:
            return jsonify({'success': False, 'message': 'Invalid videoId format'}), 400
        
        info = downloader.get_video_info(video_id)
        return jsonify(info)
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/playlist_info', methods=['POST'])
def api_get_playlist_info():
    """Get info for a playlist"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No JSON data provided'}), 400
            
        playlist_id = data.get('playlistId')
        if not playlist_id:
            return jsonify({'success': False, 'message': 'playlistId is required'}), 400
            
        return jsonify(downloader.get_playlist_info(playlist_id))
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/download_playlist', methods=['POST'])
def api_download_playlist():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No JSON data provided'}), 400
        
        playlist_id = data.get('playlistId')
        resolution = data.get('resolution', '720')
        title = data.get('title', 'Unknown Playlist')
        
        if not playlist_id:
            return jsonify({'success': False, 'message': 'playlistId is required'}), 400
            
        request_id = downloader.start_playlist_download(playlist_id, resolution, title)
        return jsonify({
            'success': True,
            'message': 'Playlist download started',
            'requestId': request_id
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/download_video', methods=['POST'])
def api_download_video():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No JSON data provided'}), 400
        
        video_id = data.get('videoId')
        resolution = data.get('resolution', '720')
        codec = data.get('codec')  # optional: 'h264' | 'hevc'
        title = data.get('title', 'Unknown Video')
        
        if not video_id:
            return jsonify({'success': False, 'message': 'videoId is required'}), 400
        if not video_id.replace('-', '').replace('_', '').isalnum() or len(video_id) > 15:
            return jsonify({'success': False, 'message': 'Invalid videoId format'}), 400
        if resolution not in ['1080', '720', '480', '360', '240', '144']:
            return jsonify({'success': False, 'message': 'Invalid resolution'}), 400
        if codec and codec not in ['h264', 'hevc']:
            return jsonify({'success': False, 'message': 'Invalid codec'}), 400
        
        request_id = downloader.start_download(video_id, resolution, title, codec=codec)
        return jsonify({
            'success': True,
            'message': 'Download started',
            'requestId': request_id
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/download_audio', methods=['POST'])
def api_download_audio():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No JSON data provided'}), 400
        
        video_id = data.get('videoId')
        quality = data.get('quality', '128')
        title = data.get('title', 'Unknown Audio')
        
        if not video_id:
            return jsonify({'success': False, 'message': 'videoId is required'}), 400
        if not video_id.replace('-', '').replace('_', '').isalnum() or len(video_id) > 15:
            return jsonify({'success': False, 'message': 'Invalid videoId format'}), 400
        if quality not in ['128', '192', '256', '320']:
            return jsonify({'success': False, 'message': 'Invalid audio quality'}), 400
        
        request_id = downloader.start_audio_download(video_id, quality, title)
        return jsonify({
            'success': True,
            'message': 'Audio download started',
            'requestId': request_id
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/download_status/<request_id>', methods=['GET'])
def api_get_download_status(request_id):
    try:
        status = downloader.get_status(request_id)
        if not status:
            return jsonify({'error': 'Request ID not found'}), 404
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/status', methods=['GET'])
def api_get_all_status():
    try:
        all_statuses = downloader.get_all_status()
        return jsonify({'downloads': all_statuses})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download/<filename>', methods=['GET'])
def serve_file_locally(filename):
    try:
        safe_filename = Path(filename).name 
        file_path = downloader.downloads_dir / safe_filename
        
        if not file_path.exists() or not file_path.is_file():
            return jsonify({'error': 'File not found or is not a file'}), 404
        
        return send_file(
            file_path,
            as_attachment=True,
            download_name=safe_filename
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/delete_file', methods=['POST'])
def api_delete_file():
    try:
        data = request.get_json()
        if not data or 'filename' not in data:
            return jsonify({'success': False, 'message': 'Filename not provided'}), 400

        filename_from_client = data['filename']
        filename = Path(filename_from_client).name 

        if not filename or filename == '.' or filename == '..':
            return jsonify({'success': False, 'message': 'Invalid filename component provided'}), 400

        file_path = downloader.downloads_dir / filename

        if file_path.exists() and file_path.is_file():
            try:
                file_path.unlink()
                return jsonify({'success': True, 'message': f'File {filename} deleted successfully.'})
            except Exception as e:
                return jsonify({'success': False, 'message': f'Could not delete file: {str(e)}'}), 500
        else:
            return jsonify({'success': False, 'message': 'File not found or is not a file.'}), 404
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

# Telegram Bot Endpoints
@app.route('/api/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Receive updates from Telegram"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data received'}), 400
        
        message = data.get('message', {})
        chat_id = message.get('chat', {}).get('id')
        text = message.get('text', '')
        
        if text:
            response_msg = f"Hello! I received: {text}"
            telegram_bot.send_message(chat_id, response_msg)
        
        return jsonify({'success': True, 'message': 'Webhook processed'})
    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/telegram/send_file', methods=['POST'])
def telegram_send_file():
    """Send a downloaded file to Telegram"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No JSON data provided'}), 400
        
        chat_id = data.get('chat_id')
        filename = data.get('filename')
        file_type = data.get('type', 'auto')  # auto, video, audio, document
        
        if not chat_id:
            return jsonify({'success': False, 'message': 'chat_id is required'}), 400
        if not filename:
            return jsonify({'success': False, 'message': 'filename is required'}), 400
        
        file_path = downloader.downloads_dir / filename
        
        if not file_path.exists():
            return jsonify({'success': False, 'message': 'File not found'}), 404
        
        caption = data.get('caption', 'Downloaded file')
        
        # Send file based on type
        if file_type == 'video' or (file_type == 'auto' and filename.endswith('.mp4')):
            result = telegram_bot.send_video(chat_id, str(file_path), caption=caption)
        elif file_type == 'audio' or (file_type == 'auto' and filename.endswith('.mp3')):
            title = data.get('title', caption)
            result = telegram_bot.send_audio(chat_id, str(file_path), caption=caption, title=title)
        else:
            result = telegram_bot.send_document(chat_id, str(file_path), caption=caption)
        
        if result.get('success'):
            return jsonify({
                'success': True,
                'message': 'File sent to Telegram successfully',
                'response': result.get('response')
            })
        else:
            return jsonify({
                'success': False,
                'message': f"Failed to send file: {result.get('error')}"
            }), 500
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/telegram/send_message', methods=['POST'])
def telegram_send_message():
    """Send a message to Telegram"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No JSON data provided'}), 400
        
        chat_id = data.get('chat_id')
        text = data.get('text', 'Hello from YouTube Downloader!')
        parse_mode = data.get('parse_mode', 'HTML')
        
        if not chat_id:
            return jsonify({'success': False, 'message': 'chat_id is required'}), 400
        
        result = telegram_bot.send_message(chat_id, text, parse_mode=parse_mode)
        
        if result.get('success'):
            return jsonify({
                'success': True,
                'message': 'Message sent successfully',
                'response': result.get('response')
            })
        else:
            return jsonify({
                'success': False,
                'message': f"Failed to send message: {result.get('error')}"
            }), 500
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/telegram/bot_info', methods=['GET'])
def telegram_bot_info():
    """Get Telegram bot information"""
    try:
        result = telegram_bot.get_me()
        if result.get('success'):
            return jsonify({
                'success': True,
                'bot_info': result.get('response').get('result', {})
            })
        else:
            return jsonify({
                'success': False,
                'message': f"Failed to get bot info: {result.get('error')}"
            }), 500
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/', methods=['GET'])
def root_info():
    return jsonify({
        'service': 'YouTube Video Downloader with Telegram Integration',
        'status': 'running',
        'version': '3.1 - Telegram Bot Integration',
        'endpoints': {
            'health': '/health',
            'video_info': '/api/video_info/<video_id> (GET)',
            'download_video': '/api/download_video (POST)',
            'download_audio': '/api/download_audio (POST)',
            'status_single': '/api/download_status/<request_id> (GET)',
            'status_all': '/api/status (GET)',
            'serve_file': '/download/<filename> (GET)',
            'delete_file': '/api/delete_file (POST)',
            'telegram_webhook': '/api/telegram/webhook (POST)',
            'telegram_send_file': '/api/telegram/send_file (POST)',
            'telegram_send_message': '/api/telegram/send_message (POST)',
            'telegram_bot_info': '/api/telegram/bot_info (GET)',
            'sync_cookies': '/api/sync-cookies (POST)'
        }
    })

@app.route('/api/sync-cookies', methods=['POST'])
def sync_cookies():
    """Receive cookies in Netscape format from Chrome Extension and save to cookies.txt"""
    try:
        data = request.get_json()
        if not data or 'cookies' not in data:
            return jsonify({'success': False, 'message': 'No cookies provided'}), 400
            
        cookies_text = data['cookies']
        cookies_file = Path(__file__).parent / 'cookies.txt'
        
        with open(cookies_file, 'w', encoding='utf-8') as f:
            f.write(cookies_text)
            
        return jsonify({
            'success': True, 
            'message': f'Cookies successfully saved to {cookies_file.name}'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to sync cookies: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)
