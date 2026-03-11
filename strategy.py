"""
Trading-Strategie: RSI + EMA Crossover mit Multi-Timeframe-Filter

Logik:
  15m Chart  -> Trendrichtung bestimmen (EMA9 vs EMA21)
  5m  Chart  -> Entry-Signale generieren (RSI + EMA-Crossover)

Long-Entry:
  - 15m: EMA9 > EMA21 (Aufwärtstrend)
  - 5m:  EMA-Crossover nach oben (EMA9 kreuzt EMA21 von unten)
  - 5m:  RSI < RSI_OVERSOLD_THRESHOLD vor dem Crossover (war überverkauft)

Short-Entry:
  - 15m: EMA9 < EMA21 (Abwärtstrend)
  - 5m:  EMA-Crossover nach unten (EMA9 kreuzt EMA21 von oben)
  - 5m:  RSI > RSI_OVERBOUGHT_THRESHOLD vor dem Crossover (war überkauft)

Kein Trade wenn:
  - Bereits eine offene Position vorhanden
  - 15m-Trend widerspricht dem 5m-Signal
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd

from indicators import add_indicators, klines_to_df, get_trend_direction


RSI_OVERSOLD = 35       # Long-Signal: RSI war unter diesem Wert
RSI_OVERBOUGHT = 65     # Short-Signal: RSI war über diesem Wert
RSI_CONFIRM_MIN = 30    # Long: aktueller RSI muss noch über 30 sein
RSI_CONFIRM_MAX = 70    # Short: aktueller RSI muss noch unter 70 sein


@dataclass
class Signal:
    direction: str      # 'long' oder 'short'
    price: float        # Aktueller Marktpreis (close der letzten Kerze)
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
    Analysiert die Kerzendaten und gibt ein Signal zurück, oder None.
    """
    # DataFrames aufbauen und Indikatoren berechnen
    df5 = klines_to_df(klines_5m)
    df5 = add_indicators(df5, fast_ema=fast_ema, slow_ema=slow_ema, rsi_period=rsi_period)

    df15 = klines_to_df(klines_15m)
    df15 = add_indicators(df15, fast_ema=fast_ema, slow_ema=slow_ema, rsi_period=rsi_period)

    if len(df5) < slow_ema + 2 or len(df15) < slow_ema + 2:
        return None  # Nicht genug Daten

    trend_15m = get_trend_direction(df15)
    if trend_15m is None:
        return None

    # Vorletzter (abgeschlossener) 5m-Balken für Signalprüfung
    prev = df5.iloc[-2]
    # Drittletzter für RSI-History (war er überverkauft/überkauft?)
    prev2 = df5.iloc[-3] if len(df5) >= 3 else prev

    current_price = float(df5.iloc[-1]["close"])

    # --- Long Signal ---
    if (
        trend_15m == "bull"
        and bool(prev["ema_cross_up"])                     # EMA-Crossover nach oben
        and float(prev2["rsi"]) < RSI_OVERSOLD             # vorher überverkauft
        and float(prev["rsi"]) > RSI_CONFIRM_MIN           # RSI erholt sich
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
        and bool(prev["ema_cross_down"])                   # EMA-Crossover nach unten
        and float(prev2["rsi"]) > RSI_OVERBOUGHT           # vorher überkauft
        and float(prev["rsi"]) < RSI_CONFIRM_MAX           # RSI dreht nach unten
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
