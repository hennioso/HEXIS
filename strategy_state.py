"""
Per-symbol strategy selection — persisted to strategies.json.
Defaults come from config.STRATEGIES on first run.
Thread-safe read/write.
"""

import json
import os
import threading

import config

_STATE_FILE = "strategies.json"
_VALID = {"trend", "scalp", "sniper"}
_lock = threading.Lock()


def _defaults() -> dict:
    return dict(zip(config.SYMBOLS, config.STRATEGIES))


def load() -> dict:
    """Returns {symbol: strategy} for all configured symbols."""
    with _lock:
        data = {}
        if os.path.exists(_STATE_FILE):
            try:
                with open(_STATE_FILE) as f:
                    data = json.load(f)
            except Exception:
                data = {}

        # Fill any missing symbols from config defaults
        defaults = _defaults()
        for sym in config.SYMBOLS:
            if sym not in data:
                data[sym] = defaults.get(sym, "trend")
        return data


def get_strategy(symbol: str) -> str:
    """Returns the current strategy for a symbol."""
    return load().get(symbol, "trend")


def set_strategy(symbol: str, strategy: str) -> bool:
    """Persists a strategy change for a symbol. Returns False on invalid strategy."""
    if strategy not in _VALID:
        return False
    with _lock:
        data = {}
        if os.path.exists(_STATE_FILE):
            try:
                with open(_STATE_FILE) as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[symbol] = strategy
        with open(_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    return True
