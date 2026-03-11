"""
Fibonacci Retracement Strategy

Logic:
  1. Determine trend using EMA9 vs EMA21 (5m chart)
  2. Find swing high and swing low over the last FIB_LOOKBACK candles
  3. Calculate Fibonacci retracement levels (23.6%, 38.2%, 50%, 61.8%, 78.6%)
  4. Enter when price retraces to a key level (38.2%, 50%, or 61.8%) WITH confirmation

Long entry (Bullish retracement):
  - EMA9 > EMA21 (uptrend)
  - Price pulled back to 38.2%, 50%, or 61.8% Fibonacci support
  - RSI(14) between 35–55 (pulled back but not collapsed)
  - RSI turning up (reversal confirmation)

Short entry (Bearish retracement):
  - EMA9 < EMA21 (downtrend)
  - Price bounced to 38.2%, 50%, or 61.8% Fibonacci resistance
  - RSI(14) between 45–65 (bounced but not surging)
  - RSI turning down

Exit: TP/SL placed via order (configured in config.py as FIB_TAKE_PROFIT_PCT / FIB_STOP_LOSS_PCT)
"""

from dataclasses import dataclass
from typing import Optional

from indicators import klines_to_df, add_fib_indicators


# Key Fibonacci levels used for entries (the most respected in practice)
FIB_ENTRY_LEVELS = ["fib_382", "fib_500", "fib_618"]

# Price must be within this % of a Fibonacci level to trigger
FIB_TOLERANCE = 0.004   # 0.4%

# RSI thresholds
RSI_LONG_MAX  = 55   # RSI must be below this for long (pulled back)
RSI_LONG_MIN  = 30   # RSI must be above this for long (not crashed)
RSI_SHORT_MIN = 45   # RSI must be above this for short (bounced)
RSI_SHORT_MAX = 70   # RSI must be below this for short (not overbought extreme)


@dataclass
class FibSignal:
    direction: str      # 'long' | 'short'
    price: float
    rsi: float
    fib_level: str      # e.g. 'fib_618'
    fib_price: float    # actual Fibonacci price level hit
    swing_high: float
    swing_low: float


def check_fib_signal(
    klines_5m: list[dict],
    lookback: int = 50,
    rsi_period: int = 14,
) -> Optional[FibSignal]:
    """
    Analyses 5m candle data and returns a Fibonacci retracement signal, or None.
    """
    df = klines_to_df(klines_5m)
    df = add_fib_indicators(df, lookback=lookback, rsi_period=rsi_period)

    if len(df) < lookback + 2:
        return None

    prev  = df.iloc[-2]   # last closed candle
    prev2 = df.iloc[-3]   # candle before that

    # NaN check
    required = ["rsi_fib", "ema_bull", "swing_high", "swing_low",
                "fib_382", "fib_500", "fib_618"]
    for col in required:
        if prev[col] != prev[col]:
            return None

    price      = float(df.iloc[-1]["close"])
    rsi_val    = float(prev["rsi_fib"])
    rsi_prev   = float(prev2["rsi_fib"])
    ema_bull   = bool(prev["ema_bull"])
    swing_high = float(prev["swing_high"])
    swing_low  = float(prev["swing_low"])

    # Find which Fibonacci level price is currently near
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

    # --- Long: Price at Fibonacci support in uptrend ---
    if (
        ema_bull
        and RSI_LONG_MIN < rsi_val < RSI_LONG_MAX
        and rsi_val > rsi_prev   # RSI turning up (reversal confirmation)
    ):
        return FibSignal(
            direction="long",
            price=price,
            rsi=rsi_val,
            fib_level=hit_level,
            fib_price=hit_price,
            swing_high=swing_high,
            swing_low=swing_low,
        )

    # --- Short: Price at Fibonacci resistance in downtrend ---
    if (
        not ema_bull
        and RSI_SHORT_MIN < rsi_val < RSI_SHORT_MAX
        and rsi_val < rsi_prev   # RSI turning down (reversal confirmation)
    ):
        return FibSignal(
            direction="short",
            price=price,
            rsi=rsi_val,
            fib_level=hit_level,
            fib_price=hit_price,
            swing_high=swing_high,
            swing_low=swing_low,
        )

    return None
