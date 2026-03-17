"""
Global Opportunity Scanner – scores all (symbol × strategy) combinations
in a single pass and returns a ranked list of trading opportunities.

Used by Agent Mode: instead of each symbol-thread independently picking
a strategy, this scanner evaluates all 7 symbols × 4 strategies = 28 combos
together and surfaces the single best setup. An order is only placed when the
top-ranked combination's score crosses MIN_OPEN_SCORE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from indicators import klines_to_df
from strategy_selector import _score_sniper, _score_lsob, _score_scalp, _score_trend, _score_fvg

log = logging.getLogger(__name__)

# Minimum score required to trigger an order entry in Agent Mode.
# Max possible scores: sniper=10, lsob=9, scalp=9, trend=7
MIN_OPEN_SCORE = 7


@dataclass
class Opportunity:
    symbol:   str
    strategy: str
    score:    int
    reasons:  list[str] = field(default_factory=list)


def scan_opportunities(
    symbols:    list[str],
    klines_map: dict[str, dict[str, list]],
) -> list[Opportunity]:
    """
    Score all (symbol × strategy) combinations and return them sorted
    by score descending.

    Args:
        symbols:    Symbols to evaluate (should be those without open positions).
        klines_map: {symbol: {"5m": [...klines...], "15m": [...klines...]}}

    Returns:
        Ranked list of Opportunity objects (highest score first).
    """
    opportunities: list[Opportunity] = []

    for symbol in symbols:
        klines_5m  = klines_map[symbol]["5m"]
        klines_15m = klines_map[symbol].get("15m")
        df5m  = klines_to_df(klines_5m)
        df15m = klines_to_df(klines_15m) if klines_15m else None

        scores = {
            # SNIPER uses 15m klines — same timeframe as the entry check in main.py
            "sniper": _score_sniper(df15m if df15m is not None else df5m, df15m),
            "lsob":   _score_lsob(klines_5m),
            "scalp":  _score_scalp(df5m),
            "trend":  _score_trend(df5m, df15m),
            # FVG uses 15m klines for meaningful gap sizes
            "fvg":    _score_fvg(klines_15m if klines_15m else klines_5m, klines_15m),
        }

        for strategy, (score, reasons) in scores.items():
            opportunities.append(
                Opportunity(
                    symbol=symbol,
                    strategy=strategy,
                    score=score,
                    reasons=reasons,
                )
            )

    opportunities.sort(key=lambda o: o.score, reverse=True)
    return opportunities
