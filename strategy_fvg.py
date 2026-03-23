"""
FVG Strategy – Fair Value Gap Retracement Entry

Logic:
  A Fair Value Gap (FVG) is a 3-candle price imbalance where the ranges of
  candles 1 and 3 do not overlap, leaving a gap that price tends to revisit.

  Bullish FVG:  candle[i+2].low  > candle[i].high  (gap above, impulse up)
  Bearish FVG:  candle[i+2].high < candle[i].low   (gap below, impulse down)

  The gap zone is treated as a support (bullish) or resistance (bearish)
  magnet. When price retraces into the unfilled gap, we enter in the
  direction of the original impulse move.

Long Entry (bullish FVG):
  - Entry:  price retraces into [c1.high, c3.low]
  - SL:     c1.high × (1 – FVG_SL_BUFFER)   (below gap bottom)
  - TP:     entry + gap_size × FVG_TP_MULTIPLIER  (2:1 R:R default)

Short Entry (bearish FVG):
  - Entry:  price rallies into [c3.high, c1.low]
  - SL:     c1.low  × (1 + FVG_SL_BUFFER)   (above gap top)
  - TP:     entry − gap_size × FVG_TP_MULTIPLIER

Trend filter: EMA50 on 15m — LONG only above EMA50, SHORT only below.
Invalidation: if any candle after the pattern closes beyond the gap
              boundary (gap fully filled), the setup is discarded.
"""

from dataclasses import dataclass

from indicators import klines_to_df, ema


# ---- Parameters ----------------------------------------------------------------

# How many candles back to scan for gap patterns
FVG_LOOKBACK      = 30

# Minimum gap size relative to current price (filters micro-imbalances)
FVG_MIN_GAP_PCT   = 0.003     # 0.3%

# Gaps older than this are considered stale
FVG_MAX_AGE       = 20

# SL placed this % beyond the gap boundary
FVG_SL_BUFFER     = 0.003     # 0.3% (widened from 0.2% — backtest showed 1.5x saves 58% of SL trades)

# TP = gap_size × this multiplier (2.0 = 2:1 R:R)
FVG_TP_MULTIPLIER = 2.0

# EMA period for the 15m trend filter
FVG_TREND_EMA     = 50


# ---- Signal dataclass ----------------------------------------------------------

@dataclass
class FVGSignal:
    direction:  str    # 'long' | 'short'
    price:      float  # current market price (entry reference)
    fvg_top:    float  # upper boundary of the gap
    fvg_bottom: float  # lower boundary of the gap
    sl_price:   float
    tp_price:   float
    gap_pct:    float  # gap size as % of price
    candle_age: int    # candles elapsed since the pattern formed


# ---- Signal detection ----------------------------------------------------------

def check_fvg_signal(
    klines: list[dict],
    lookback: int = FVG_LOOKBACK,
    klines_15m: list[dict] | None = None,
) -> FVGSignal | None:
    """
    Scans kline data for the most recent unfilled Fair Value Gap whose zone
    the current price is currently retesting.

    Returns FVGSignal or None.

    If klines_15m is provided, applies EMA50 trend filter:
      - LONG only when price > EMA50
      - SHORT only when price < EMA50
    """
    df = klines_to_df(klines)
    if len(df) < lookback + 3:
        return None

    price = float(df.iloc[-1]["close"])

    # --- EMA50 trend filter ---
    allow_long  = True
    allow_short = True
    if klines_15m:
        try:
            df15 = klines_to_df(klines_15m)
            ema50 = float(ema(df15["close"], FVG_TREND_EMA).iloc[-1])
            allow_long  = price > ema50
            ema200 = float(ema(df15["close"], 200).iloc[-1])
            allow_short = price < ema50 and price < ema200  # confirmed bearish
        except Exception:
            pass

    # Scan backwards — return the first (most recent) valid FVG where price is inside
    scan_end = max(0, len(df) - 3 - lookback)
    for i in range(len(df) - 3, scan_end - 1, -1):
        c1_high = float(df.iloc[i]["high"])
        c1_low  = float(df.iloc[i]["low"])
        c3_high = float(df.iloc[i + 2]["high"])
        c3_low  = float(df.iloc[i + 2]["low"])

        candle_age = len(df) - 1 - (i + 2)
        if candle_age > FVG_MAX_AGE:
            break  # everything older is stale — stop

        # Candles that formed after this gap (used for invalidation check)
        after = df.iloc[i + 3:]

        # ---- Bullish FVG: gap = [c1.high, c3.low], impulse direction = UP ----
        if allow_long and c3_low > c1_high:
            fvg_bottom = c1_high
            fvg_top    = c3_low
            gap_pct    = (fvg_top - fvg_bottom) / price

            if gap_pct < FVG_MIN_GAP_PCT:
                continue

            # Invalidated if any subsequent candle closed below the gap bottom
            if len(after) > 0 and float(after["close"].min()) < fvg_bottom:
                continue

            # Entry: current price is inside the unfilled gap
            if fvg_bottom <= price <= fvg_top:
                gap_size = fvg_top - fvg_bottom
                return FVGSignal(
                    direction  = "long",
                    price      = price,
                    fvg_top    = round(fvg_top,    8),
                    fvg_bottom = round(fvg_bottom, 8),
                    sl_price   = round(fvg_bottom * (1 - FVG_SL_BUFFER), 8),
                    tp_price   = round(price + gap_size * FVG_TP_MULTIPLIER, 8),
                    gap_pct    = round(gap_pct, 5),
                    candle_age = candle_age,
                )

        # ---- Bearish FVG: gap = [c3.high, c1.low], impulse direction = DOWN ----
        if allow_short and c3_high < c1_low:
            fvg_bottom = c3_high
            fvg_top    = c1_low
            gap_pct    = (fvg_top - fvg_bottom) / price

            if gap_pct < FVG_MIN_GAP_PCT:
                continue

            # Invalidated if any subsequent candle closed above the gap top
            if len(after) > 0 and float(after["close"].max()) > fvg_top:
                continue

            # Entry: current price is inside the unfilled gap
            if fvg_bottom <= price <= fvg_top:
                gap_size = fvg_top - fvg_bottom
                return FVGSignal(
                    direction  = "short",
                    price      = price,
                    fvg_top    = round(fvg_top,    8),
                    fvg_bottom = round(fvg_bottom, 8),
                    sl_price   = round(fvg_top * (1 + FVG_SL_BUFFER), 8),
                    tp_price   = round(price - gap_size * FVG_TP_MULTIPLIER, 8),
                    gap_pct    = round(gap_pct, 5),
                    candle_age = candle_age,
                )

    return None
