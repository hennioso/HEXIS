"""
Technische Indikatoren: EMA, RSI
Berechnet auf Basis von OHLCV-Daten der Bitunix API.
"""

import numpy as np
import pandas as pd
from typing import Optional


def klines_to_df(klines: list[dict]) -> pd.DataFrame:
    """
    Konvertiert Bitunix-Kerzendaten in einen sortierten DataFrame.
    Spalten: time, open, high, low, close, volume
    """
    df = pd.DataFrame(klines)
    df = df.rename(columns={"baseVol": "volume"})
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df = df.sort_values("time").reset_index(drop=True)
    return df


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponentieller gleitender Durchschnitt."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (RSI).
    Gibt eine Series mit Werten zwischen 0 und 100 zurück.
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_values = 100 - (100 / (1 + rs))
    return rsi_values


def add_indicators(df: pd.DataFrame, fast_ema: int = 9, slow_ema: int = 21, rsi_period: int = 14) -> pd.DataFrame:
    """
    Fügt EMA und RSI zum DataFrame hinzu.
    Spalten die hinzugefügt werden:
      ema_fast, ema_slow, rsi, ema_cross (True wenn fast > slow)
    """
    df = df.copy()
    df["ema_fast"] = ema(df["close"], fast_ema)
    df["ema_slow"] = ema(df["close"], slow_ema)
    df["rsi"] = rsi(df["close"], rsi_period)

    # True = fast EMA ist über slow EMA (bullish)
    df["ema_bull"] = df["ema_fast"] > df["ema_slow"]

    # Crossover-Signale: True nur in der Kerze, in der der Crossover passiert
    df["ema_cross_up"] = df["ema_bull"] & ~df["ema_bull"].shift(1).fillna(False)
    df["ema_cross_down"] = ~df["ema_bull"] & df["ema_bull"].shift(1).fillna(True)

    return df


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """
    Bollinger Bands.
    Gibt DataFrame mit Spalten: bb_mid, bb_upper, bb_lower, bb_width zurück.
    """
    mid = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return pd.DataFrame({
        "bb_mid":   mid,
        "bb_upper": upper,
        "bb_lower": lower,
        "bb_width": (upper - lower) / mid,  # normalisierte Breite
    })


def add_scalp_indicators(
    df: pd.DataFrame,
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_period: int = 7,
    vol_period: int = 20,
) -> pd.DataFrame:
    """
    Fügt Scalping-Indikatoren hinzu:
      bb_mid, bb_upper, bb_lower, bb_width
      rsi_scalp (kurzer RSI)
      vol_ratio (aktuelles Volumen / Durchschnittsvolumen)
      bb_pct    (Position des Close innerhalb der Bänder, 0=lower, 1=upper)
    """
    df = df.copy()
    bb = bollinger_bands(df["close"], period=bb_period, std_dev=bb_std)
    df = pd.concat([df, bb], axis=1)
    df["rsi_scalp"] = rsi(df["close"], period=rsi_period)
    df["vol_avg"]   = df["volume"].rolling(window=vol_period).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, np.nan)
    band_range = df["bb_upper"] - df["bb_lower"]
    df["bb_pct"] = (df["close"] - df["bb_lower"]) / band_range.replace(0, np.nan)
    return df


def get_trend_direction(df: pd.DataFrame) -> Optional[str]:
    """
    Gibt die aktuelle Trendrichtung zurück: 'bull', 'bear', oder None wenn unklar.
    Basiert auf dem letzten abgeschlossenen Balken (vorletzter Eintrag, da letzter noch offen ist).
    """
    if len(df) < 2:
        return None
    last = df.iloc[-2]  # vorletzter (abgeschlossener) Balken
    if last["ema_bull"]:
        return "bull"
    return "bear"
