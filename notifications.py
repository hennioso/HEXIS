"""
Telegram notification module for HEXIS.

Global bot notifications go to TELEGRAM_CHAT_ID (owner).
Per-user notifications go to the user's linked telegram_chat_id from DB.
"""

import logging
import os
import threading

import requests

log = logging.getLogger("notify")

_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_ENABLED = bool(_TOKEN)

if _TOKEN and _CHAT_ID:
    log.info("Telegram notifications enabled.")
else:
    log.info("Telegram notifications disabled (TELEGRAM_TOKEN not set).")


def _send(text: str, chat_id: str = None):
    """Fire-and-forget Telegram message. Sends to chat_id if given, else global."""
    if not _ENABLED:
        return
    target = chat_id or _CHAT_ID
    if not target:
        return

    def _worker():
        try:
            requests.post(
                f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
                json={"chat_id": target, "text": text, "parse_mode": "HTML"},
                timeout=8,
            )
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def _get_chat_id(user_id) -> str:
    """Look up the user's linked Telegram chat_id from DB (or fall back to global)."""
    if user_id is not None:
        try:
            import database as db
            cid = db.get_telegram_chat_id(user_id)
            if cid:
                return cid
        except Exception:
            pass
    return _CHAT_ID


def send_trade_open(symbol, direction, strategy, entry, tp, sl, qty, user_id=None):
    dir_emoji = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    _send(
        f"<b>HEXIS — Trade Opened</b>\n"
        f"{dir_emoji} <b>{symbol}</b> [{strategy.upper()}]\n"
        f"Entry: <code>{entry:.4f}</code>  Qty: <code>{qty}</code>\n"
        f"TP: <code>{tp:.4f}</code>  SL: <code>{sl:.4f}</code>",
        chat_id=_get_chat_id(user_id),
    )


def send_trade_close(symbol, direction, strategy, entry, exit_p, pnl, status, user_id=None):
    if status == "tp_hit":
        result_emoji = "✅ TP Hit"
    elif status == "sl_hit":
        result_emoji = "❌ SL Hit"
    else:
        result_emoji = "⚠️ Closed"
    pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
    dir_str  = "LONG" if direction == "long" else "SHORT"
    _send(
        f"<b>HEXIS — Trade Closed</b> {result_emoji}\n"
        f"{dir_str} <b>{symbol}</b> [{strategy.upper()}]\n"
        f"Entry: <code>{entry:.4f}</code> → Exit: <code>{exit_p:.4f}</code>\n"
        f"PnL: <b>{pnl_str} USD</b>",
        chat_id=_get_chat_id(user_id),
    )


def send_sniper_tp(symbol, tp_num, direction, tp_price, partial_pnl, user_id=None):
    pnl_str = f"+{partial_pnl:.2f}" if partial_pnl >= 0 else f"{partial_pnl:.2f}"
    _send(
        f"<b>HEXIS — SNIPER TP{tp_num}</b> ✅\n"
        f"{'LONG' if direction == 'long' else 'SHORT'} <b>{symbol}</b>\n"
        f"TP{tp_num} @ <code>{tp_price:.4f}</code>  Partial PnL: <b>{pnl_str} USD</b>",
        chat_id=_get_chat_id(user_id),
    )


def send_alert(title: str, message: str, user_id=None):
    _send(
        f"<b>⚠️ HEXIS — {title}</b>\n{message}",
        chat_id=_get_chat_id(user_id),
    )
