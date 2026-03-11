"""
Trading Strategy: RSI + EMA Crossover with Multi-Timeframe Filter

Logic:
  15m chart  -> Determine trend direction (EMA9 vs EMA21)
  5m  chart  -> Generate entry signals (RSI + EMA crossover)

Long entry:
  - 15m: EMA9 > EMA21 (uptrend)
  - 5m:  EMA crossover upward (EMA9 crosses EMA21 from below)
  - 5m:  RSI < RSI_OVERSOLD_THRESHOLD before crossover (was oversold)

Short entry:
  - 15m: EMA9 < EMA21 (downtrend)
  - 5m:  EMA crossover downward (EMA9 crosses EMA21 from above)
  - 5m:  RSI > RSI_OVERBOUGHT_THRESHOLD before crossover (was overbought)

No trade when:
  - A position is already open
  - 15m trend contradicts the 5m signal
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd

from indicators import add_indicators, klines_to_df, get_trend_direction


RSI_OVERSOLD = 35       # Long signal: RSI was below this value
RSI_OVERBOUGHT = 65     # Short signal: RSI was above this value
RSI_CONFIRM_MIN = 30    # Long: current RSI must still be above 30
RSI_CONFIRM_MAX = 70    # Short: current RSI must still be below 70


@dataclass
class Signal:
    direction: str      # 'long' or 'short'
    price: float        # Current market price (close of last candle)
    rsi_5m: float
    ema_fast_5m: float
    ema_slow_5m: float
    trend_15m: str


def check_signal(
    klines_5m: list[dict],
    klines_15m: list[dict],
    fast_ema: int = 9,
    slow_ema: int = 21,
    rsi_period: int = 14,
) -> Optional[Signal]:
    """
    Analyses candle data and returns a Signal, or None.
    """
    # Build DataFrames and calculate indicators
    df5 = klines_to_df(klines_5m)
    df5 = add_indicators(df5, fast_ema=fast_ema, slow_ema=slow_ema, rsi_period=rsi_period)

    df15 = klines_to_df(klines_15m)
    df15 = add_indicators(df15, fast_ema=fast_ema, slow_ema=slow_ema, rsi_period=rsi_period)

    if len(df5) < slow_ema + 2 or len(df15) < slow_ema + 2:
        return None  # Not enough data

    trend_15m = get_trend_direction(df15)
    if trend_15m is None:
        return None

    # Second-to-last (closed) 5m candle for signal check
    prev = df5.iloc[-2]
    # Third-to-last for RSI history (was it oversold/overbought?)
    prev2 = df5.iloc[-3] if len(df5) >= 3 else prev

    current_price = float(df5.iloc[-1]["close"])

    # --- Long Signal ---
    if (
        trend_15m == "bull"
        and bool(prev["ema_cross_up"])                     # EMA crossover upward
        and float(prev2["rsi"]) < RSI_OVERSOLD             # previously oversold
        and float(prev["rsi"]) > RSI_CONFIRM_MIN           # RSI recovering
    ):
        return Signal(
            direction="long",
            price=current_price,
            rsi_5m=float(prev["rsi"]),
            ema_fast_5m=float(prev["ema_fast"]),
            ema_slow_5m=float(prev["ema_slow"]),
            trend_15m=trend_15m,
        )

    # --- Short Signal ---
    if (
        trend_15m == "bear"
        and bool(prev["ema_cross_down"])                   # EMA crossover downward
        and float(prev2["rsi"]) > RSI_OVERBOUGHT           # previously overbought
        and float(prev["rsi"]) < RSI_CONFIRM_MAX           # RSI turning down
    ):
        return Signal(
            direction="short",
            price=current_price,
            rsi_5m=float(prev["rsi"]),
            ema_fast_5m=float(prev["ema_fast"]),
            ema_slow_5m=float(prev["ema_slow"]),
            trend_15m=trend_15m,
        )

    return None
