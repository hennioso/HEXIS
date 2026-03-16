"""
Circuit Breaker guards for HEXIS.

DailyLossGuard
  Halts all new trade openings when today's realized PnL drops below
  DAILY_LOSS_LIMIT_USDT (e.g. -30). Resets automatically at midnight UTC.

ConsecutiveLossGuard
  Disables a strategy after MAX_CONSECUTIVE_LOSSES SL hits in a row.
  Resets when the strategy books a winning trade, or via the dashboard.

Usage:
  import circuit_breaker
  circuit_breaker.init(daily_limit_usdt=-30.0, max_consecutive_losses=4)

  # Before opening a trade:
  ok, reason = circuit_breaker.is_trading_allowed(strategy="sniper")
  if not ok:
      log.warning(reason)
      return None

  # After a trade closes:
  circuit_breaker.record_trade(strategy="sniper", pnl=-5.2)
"""

import logging
import threading
from datetime import date, timezone, datetime

import database as db
import notifications

log = logging.getLogger("circuit")


# ---------------------------------------------------------------------------
# Daily Loss Guard
# ---------------------------------------------------------------------------

class _DailyLossGuard:
    def __init__(self, limit_usdt: float):
        self.limit_usdt      = limit_usdt
        self._tripped        = False
        self._manually_reset = False   # skip DB re-check until next UTC day
        self._lock           = threading.Lock()
        self._last_date: date | None = None

    def _reset_if_new_day(self):
        today = datetime.now(tz=timezone.utc).date()
        if self._last_date != today:
            if self._tripped:
                log.info("DailyLossGuard: new UTC day — resetting.")
            self._tripped        = False
            self._manually_reset = False
            self._last_date      = today

    def is_allowed(self) -> tuple[bool, str]:
        with self._lock:
            self._reset_if_new_day()
            if self._tripped:
                return False, (
                    f"Daily loss limit {self.limit_usdt:.0f} USDT reached — "
                    f"trading paused until midnight UTC."
                )
            # After a manual reset, skip the DB check for the rest of this day
            if self._manually_reset:
                return True, ""
            today_pnl = db.get_today_pnl()
            if today_pnl <= self.limit_usdt:
                self._tripped = True
                msg = (
                    f"Daily loss limit hit: {today_pnl:.2f} USDT "
                    f"(limit: {self.limit_usdt:.0f} USDT)"
                )
                log.warning(f"DailyLossGuard TRIPPED — {msg}")
                notifications.send_alert("Daily Loss Limit Reached", msg)
                return False, msg
            return True, ""

    def reset(self):
        with self._lock:
            self._tripped        = False
            self._manually_reset = True   # prevent immediate re-trip from DB check
            log.info("DailyLossGuard manually reset — DB check suppressed for today.")

    def status(self) -> dict:
        with self._lock:
            self._reset_if_new_day()
            return {
                "tripped":    self._tripped,
                "today_pnl":  db.get_today_pnl(),
                "limit_usdt": self.limit_usdt,
            }


# ---------------------------------------------------------------------------
# Consecutive Loss Guard
# ---------------------------------------------------------------------------

class _ConsecutiveLossGuard:
    def __init__(self, max_losses: int):
        self.max_losses = max_losses
        self._counts:   dict[str, int] = {}   # strategy → consecutive losses
        self._disabled: set[str]       = set()
        self._lock = threading.Lock()

    def record(self, strategy: str, pnl: float):
        if not strategy or strategy in ("manual", "None", None):
            return
        with self._lock:
            if pnl > 0:
                if strategy in self._disabled:
                    log.info(
                        f"ConsecutiveLossGuard: {strategy.upper()} booked a win — re-enabled."
                    )
                    notifications.send_alert(
                        "Strategy Re-enabled",
                        f"{strategy.upper()} booked a profit ({pnl:+.2f} USDT) — strategy is active again.",
                    )
                self._counts[strategy]  = 0
                self._disabled.discard(strategy)
            else:
                self._counts[strategy] = self._counts.get(strategy, 0) + 1
                n = self._counts[strategy]
                if n >= self.max_losses and strategy not in self._disabled:
                    self._disabled.add(strategy)
                    msg = (
                        f"{strategy.upper()} disabled after {n} consecutive losses. "
                        f"Re-enable from the dashboard."
                    )
                    log.warning(f"ConsecutiveLossGuard: {msg}")
                    notifications.send_alert("Strategy Auto-Disabled", msg)

    def is_allowed(self, strategy: str) -> tuple[bool, str]:
        with self._lock:
            if strategy in self._disabled:
                n = self._counts.get(strategy, 0)
                return False, (
                    f"{strategy.upper()} auto-disabled after {n} consecutive losses. "
                    f"Re-enable from the dashboard."
                )
            return True, ""

    def reset(self, strategy: str | None = None):
        with self._lock:
            if strategy:
                self._disabled.discard(strategy)
                self._counts.pop(strategy, None)
                log.info(f"ConsecutiveLossGuard: {strategy.upper()} manually reset.")
            else:
                self._disabled.clear()
                self._counts.clear()
                log.info("ConsecutiveLossGuard: all strategies reset.")

    def status(self) -> dict:
        with self._lock:
            return {
                "max_losses": self.max_losses,
                "counts":     dict(self._counts),
                "disabled":   list(self._disabled),
            }


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_daily:  _DailyLossGuard       | None = None
_closs:  _ConsecutiveLossGuard | None = None


def init(daily_limit_usdt: float, max_consecutive_losses: int):
    global _daily, _closs
    _daily = _DailyLossGuard(daily_limit_usdt)
    _closs = _ConsecutiveLossGuard(max_consecutive_losses)
    log.info(
        f"Circuit breakers armed — "
        f"daily limit: {daily_limit_usdt:.0f} USDT, "
        f"max consecutive losses: {max_consecutive_losses}"
    )


def is_trading_allowed(strategy: str = "") -> tuple[bool, str]:
    if _daily:
        ok, reason = _daily.is_allowed()
        if not ok:
            return False, reason
    if strategy and _closs:
        ok, reason = _closs.is_allowed(strategy)
        if not ok:
            return False, reason
    return True, ""


def record_trade(strategy: str, pnl: float):
    if _closs:
        _closs.record(strategy, pnl)


def reset(strategy: str | None = None):
    """Reset guards. strategy=None resets everything."""
    if _daily:
        _daily.reset()
    if _closs:
        _closs.reset(strategy)


def get_status() -> dict:
    return {
        "daily":       _daily.status() if _daily else {},
        "consecutive": _closs.status() if _closs else {},
    }
