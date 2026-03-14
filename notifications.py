"""
Telegram notification module for HEXIS.

Setup:
  1. Create a bot via @BotFather → copy token
  2. Send a message to your bot, then:
     curl "https://api.telegram.org/bot<TOKEN>/getUpdates"  → copy chat_id
  3. Add to .env:
       TELEGRAM_TOKEN=<token>
       TELEGRAM_CHAT_ID=<chat_id>

If TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is not set, all calls are silent no-ops.
"""

import logging
import os
import threading

import requests

log = logging.getLogger("notify")

_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_ENABLED = bool(_TOKEN and _CHAT_ID)

if _ENABLED:
    log.info("Telegram notifications enabled.")
else:
    log.info("Telegram notifications disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set).")


def _send(text: str):
    """Fire-and-forget Telegram message. Never blocks the caller."""
    if not _ENABLED:
        return

    def _worker():
        try:
            requests.post(
                f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
                json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=8,
            )
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def send_trade_open(
    symbol: str,
    direction: str,
    strategy: str,
    entry: float,
    tp: float,
    sl: float,
    qty: float,
):
    dir_emoji = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    _send(
        f"<b>HEXIS — Trade Opened</b>\n"
        f"{dir_emoji} <b>{symbol}</b> [{strategy.upper()}]\n"
        f"Entry: <code>{entry:.4f}</code>  Qty: <code>{qty}</code>\n"
        f"TP: <code>{tp:.4f}</code>  SL: <code>{sl:.4f}</code>"
    )


def send_trade_close(
    symbol: str,
    direction: str,
    strategy: str,
    entry: float,
    exit_p: float,
    pnl: float,
    status: str,
):
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
        f"PnL: <b>{pnl_str} USDT</b>"
    )


def send_sniper_tp(symbol: str, tp_num: int, direction: str, tp_price: float, partial_pnl: float):
    pnl_str = f"+{partial_pnl:.2f}" if partial_pnl >= 0 else f"{partial_pnl:.2f}"
    _send(
        f"<b>HEXIS — SNIPER TP{tp_num}</b> ✅\n"
        f"{'LONG' if direction == 'long' else 'SHORT'} <b>{symbol}</b>\n"
        f"TP{tp_num} @ <code>{tp_price:.4f}</code>  Partial PnL: <b>{pnl_str} USDT</b>"
    )


def send_alert(title: str, message: str):
    _send(f"<b>⚠️ HEXIS — {title}</b>\n{message}")
