"""
LSOB Strategy – Liquidity Sweep Orderblock
Based on: Claudius_Vertesi LSOB concept

Logic (SHORT example — LONG is mirrored):
  1. Liquidity Sweep: candle wicks ABOVE a prior swing high but CLOSES back below it
  2. 2 opposing candles: the next 2 candles after the sweep are bearish (body close < open)
  3. Orderblock zone: [open of candle_1, close of candle_2] — price has to return here
  4. Entry: when current price is back inside the OB zone
     → OB is INVALID if any later candle closes beyond the OB boundary (fully pierced)

INVALID:
  - Sweep candle closes above prior high (not a wick sweep)
  - Fewer than 2 opposing candles after the sweep
  - OB zone is invalidated (later close beyond boundary)
  - "Neuer Liquidity Sweep" above the old OB (resets setup)

SL: 0.2% beyond the sweep wick extreme (structural)
TP: prior opposite liquidity (swing low for short / swing high for long)
"""

from dataclasses import dataclass

from indicators import klines_to_df


# Minimum distance the sweep must exceed the prior swing (filters equal-high fakeouts)
MIN_SWEEP_PCT = 0.001       # 0.1%

# Minimum OB zone height as % of price
MIN_OB_PCT = 0.0005         # 0.05%

# SL buffer beyond the sweep wick
SL_BUFFER = 0.002           # 0.2%

# Minimum Risk/Reward ratio — TP must be at least this multiple of SL distance
MIN_CRV = 1.5

# How many candles back to define the "prior" swing
OB_LOOKBACK = 40

# How deep to scan for a valid sweep event
OB_SCAN_DEPTH = 25


@dataclass
class LSOBSignal:
    direction: str      # 'long' | 'short'
    price: float        # current market price (entry)
    ob_top: float       # Orderblock top boundary
    ob_bottom: float    # Orderblock bottom boundary
    sweep_price: float  # Sweep extreme (high for short, low for long)
    sl_price: float     # Structural SL — beyond the sweep wick
    tp_price: float     # Target: opposite prior liquidity


def check_lsob_signal(
    klines: list[dict],
    lookback: int = OB_LOOKBACK,
    scan_depth: int = OB_SCAN_DEPTH,
) -> LSOBSignal | None:
    """
    Scans candle data for an LSOB entry.

    SHORT: prior high swept → 2 bearish candles form OB → price retests OB zone
    LONG:  prior low swept  → 2 bullish candles form OB → price retests OB zone

    Returns LSOBSignal or None.
    """
    df = klines_to_df(klines)
    min_len = lookback + scan_depth + 5
    if len(df) < min_len:
        return None

    current_price = float(df.iloc[-1]["close"])

    # Scan backwards: sweep candle must have at least 2 candles after it,
    # so the latest possible sweep is df[-3].
    scan_start = len(df) - 3
    scan_end   = max(lookback + 1, len(df) - scan_depth - lookback)

    for i in range(scan_start, scan_end - 1, -1):
        if i < lookback + 1:
            break

        sweep = df.iloc[i]

        # Prior high/low: the N candles BEFORE the sweep candle
        prior_highs = df["high"].iloc[i - lookback : i]
        prior_lows  = df["low"].iloc[i - lookback : i]
        if len(prior_highs) < 5:
            continue

        prior_high = float(prior_highs.max())
        prior_low  = float(prior_lows.min())

        # ── BEARISH LSOB (SHORT) ─────────────────────────────────────────────
        sweep_dist_up = sweep["high"] - prior_high
        if (sweep_dist_up / prior_high >= MIN_SWEEP_PCT and
                float(sweep["close"]) < prior_high):

            if i + 2 >= len(df):
                continue

            c1 = df.iloc[i + 1]
            c2 = df.iloc[i + 2]

            # 2 opposing (bearish) candles required
            if not (float(c1["close"]) < float(c1["open"]) and
                    float(c2["close"]) < float(c2["open"])):
                continue

            ob_top    = float(c1["open"])    # top of first bearish candle body
            ob_bottom = float(c2["close"])   # bottom of second bearish candle body

            if ob_top <= ob_bottom:
                continue
            if (ob_top - ob_bottom) / ob_top < MIN_OB_PCT:
                continue

            # OB is invalidated if any later candle CLOSES above ob_top
            invalidated = False
            for j in range(i + 3, len(df) - 1):
                if float(df.iloc[j]["close"]) > ob_top:
                    invalidated = True
                    break
            if invalidated:
                continue

            # Entry: current price inside the OB zone
            if ob_bottom <= current_price <= ob_top:
                sl_price = round(float(sweep["high"]) * (1 + SL_BUFFER), 8)
                tp_price = round(float(prior_lows.min()), 8)

                # Sanity: TP below entry, SL above entry
                if tp_price >= current_price or sl_price <= current_price:
                    continue

                sl_dist = abs(sl_price - current_price)
                tp_dist = abs(current_price - tp_price)
                if sl_dist == 0 or tp_dist / sl_dist < MIN_CRV:
                    continue  # CRV too low — skip this setup

                return LSOBSignal(
                    direction="short",
                    price=current_price,
                    ob_top=ob_top,
                    ob_bottom=ob_bottom,
                    sweep_price=float(sweep["high"]),
                    sl_price=sl_price,
                    tp_price=tp_price,
                )

        # ── BULLISH LSOB (LONG) ──────────────────────────────────────────────
        sweep_dist_dn = prior_low - sweep["low"]
        if (sweep_dist_dn / prior_low >= MIN_SWEEP_PCT and
                float(sweep["close"]) > prior_low):

            if i + 2 >= len(df):
                continue

            c1 = df.iloc[i + 1]
            c2 = df.iloc[i + 2]

            # 2 opposing (bullish) candles required
            if not (float(c1["close"]) > float(c1["open"]) and
                    float(c2["close"]) > float(c2["open"])):
                continue

            ob_bottom = float(c1["open"])    # bottom of first bullish candle body
            ob_top    = float(c2["close"])   # top of second bullish candle body

            if ob_top <= ob_bottom:
                continue
            if (ob_top - ob_bottom) / ob_top < MIN_OB_PCT:
                continue

            # OB is invalidated if any later candle CLOSES below ob_bottom
            invalidated = False
            for j in range(i + 3, len(df) - 1):
                if float(df.iloc[j]["close"]) < ob_bottom:
                    invalidated = True
                    break
            if invalidated:
                continue

            # Entry: current price inside the OB zone
            if ob_bottom <= current_price <= ob_top:
                sl_price = round(float(sweep["low"]) * (1 - SL_BUFFER), 8)
                tp_price = round(float(prior_highs.max()), 8)

                # Sanity: TP above entry, SL below entry
                if tp_price <= current_price or sl_price >= current_price:
                    continue

                sl_dist = abs(current_price - sl_price)
                tp_dist = abs(tp_price - current_price)
                if sl_dist == 0 or tp_dist / sl_dist < MIN_CRV:
                    continue  # CRV too low — skip this setup

                return LSOBSignal(
                    direction="long",
                    price=current_price,
                    ob_top=ob_top,
                    ob_bottom=ob_bottom,
                    sweep_price=float(sweep["low"]),
                    sl_price=sl_price,
                    tp_price=tp_price,
                )

    return None
