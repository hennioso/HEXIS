"""
Scalping-Strategie: Bollinger Bands + RSI(7) + Volume-Bestätigung

Logik (Mean-Reversion):
  Preis berührt unteres Bollinger Band  → potenzielle Übertreibung nach unten
  Preis berührt oberes Bollinger Band   → potenzielle Übertreibung nach oben

Long-Entry (Oversold Bounce):
  - Close <= unteres BB (oder bb_pct < OVERSOLD_PCT)
  - RSI(7) < RSI_OVERSOLD  (default 32)
  - Volumen > VOL_RATIO_MIN × Durchschnitt  (Bestätigung durch Aktivität)
  - RSI dreht nach oben (aktuell > vorherige Kerze)   ← Trendwechsel-Frühzeichen

Short-Entry (Overbought Rejection):
  - Close >= oberes BB (oder bb_pct > OVERBOUGHT_PCT)
  - RSI(7) > RSI_OVERBOUGHT  (default 68)
  - Volumen > VOL_RATIO_MIN × Durchschnitt
  - RSI dreht nach unten

Exit: Über TP/SL in der Order (konfigurierbar in config.py)
Empfohlene Parameter: SL 0.8%, TP 1.6% (2:1 R:R), engere Bänder als Trend-Strategie
"""

from dataclasses import dataclass
from typing import Optional

from indicators import klines_to_df, add_scalp_indicators


RSI_OVERSOLD    = 32
RSI_OVERBOUGHT  = 68
OVERSOLD_PCT    = 0.05   # bb_pct < 5%  → Preis im untersten BB-Bereich
OVERBOUGHT_PCT  = 0.95   # bb_pct > 95% → Preis im obersten BB-Bereich
VOL_RATIO_MIN   = 1.2    # Volumen muss mind. 20% über Durchschnitt liegen


@dataclass
class ScalpSignal:
    direction: str      # 'long' | 'short'
    price: float
    rsi_7: float
    bb_pct: float       # 0.0 = unteres Band, 1.0 = oberes Band
    vol_ratio: float
    bb_upper: float
    bb_lower: float


def check_scalp_signal(
    klines_5m: list[dict],
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_period: int = 7,
    vol_period: int = 20,
) -> Optional[ScalpSignal]:
    """
    Analysiert 5m-Kerzendaten und gibt ein Scalp-Signal zurück, oder None.
    Kein 15m-Trendfilter – funktioniert in Range- und Trendmärkten.
    """
    df = klines_to_df(klines_5m)
    df = add_scalp_indicators(df, bb_period=bb_period, bb_std=bb_std,
                               rsi_period=rsi_period, vol_period=vol_period)

    if len(df) < bb_period + 2:
        return None

    # Vorletzter (abgeschlossener) Balken
    prev  = df.iloc[-2]
    prev2 = df.iloc[-3]

    # NaN-Check
    for col in ["rsi_scalp", "bb_pct", "vol_ratio", "bb_upper", "bb_lower"]:
        if prev[col] != prev[col]:  # NaN check
            return None

    rsi_val   = float(prev["rsi_scalp"])
    rsi_prev  = float(prev2["rsi_scalp"])
    bb_pct    = float(prev["bb_pct"])
    vol_ratio = float(prev["vol_ratio"])
    price     = float(df.iloc[-1]["close"])

    # Volumen-Filter
    if vol_ratio < VOL_RATIO_MIN:
        return None

    # --- Long: Oversold Bounce ---
    if (
        bb_pct < OVERSOLD_PCT
        and rsi_val < RSI_OVERSOLD
        and rsi_val > rsi_prev   # RSI dreht nach oben
    ):
        return ScalpSignal(
            direction="long",
            price=price,
            rsi_7=rsi_val,
            bb_pct=bb_pct,
            vol_ratio=vol_ratio,
            bb_upper=float(prev["bb_upper"]),
            bb_lower=float(prev["bb_lower"]),
        )

    # --- Short: Overbought Rejection ---
    if (
        bb_pct > OVERBOUGHT_PCT
        and rsi_val > RSI_OVERBOUGHT
        and rsi_val < rsi_prev   # RSI dreht nach unten
    ):
        return ScalpSignal(
            direction="short",
            price=price,
            rsi_7=rsi_val,
            bb_pct=bb_pct,
            vol_ratio=vol_ratio,
            bb_upper=float(prev["bb_upper"]),
            bb_lower=float(prev["bb_lower"]),
        )

    return None
