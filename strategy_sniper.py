"""
SNIPER Strategy – Fibonacci Retracement with Smart Stop Loss

Logic:
  1. Determine trend using EMA9 vs EMA21 (5m chart)
  2. Find swing high and swing low over the last FIB_LOOKBACK candles
  3. Calculate Fibonacci retracement levels
  4. Enter when price retraces to a key level WITH RSI confirmation

Standard levels (38.2%, 50%, 61.8%):
  - SL calculated by RiskManager (percentage-based)

Deep levels (88.2%, 94.1%) — "Sniper entries":
  - Long: SL placed just below the swing low (1.0 level) — tight, structural SL
  - Short: SL placed just above the swing high (1.0 level)
  - TP scales with actual SL distance to maintain 2:1 R:R

Long entry:
  - EMA9 > EMA21 (uptrend)
  - Price at 38.2%, 50%, 61.8%, 88.2%, or 94.1% Fibonacci support
  - RSI(14) between 30–55 and turning up

Short entry:
  - EMA9 < EMA21 (downtrend)
  - Price at 38.2%, 50%, 61.8%, 88.2%, or 94.1% Fibonacci resistance
  - RSI(14) between 45–70 and turning down
"""

from dataclasses import dataclass
from typing import Optional

from indicators import klines_to_df, add_fib_indicators


# Standard Fibonacci levels for normal entries
FIB_STANDARD_LEVELS = ["fib_382", "fib_500", "fib_618"]

# Deep levels — sniper entries with structural SL
FIB_DEEP_LEVELS = ["fib_882", "fib_941"]

# All entry levels
FIB_ENTRY_LEVELS = FIB_STANDARD_LEVELS + FIB_DEEP_LEVELS

# Price must be within this % of a Fibonacci level to trigger
FIB_TOLERANCE = 0.004   # 0.4%

# Buffer below swing low (long) / above swing high (short) for deep-level SL
SL_STRUCTURAL_BUFFER = 0.002  # 0.2%

# RSI thresholds
RSI_LONG_MAX  = 55
RSI_LONG_MIN  = 30
RSI_SHORT_MIN = 45
RSI_SHORT_MAX = 70


@dataclass
class SniperSignal:
    direction: str          # 'long' | 'short'
    price: float
    rsi: float
    fib_level: str          # e.g. 'fib_618', 'fib_882'
    fib_price: float        # actual Fibonacci price level hit
    swing_high: float
    swing_low: float
    sl_price: Optional[float] = None  # set for deep levels (0.882 / 0.941), None for standard


def check_sniper_signal(
    klines_5m: list[dict],
    lookback: int = 50,
    rsi_period: int = 14,
) -> Optional[SniperSignal]:
    """
    Analyses 5m candle data and returns a SNIPER signal, or None.
    """
    df = klines_to_df(klines_5m)
    df = add_fib_indicators(df, lookback=lookback, rsi_period=rsi_period)

    if len(df) < lookback + 2:
        return None

    prev  = df.iloc[-2]   # last closed candle
    prev2 = df.iloc[-3]

    required = ["rsi_fib", "ema_bull", "swing_high", "swing_low",
                "fib_382", "fib_500", "fib_618", "fib_882", "fib_941"]
    for col in required:
        if prev[col] != prev[col]:
            return None

    price      = float(df.iloc[-1]["close"])
    rsi_val    = float(prev["rsi_fib"])
    rsi_prev   = float(prev2["rsi_fib"])
    ema_bull   = bool(prev["ema_bull"])
    swing_high = float(prev["swing_high"])
    swing_low  = float(prev["swing_low"])

    # Find which Fibonacci level price is near
    hit_level = None
    hit_price = None
    for level in FIB_ENTRY_LEVELS:
        fib_val = float(prev[level])
        if abs(price - fib_val) / fib_val <= FIB_TOLERANCE:
            hit_level = level
            hit_price = fib_val
            break

    if hit_level is None:
        return None

    # Compute structural SL for deep levels
    sl_price = None
    if hit_level in FIB_DEEP_LEVELS:
        sl_price = swing_low * (1 - SL_STRUCTURAL_BUFFER)   # long SL: just below swing low

    # --- Long: Fibonacci support in uptrend ---
    if (
        ema_bull
        and RSI_LONG_MIN < rsi_val < RSI_LONG_MAX
        and rsi_val > rsi_prev
    ):
        return SniperSignal(
            direction="long",
            price=price,
            rsi=rsi_val,
            fib_level=hit_level,
            fib_price=hit_price,
            swing_high=swing_high,
            swing_low=swing_low,
            sl_price=sl_price,
        )

    # --- Short: Fibonacci resistance in downtrend ---
    # For shorts, structural SL is above swing high
    if hit_level in FIB_DEEP_LEVELS:
        sl_price = swing_high * (1 + SL_STRUCTURAL_BUFFER)

    if (
        not ema_bull
        and RSI_SHORT_MIN < rsi_val < RSI_SHORT_MAX
        and rsi_val < rsi_prev
    ):
        return SniperSignal(
            direction="short",
            price=price,
            rsi=rsi_val,
            fib_level=hit_level,
            fib_price=hit_price,
            swing_high=swing_high,
            swing_low=swing_low,
            sl_price=sl_price,
        )

    return None
