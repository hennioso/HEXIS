"""
Technical indicators: EMA, RSI, Bollinger Bands
Calculated from Bitunix API OHLCV kline data.
"""

import numpy as np
import pandas as pd
from typing import Optional


def klines_to_df(klines: list[dict]) -> pd.DataFrame:
    """
    Converts Bitunix kline data into a sorted DataFrame.
    Columns: time, open, high, low, close, volume
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
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (RSI).
    Returns a Series with values between 0 and 100.
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
    Adds EMA and RSI columns to the DataFrame.
    Added columns: ema_fast, ema_slow, rsi, ema_bull, ema_cross_up, ema_cross_down
    """
    df = df.copy()
    df["ema_fast"] = ema(df["close"], fast_ema)
    df["ema_slow"] = ema(df["close"], slow_ema)
    df["rsi"] = rsi(df["close"], rsi_period)

    # True = fast EMA is above slow EMA (bullish)
    df["ema_bull"] = df["ema_fast"] > df["ema_slow"]

    # Crossover signals: True only in the candle where the crossover occurs
    # fill_value=False avoids fillna downcasting FutureWarning
    prev_bull = df["ema_bull"].shift(1, fill_value=False)
    df["ema_cross_up"]   = df["ema_bull"] & ~prev_bull
    df["ema_cross_down"] = ~df["ema_bull"] & prev_bull

    return df


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """
    Bollinger Bands.
    Returns DataFrame with columns: bb_mid, bb_upper, bb_lower, bb_width
    """
    mid = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return pd.DataFrame({
        "bb_mid":   mid,
        "bb_upper": upper,
        "bb_lower": lower,
        "bb_width": (upper - lower) / mid,  # normalized band width
    })


def add_scalp_indicators(
    df: pd.DataFrame,
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_period: int = 7,
    vol_period: int = 20,
) -> pd.DataFrame:
    """
    Adds scalping indicators to the DataFrame:
      bb_mid, bb_upper, bb_lower, bb_width
      rsi_scalp  (short-period RSI)
      vol_ratio  (current volume / average volume)
      bb_pct     (close position within bands: 0=lower, 1=upper)
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


def add_fib_indicators(
    df: pd.DataFrame,
    lookback: int = 50,
    rsi_period: int = 14,
) -> pd.DataFrame:
    """
    Adds Fibonacci retracement indicators to the DataFrame.
      swing_high  – highest high in the last `lookback` candles
      swing_low   – lowest low in the last `lookback` candles
      fib_236 / fib_382 / fib_500 / fib_618 / fib_786  – retracement levels
      rsi_fib     – RSI for entry confirmation
      ema_fast / ema_slow / ema_bull – trend direction filter
    """
    df = df.copy()
    df["swing_high"] = df["high"].rolling(window=lookback).max()
    df["swing_low"]  = df["low"].rolling(window=lookback).min()

    rng = df["swing_high"] - df["swing_low"]
    df["fib_236"] = df["swing_high"] - 0.236 * rng
    df["fib_382"] = df["swing_high"] - 0.382 * rng
    df["fib_500"] = df["swing_high"] - 0.500 * rng
    df["fib_618"] = df["swing_high"] - 0.618 * rng
    df["fib_650"] = df["swing_high"] - 0.650 * rng
    df["fib_786"] = df["swing_high"] - 0.786 * rng
    df["fib_882"] = df["swing_high"] - 0.882 * rng
    df["fib_941"] = df["swing_high"] - 0.941 * rng

    df["rsi_fib"]  = rsi(df["close"], period=rsi_period)
    df["ema_fast"] = ema(df["close"], 9)
    df["ema_slow"] = ema(df["close"], 21)
    df["ema_bull"] = df["ema_fast"] > df["ema_slow"]
    return df


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range (ATR) — measures recent volatility.
    Uses EWM smoothing (same as Wilder's method with com=period-1).
    """
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()


def get_trend_direction(df: pd.DataFrame) -> Optional[str]:
    """
    Returns current trend direction: 'bull', 'bear', or None if unclear.
    Based on the last closed candle (second-to-last row, as the last is still open).
    """
    if len(df) < 2:
        return None
    last = df.iloc[-2]  # second-to-last (closed) candle
    if last["ema_bull"]:
        return "bull"
    return "bear"
