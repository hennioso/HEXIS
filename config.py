"""
HEXIS configuration.
API keys are loaded from .env – never hardcode them!
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ---- API Credentials -------------------------------------------------------
API_KEY = os.environ["BITUNIX_API_KEY"]
SECRET_KEY = os.environ["BITUNIX_SECRET_KEY"]

# ---- Trading Symbols -------------------------------------------------------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "HYPEUSDT", "ADAUSDT"]
SYMBOL = SYMBOLS[0]  # Fallback for single-symbol access (e.g. dashboard default)

# ---- Timeframes ------------------------------------------------------------
FAST_TF = "5m"     # Entry signals
SLOW_TF = "15m"    # Trend filter
KLINE_LIMIT = 100  # Number of candles to fetch

# ---- Indicators ------------------------------------------------------------
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14

# ---- Risk Management -------------------------------------------------------
LEVERAGE = 10              # Leverage (must be set on Bitunix for the symbol)
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.05"))    # 5% capital risk per trade
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.025"))     # 2.5% stop loss
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.050")) # 5.0% take profit (2:1 R:R)

# Learning phase: for the first N trades, margin is capped at MAX_MARGIN_USDT
MAX_MARGIN_TRADES = int(os.getenv("MAX_MARGIN_TRADES", "10"))
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "25.0"))

# ---- Strategy Selection ----------------------------------------------------
# 'trend' = RSI + EMA Crossover (multi-timeframe, trend-following)
# 'scalp' = Bollinger Bands + RSI(7) + Volume (mean-reversion, tighter SL/TP)
# Configurable per symbol – same order as SYMBOLS
STRATEGIES = ["trend", "trend", "scalp", "scalp", "scalp", "scalp", "scalp"]  # BTC=trend, ETH=trend, SOL/XRP/BNB/HYPE/ADA=scalp

# Scalping-specific parameters (override SL/TP for scalp symbols)
SCALP_STOP_LOSS_PCT    = float(os.getenv("SCALP_STOP_LOSS_PCT", "0.008"))   # 0.8%
SCALP_TAKE_PROFIT_PCT  = float(os.getenv("SCALP_TAKE_PROFIT_PCT", "0.016")) # 1.6%
SCALP_BB_PERIOD        = 20
SCALP_BB_STD           = 2.0
SCALP_RSI_PERIOD       = 7
SCALP_VOL_PERIOD       = 20

# ---- SNIPER Strategy Parameters (Fibonacci Retracement) -------------------
SNIPER_TF           = os.getenv("SNIPER_TF", "15m")                             # 15m candles for meaningful swings
SNIPER_KLINE_LIMIT  = int(os.getenv("SNIPER_KLINE_LIMIT", "120"))               # Fetch 120 candles (covers lookback + buffer)
FIB_LOOKBACK        = int(os.getenv("FIB_LOOKBACK", "100"))                     # 100 × 15m = ~25 hours of swing history
FIB_STOP_LOSS_PCT   = float(os.getenv("FIB_STOP_LOSS_PCT",  "0.015"))           # 1.5% SL (standard levels)
FIB_TAKE_PROFIT_PCT = float(os.getenv("FIB_TAKE_PROFIT_PCT", "0.030"))          # 3.0% TP (2:1 R:R)
# Deep levels (0.882): SL is placed structurally at swing low/high ± 0.2%

# ---- Bot Behaviour ---------------------------------------------------------
LOOP_INTERVAL_SECONDS = 15  # How often the bot checks prices (seconds)
