"""
Konfiguration des Trading Bots.
API-Keys werden aus .env geladen – niemals hardcoden!
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ---- API Credentials -------------------------------------------------------
API_KEY = os.environ["BITUNIX_API_KEY"]
SECRET_KEY = os.environ["BITUNIX_SECRET_KEY"]

# ---- Trading Symbols -------------------------------------------------------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
SYMBOL = SYMBOLS[0]  # Fallback für Einzelzugriff (z.B. Dashboard-Default)

# ---- Timeframes ------------------------------------------------------------
FAST_TF = "5m"    # Entry-Signale
SLOW_TF = "15m"   # Trend-Filter
KLINE_LIMIT = 100  # Anzahl abzurufender Kerzen

# ---- Indikatoren -----------------------------------------------------------
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14

# ---- Risk Management -------------------------------------------------------
LEVERAGE = 10              # Hebel (muss auf Bitunix für das Symbol gesetzt sein)
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.05"))    # 5% Kapitalrisiko
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.025"))     # 2.5% Stop Loss
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.050")) # 5.0% Take Profit (2:1 R:R)

# Lernphase: Für die ersten N Trades wird die Margin auf MAX_MARGIN_USDT begrenzt
MAX_MARGIN_TRADES = int(os.getenv("MAX_MARGIN_TRADES", "10"))
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "25.0"))

# ---- Strategie-Auswahl ----------------------------------------------------
# 'trend'  = RSI + EMA Crossover (Multi-Timeframe, Trend-Following)
# 'scalp'  = Bollinger Bands + RSI(7) + Volume (Mean-Reversion, engere SL/TP)
# Pro Symbol konfigurierbar – gleiche Reihenfolge wie SYMBOLS
STRATEGIES = ["trend", "trend", "scalp", "scalp"]  # BTC=trend, ETH=trend, SOL=scalp, XRP=scalp

# Scalping-spezifische Parameter (überschreiben SL/TP für Scalp-Symbole)
SCALP_STOP_LOSS_PCT    = float(os.getenv("SCALP_STOP_LOSS_PCT", "0.008"))   # 0.8%
SCALP_TAKE_PROFIT_PCT  = float(os.getenv("SCALP_TAKE_PROFIT_PCT", "0.016")) # 1.6%
SCALP_BB_PERIOD        = 20
SCALP_BB_STD           = 2.0
SCALP_RSI_PERIOD       = 7
SCALP_VOL_PERIOD       = 20

# ---- Bot-Verhalten ---------------------------------------------------------
LOOP_INTERVAL_SECONDS = 15  # Wie oft der Bot prüft (sekunden)
