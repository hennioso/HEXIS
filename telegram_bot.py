"""
HEXIS Telegram Bot — polls for /connect <code> commands from users.

Users get a one-time link code from the dashboard settings.
They send /connect <code> to @HexisAgentBot.
The bot matches the code, saves their chat_id, and confirms.
"""

import logging
import time
import threading
import requests
import os

log = logging.getLogger("TelegramBot")

_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
_OFFSET = 0


def _api(method: str, **kwargs) -> dict:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/{method}",
            json=kwargs, timeout=10,
        )
        return r.json()
    except Exception as e:
        log.debug(f"Telegram API error: {e}")
        return {}


def _reply(chat_id, text: str):
    _api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def _handle_update(update: dict):
    global _OFFSET
    _OFFSET = update["update_id"] + 1
    msg = update.get("message", {})
    text = msg.get("text", "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id or not text:
        return

    if text.lower().startswith("/connect"):
        parts = text.split()
        if len(parts) < 2:
            _reply(chat_id, "Usage: <code>/connect YOUR_CODE</code>\n\nGet your code from the HEXIS dashboard → Settings → Connect Telegram.")
            return

        code = parts[1].strip().upper()
        try:
            import database as db
            user = db.get_user_by_telegram_link_code(code)
            if not user:
                _reply(chat_id, "❌ Invalid or expired code. Generate a new one in the HEXIS dashboard.")
                return
            db.save_telegram_chat_id(user["id"], chat_id)
            _reply(
                chat_id,
                f"✅ <b>Connected!</b>\n\n"
                f"Account <b>{user['username']}</b> is now linked.\n"
                f"You'll receive trade notifications here."
            )
            log.info(f"Telegram linked: user '{user['username']}' → chat_id {chat_id}")
        except Exception as e:
            log.error(f"Telegram connect error: {e}")
            _reply(chat_id, "⚠️ Something went wrong. Please try again.")

    elif text.lower() == "/disconnect":
        try:
            import database as db
            import sqlite3
            conn = sqlite3.connect("trades.db")
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM users WHERE telegram_chat_id = ?", (chat_id,)).fetchone()
            if row:
                db.disconnect_telegram(row["id"])
                _reply(chat_id, "✅ Disconnected. You will no longer receive trade notifications.")
            else:
                _reply(chat_id, "No account linked to this chat.")
        except Exception as e:
            _reply(chat_id, "⚠️ Error disconnecting.")

    elif text.lower() == "/start":
        _reply(
            chat_id,
            "<b>HEXIS Trading Bot</b>\n\n"
            "To receive trade notifications:\n"
            "1. Open your HEXIS dashboard\n"
            "2. Go to Settings → Connect Telegram\n"
            "3. Copy your link code\n"
            "4. Send: <code>/connect YOUR_CODE</code>\n\n"
            "To disconnect: /disconnect"
        )


def poll_loop(stop_event: threading.Event):
    if not _TOKEN:
        log.info("No TELEGRAM_TOKEN — bot polling disabled.")
        return
    log.info("Telegram bot polling started.")
    global _OFFSET
    while not stop_event.is_set():
        try:
            data = _api("getUpdates", offset=_OFFSET, timeout=30, allowed_updates=["message"])
            for update in data.get("result", []):
                _handle_update(update)
        except Exception as e:
            log.debug(f"Poll error: {e}")
        stop_event.wait(2)
