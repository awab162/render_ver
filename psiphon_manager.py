import subprocess
import time
import json
import os
import urllib.request
from pathlib import Path

PSIPHON_BIN     = Path('/tmp/psiphon-tunnel-core')
PSIPHON_CONFIG  = Path('/tmp/psiphon_config.json')
SOCKS5_PORT     = 1080
HTTP_PORT       = 8080
PROXY_URL       = f'socks5://127.0.0.1:{SOCKS5_PORT}'

_psiphon_process = None   # تتبع الـ subprocess


def _download_binary():
    """تحميل binary لـ Linux x86_64 إذا غير موجود."""
    if PSIPHON_BIN.exists() and PSIPHON_BIN.stat().st_size > 1_000_000:
        print("PSIPHON: binary موجود، يتخطى التحميل.")
        return

    url = (
        "https://github.com/Psiphon-Labs/psiphon-tunnel-core"
        "/releases/latest/download/"
        "psiphon-tunnel-core-x86_64-unknown-linux-gnu"
    )
    print(f"PSIPHON: جاري تحميل binary...")
    urllib.request.urlretrieve(url, str(PSIPHON_BIN))
    PSIPHON_BIN.chmod(0o755)
    print(f"PSIPHON: تم التحميل — {PSIPHON_BIN.stat().st_size / 1e6:.1f} MB")


def _write_config():
    """كتابة config مناسب لـ psiphon-tunnel-core."""
    config = {
        "LocalSocksProxyPort": SOCKS5_PORT,
        "LocalHttpProxyPort":  HTTP_PORT,
        # القيم الافتراضية للبيلد العام (open source)
        "PropagationChannelId": "FFFFFFFFFFFFFFFF",
        "SponsorId":            "FFFFFFFFFFFFFFFF",
        "ConnectionWorkerPoolSize": 10,
        "TunnelPoolSize": 1,
        "UpstreamProxyUrl": "",
        # تسريع الاتصال الأول
        "LimitTunnelProtocols": ["OSSH", "SSH"],
        "EstablishTunnelTimeoutSeconds": 60,
    }
    PSIPHON_CONFIG.write_text(json.dumps(config), encoding='utf-8')
    print("PSIPHON: config مكتوب.")


def start() -> str | None:
    """
    تشغيل Psiphon في الخلفية.
    يرجع proxy URL إذا نجح، وNone إذا فشل.
    """
    global _psiphon_process

    try:
        _download_binary()
        _write_config()

        _psiphon_process = subprocess.Popen(
            [str(PSIPHON_BIN), '-config', str(PSIPHON_CONFIG)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        print("PSIPHON: subprocess شغّال، انتظار الاتصال...")

        # انتظر حتى يتصل Psiphon (max 60 ثانية)
        deadline = time.time() + 60
        for line in _psiphon_process.stdout:
            print(f"PSIPHON LOG: {line.rstrip()}")
            if 'tunnels 1' in line.lower() or 'active tunnel' in line.lower():
                print(f"PSIPHON: ✅ متصل — proxy جاهز على {PROXY_URL}")
                return PROXY_URL
            if time.time() > deadline:
                print("PSIPHON: ⏰ timeout — لم يتصل خلال 60 ثانية")
                stop()
                return None

    except Exception as e:
        print(f"PSIPHON: ❌ فشل التشغيل: {e}")
        return None


def stop():
    """إيقاف psiphon subprocess."""
    global _psiphon_process
    if _psiphon_process:
        _psiphon_process.terminate()
        _psiphon_process = None
        print("PSIPHON: تم الإيقاف.")


def is_running() -> bool:
    """هل Psiphon شغّال الآن؟"""
    return _psiphon_process is not None and _psiphon_process.poll() is None