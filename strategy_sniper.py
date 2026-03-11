"""
SNIPER Strategy – Fibonacci 0.882 Retracement Entry

Logic:
  - Find swing high and swing low over the last FIB_LOOKBACK candles
  - Entry ONLY at Fibonacci 0.882 level — no RSI, no EMA filter

Long Entry (price retraced 88.2% DOWN from swing high):
  - Entry:  swing_high - 0.882 * range        (deep support, near swing low)
  - SL:     swing_low  * (1 - 0.002)          (2 ticks below structural swing low)
  - TP1:    swing_high - 0.786 * range  → close 20% + move SL to Break Even
  - TP2:    swing_high - 0.650 * range  → close 50%
  - TP3:    swing_high - 0.500 * range  → close 25%
  - 5% stays open protected by BE stop

Short Entry (price rallied 88.2% UP from swing low):
  - Entry:  swing_low  + 0.882 * range        (deep resistance, near swing high)
  - SL:     swing_high * (1 + 0.002)          (2 ticks above structural swing high)
  - TP1:    swing_low  + 0.786 * range  → close 20% + move SL to Break Even
  - TP2:    swing_low  + 0.650 * range  → close 50%
  - TP3:    swing_low  + 0.500 * range  → close 25%
  - 5% stays open protected by BE stop
"""

from dataclasses import dataclass

from indicators import klines_to_df, add_fib_indicators


# Price must be within this % of the 0.882 level to trigger
FIB_TOLERANCE = 0.003       # 0.3% — tighter, more precise entries

# Buffer beyond the structural swing point for SL placement
SL_BUFFER = 0.002           # 0.2%

# Range must be at least this % of price to avoid flat/consolidating markets
MIN_RANGE_PCT = 0.015       # 1.5% — filters out consolidation phases


@dataclass
class SniperSignal:
    direction: str      # 'long' | 'short'
    price: float        # current market price (entry)
    fib_price: float    # exact 0.882 Fibonacci price
    swing_high: float
    swing_low: float
    sl_price: float     # structural SL — always set
    tp1_price: float    # Fib 0.786 — close 20%, then SL → BE
    tp2_price: float    # Fib 0.650 — close 50%
    tp3_price: float    # Fib 0.500 — close 25%


def check_sniper_signal(
    klines_5m: list[dict],
    lookback: int = 50,
) -> SniperSignal | None:
    """
    Scans 5m candle data for a Fibonacci 0.882 SNIPER entry.
    Returns a SniperSignal or None.

    No RSI or EMA filter — purely structural price-action entry.
    """
    df = klines_to_df(klines_5m)
    df = add_fib_indicators(df, lookback=lookback)

    if len(df) < lookback + 2:
        return None

    prev = df.iloc[-2]   # last fully closed candle

    # NaN guard
    for col in ("swing_high", "swing_low", "fib_882"):
        if prev[col] != prev[col]:
            return None

    price      = float(df.iloc[-1]["close"])
    swing_high = float(prev["swing_high"])
    swing_low  = float(prev["swing_low"])
    rng        = swing_high - swing_low

    # Skip flat / very narrow range markets
    if rng / price < MIN_RANGE_PCT:
        return None

    # ------------------------------------------------------------------
    # Long: price retraced 88.2% down from swing high (near swing low)
    # ------------------------------------------------------------------
    long_entry = swing_high - 0.882 * rng
    if abs(price - long_entry) / long_entry <= FIB_TOLERANCE:
        return SniperSignal(
            direction="long",
            price=price,
            fib_price=long_entry,
            swing_high=swing_high,
            swing_low=swing_low,
            sl_price=round(swing_low * (1 - SL_BUFFER), 8),
            tp1_price=round(swing_high - 0.786 * rng, 8),
            tp2_price=round(swing_high - 0.650 * rng, 8),
            tp3_price=round(swing_high - 0.500 * rng, 8),
        )

    # ------------------------------------------------------------------
    # Short: price rallied 88.2% up from swing low (near swing high)
    # ------------------------------------------------------------------
    short_entry = swing_low + 0.882 * rng
    if abs(price - short_entry) / short_entry <= FIB_TOLERANCE:
        return SniperSignal(
            direction="short",
            price=price,
            fib_price=short_entry,
            swing_high=swing_high,
            swing_low=swing_low,
            sl_price=round(swing_high * (1 + SL_BUFFER), 8),
            tp1_price=round(swing_low + 0.786 * rng, 8),
            tp2_price=round(swing_low + 0.650 * rng, 8),
            tp3_price=round(swing_low + 0.500 * rng, 8),
        )

    return None
