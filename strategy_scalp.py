"""
Scalping Strategy: Bollinger Bands + RSI(7) + Volume Confirmation

Logic (Mean-Reversion):
  Price touches lower Bollinger Band  -> potential downside overextension
  Price touches upper Bollinger Band  -> potential upside overextension

Long entry (Oversold Bounce):
  - Close <= lower BB (or bb_pct < OVERSOLD_PCT)
  - RSI(7) < RSI_OVERSOLD  (default 32)
  - Volume > VOL_RATIO_MIN x average  (confirmation by activity)
  - RSI turning up (current > previous candle)  <- early reversal signal

Short entry (Overbought Rejection):
  - Close >= upper BB (or bb_pct > OVERBOUGHT_PCT)
  - RSI(7) > RSI_OVERBOUGHT  (default 68)
  - Volume > VOL_RATIO_MIN x average
  - RSI turning down

Exit: Via TP/SL in the order (configurable in config.py)
Recommended params: SL 0.8%, TP 1.6% (2:1 R:R), tighter bands than trend strategy
"""

from dataclasses import dataclass
from typing import Optional

from indicators import klines_to_df, add_scalp_indicators


RSI_OVERSOLD    = 32
RSI_OVERBOUGHT  = 68
OVERSOLD_PCT    = 0.05   # bb_pct < 5%  -> price in lowest BB zone
OVERBOUGHT_PCT  = 0.95   # bb_pct > 95% -> price in highest BB zone
VOL_RATIO_MIN   = 1.2    # Volume must be at least 20% above average


@dataclass
class ScalpSignal:
    direction: str      # 'long' | 'short'
    price: float
    rsi_7: float
    bb_pct: float       # 0.0 = lower band, 1.0 = upper band
    vol_ratio: float
    bb_upper: float
    bb_lower: float


def check_scalp_signal(
    klines_5m: list[dict],
    klines_1m: list[dict] | None = None,
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_period: int = 7,
    vol_period: int = 20,
) -> Optional[ScalpSignal]:
    """
    Analyses 5m candle data and returns a scalp signal, or None.

    If klines_1m is provided, a 1m momentum filter is applied:
    the 1m RSI(7) must be oversold and turning up (long) or overbought
    and turning down (short). This tightens entry timing significantly,
    filtering out 5m signals where the 1m chart is still moving against us.
    """
    df = klines_to_df(klines_5m)
    df = add_scalp_indicators(df, bb_period=bb_period, bb_std=bb_std,
                               rsi_period=rsi_period, vol_period=vol_period)

    if len(df) < bb_period + 2:
        return None

    # Second-to-last (closed) candle
    prev  = df.iloc[-2]
    prev2 = df.iloc[-3]

    # NaN check
    for col in ["rsi_scalp", "bb_pct", "vol_ratio", "bb_upper", "bb_lower"]:
        if prev[col] != prev[col]:
            return None

    rsi_val   = float(prev["rsi_scalp"])
    rsi_prev  = float(prev2["rsi_scalp"])
    bb_pct    = float(prev["bb_pct"])
    vol_ratio = float(prev["vol_ratio"])
    price     = float(df.iloc[-1]["close"])

    # Volume filter
    if vol_ratio < VOL_RATIO_MIN:
        return None

    direction = None

    # --- Long: Oversold Bounce ---
    if (
        bb_pct < OVERSOLD_PCT
        and rsi_val < RSI_OVERSOLD
        and rsi_val > rsi_prev   # RSI turning up
    ):
        direction = "long"

    # --- Short: Overbought Rejection ---
    elif (
        bb_pct > OVERBOUGHT_PCT
        and rsi_val > RSI_OVERBOUGHT
        and rsi_val < rsi_prev   # RSI turning down
    ):
        direction = "short"

    if not direction:
        return None

    # ── 1m momentum confirmation (optional) ──────────────────────────────────
    # The 1m RSI(7) must also show momentum in the trade direction.
    # This filters out 5m signals where the 1m chart is still trending against us.
    if klines_1m:
        try:
            df1m = klines_to_df(klines_1m)
            df1m = add_scalp_indicators(df1m, bb_period=10, bb_std=2.0,
                                         rsi_period=7, vol_period=10)
            if len(df1m) >= 12:
                rsi_1m      = float(df1m.iloc[-2]["rsi_scalp"])
                rsi_1m_prev = float(df1m.iloc[-3]["rsi_scalp"])
                if direction == "long":
                    # 1m must be oversold (<50) AND turning up — entry momentum aligned
                    if rsi_1m >= 50 or rsi_1m <= rsi_1m_prev:
                        return None
                else:
                    # 1m must be overbought (>50) AND turning down
                    if rsi_1m <= 50 or rsi_1m >= rsi_1m_prev:
                        return None
        except Exception:
            pass  # if 1m data unavailable, don't block signal

    return ScalpSignal(
        direction=direction,
        price=price,
        rsi_7=rsi_val,
        bb_pct=bb_pct,
        vol_ratio=vol_ratio,
        bb_upper=float(prev["bb_upper"]),
        bb_lower=float(prev["bb_lower"]),
    )
