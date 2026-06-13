import subprocess
import os
import sys
import json
import logging
import threading
from datetime import datetime, timedelta
import psiphon_manager

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PO-TOKEN")

# Thread safety lock for token generation
_generation_lock = threading.Lock()

# Thread safety lock for reading/writing cache
_cache_lock = threading.Lock()

# Cache storage
_cache = {
    'po_token': None,
    'visitor_data': None,
    'generated_at': None
}

def get_cache_duration_hours():
    try:
        return float(os.environ.get('PO_TOKEN_CACHE_HOURS', '2'))
    except ValueError:
        return 2.0

def invalidate_cache():
    with _cache_lock:
        _cache['po_token'] = None
        _cache['visitor_data'] = None
        _cache['generated_at'] = None
        logger.info("[PO-TOKEN] Cache invalidated.")

def get_cache_status():
    with _cache_lock:
        status = {
            'has_cached_token': bool(_cache['po_token']),
            'generated_at': _cache['generated_at'].isoformat() if _cache['generated_at'] else None,
            'cache_duration_hours': get_cache_duration_hours(),
        }
        if _cache['generated_at']:
            expiration = _cache['generated_at'] + timedelta(hours=get_cache_duration_hours())
            status['expires_at'] = expiration.isoformat()
            status['is_expired'] = datetime.now() > expiration
        else:
            status['expires_at'] = None
            status['is_expired'] = True
        return status

def _generate_new_token():
    """Run youtube-po-token-generator CLI in a subprocess to get new tokens."""
    # Prepare environment variables
    env = os.environ.copy()
    
    # Proxy Passthrough Handling
    if psiphon_manager.is_running():
        # youtube-po-token-generator uses global-agent which respects HTTPS_PROXY
        http_proxy = f"http://127.0.0.1:{psiphon_manager.HTTP_PORT}"
        env['HTTPS_PROXY'] = http_proxy
        logger.info(f"[PO-TOKEN] Psiphon is running. Routing generator traffic via HTTP proxy: {http_proxy}")
    else:
        logger.info("[PO-TOKEN] Psiphon is not running. Running generator without proxy.")

    # Render OOM / Timeout protection: 30s timeout on subprocess
    # Run in a headless manner without full browser (the generator uses jsdom under the hood)
    cmd = ["youtube-po-token-generator"]
    try:
        logger.info("[PO-TOKEN] Invoking youtube-po-token-generator subprocess...")
        use_shell = (sys.platform == 'win32')
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            shell=use_shell
        )
        
        if result.returncode != 0:
            logger.error(f"[PO-TOKEN] Subprocess failed with exit code {result.returncode}. Stderr: {result.stderr.strip()}")
            return None

        # Parse JSON output from stdout
        stdout_data = result.stdout.strip()
        logger.info(f"[PO-TOKEN] Subprocess succeeded. Output length: {len(stdout_data)}")
        
        try:
            parsed = json.loads(stdout_data)
            visitor_data = parsed.get("visitorData")
            po_token = parsed.get("poToken")
            
            if not visitor_data or not po_token:
                logger.error("[PO-TOKEN] Parsed output is missing visitorData or poToken.")
                return None
                
            return {
                'po_token': po_token,
                'visitor_data': visitor_data,
                'generated_at': datetime.now()
            }
        except json.JSONDecodeError:
            logger.error(f"[PO-TOKEN] Failed to parse JSON from stdout. Raw output: {stdout_data[:200]}")
            return None

    except subprocess.TimeoutExpired:
        logger.error("[PO-TOKEN] Subprocess timed out (30s limit exceeded). Preventing OOM/freeze.")
        return None
    except Exception as e:
        logger.error(f"[PO-TOKEN] Subprocess exception occurred: {e}")
        return None

def get_po_token(force_refresh=False):
    """
    Retrieve PO Token and Visitor Data. 
    Uses thread-safe cached values if available and valid.
    """
    global _cache
    
    # 1. Quick read under cache lock
    if not force_refresh:
        with _cache_lock:
            if _cache['po_token'] and _cache['generated_at']:
                expiration = _cache['generated_at'] + timedelta(hours=get_cache_duration_hours())
                if datetime.now() < expiration:
                    return {
                        'po_token': _cache['po_token'],
                        'visitor_data': _cache['visitor_data']
                    }

    # 2. Acquire generation lock
    with _generation_lock:
        # Re-check cache under cache lock in case another thread generated it while we were waiting
        if not force_refresh:
            with _cache_lock:
                if _cache['po_token'] and _cache['generated_at']:
                    expiration = _cache['generated_at'] + timedelta(hours=get_cache_duration_hours())
                    if datetime.now() < expiration:
                        return {
                            'po_token': _cache['po_token'],
                            'visitor_data': _cache['visitor_data']
                        }

        # Generate new token
        new_data = _generate_new_token()
        
        with _cache_lock:
            if new_data:
                _cache.update(new_data)
                logger.info("[PO-TOKEN] Successfully generated and cached new PO token.")
            else:
                logger.warning("[PO-TOKEN] Token generation failed. Returning current cache content.")
                
            return {
                'po_token': _cache['po_token'],
                'visitor_data': _cache['visitor_data']
            }
