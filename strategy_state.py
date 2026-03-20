"""
Per-symbol strategy selection — persisted per user.
Admin (user_id=None or 1) uses strategies.json (controls the live bot).
Other users get their own strategies_{user_id}.json (view only).
Thread-safe read/write.
"""

import json
import os
import threading

import config

_VALID = {"trend", "scalp", "sniper", "lsob", "fvg", "auto"}
_lock  = threading.Lock()

ADMIN_FILE = "strategies.json"   # legacy / bot uses this


def _file(user_id=None) -> str:
    if user_id is None or user_id == 1:
        return ADMIN_FILE
    return f"strategies_{user_id}.json"


def _defaults() -> dict:
    return dict(zip(config.SYMBOLS, config.STRATEGIES))


def load(user_id=None) -> dict:
    """Returns {symbol: strategy} for the given user (falls back to admin defaults)."""
    with _lock:
        path = _file(user_id)
        data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception:
                data = {}

        defaults = _defaults()
        for sym in config.SYMBOLS:
            if sym not in data:
                # Non-admin: start from admin's current selection as default
                if user_id and user_id != 1 and os.path.exists(ADMIN_FILE):
                    try:
                        admin = json.load(open(ADMIN_FILE))
                        data[sym] = admin.get(sym, defaults.get(sym, "trend"))
                    except Exception:
                        data[sym] = defaults.get(sym, "trend")
                else:
                    data[sym] = defaults.get(sym, "trend")
        return data


def get_strategy(symbol: str, user_id=None) -> str:
    """Returns the current strategy for a symbol."""
    return load(user_id).get(symbol, "trend")


def set_strategy(symbol: str, strategy: str, user_id=None) -> bool:
    """Persists a strategy change for a symbol. Returns False on invalid strategy."""
    if strategy not in _VALID:
        return False
    with _lock:
        path = _file(user_id)
        data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[symbol] = strategy
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    return True
