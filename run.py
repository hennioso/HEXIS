"""
HEXIS — Unified startup script.

Starts the trading bot AND the web dashboard in a single process.

Usage:
    python3 run.py

Dashboard:  http://<server-ip>:5000
Bot logs:   bot.log  /  stdout
"""

import threading
import logging
import sys
import os

# ── Logging (shared for both components) ────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-14s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)

log = logging.getLogger("run")
log.info("=" * 60)
log.info("  HEXIS — starting unified process")
log.info("=" * 60)


# ── Dashboard (Flask in a background thread) ─────────────────────────────────
def _start_dashboard():
    import database as db
    from web_dashboard import app, _start_background_sync
    db.init_db()
    _start_background_sync()
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    log.info(f"Dashboard available at http://0.0.0.0:{port}")
    # use_reloader=False is required when running inside a thread
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


dashboard_thread = threading.Thread(
    target=_start_dashboard,
    name="Dashboard",
    daemon=True,   # dies automatically when the main thread exits
)
dashboard_thread.start()


# ── Crypto payment watcher (optional — only active if CRYPTO_WALLET_ADDRESS set) ─
import threading as _threading
import config as _config

if _config.CRYPTO_WALLET_ADDRESS:
    import crypto_watcher
    _crypto_stop = _threading.Event()
    _crypto_thread = _threading.Thread(
        target=crypto_watcher.watcher_loop,
        args=(_crypto_stop,),
        name="CryptoWatcher",
        daemon=True,
    )
    _crypto_thread.start()


# ── Trading bot (runs in the main thread — blocks until CTRL+C) ──────────────
from main import main   # noqa: E402
main()
