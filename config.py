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

# Position sizing: margin per trade as % of total account equity (available + used + unrealized PnL)
# 7.5% means: $1171 equity → $87.88 margin → $878.80 notional at 10× leverage
POSITION_MARGIN_PCT = float(os.getenv("POSITION_MARGIN_PCT", "0.075"))

# Hard cap: margin per trade never exceeds this % of available balance (always active)
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.05"))  # 5% → max $55 margin on $1100

# Learning phase: for the first N trades, margin is additionally capped at MAX_MARGIN_USDT
MAX_MARGIN_TRADES = int(os.getenv("MAX_MARGIN_TRADES", "10"))
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "25.0"))

# ---- Strategy Selection ----------------------------------------------------
# 'trend' = RSI + EMA Crossover (multi-timeframe, trend-following)
# 'scalp' = Bollinger Bands + RSI(7) + Volume (mean-reversion, tighter SL/TP)
# Configurable per symbol – same order as SYMBOLS
STRATEGIES = ["auto", "auto", "auto", "auto", "auto", "auto", "auto"]  # Agent Mode default – strategy selector picks best per symbol

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

# ---- FVG Strategy Parameters (Fair Value Gap) ------------------------------
FVG_TF          = os.getenv("FVG_TF", "15m")             # 15m for meaningful gap sizes
FVG_KLINE_LIMIT = int(os.getenv("FVG_KLINE_LIMIT", "120"))

# ---- LSOB Strategy Parameters (Liquidity Sweep Orderblock) ----------------
LSOB_TF          = os.getenv("LSOB_TF", "15m")
LSOB_KLINE_LIMIT = int(os.getenv("LSOB_KLINE_LIMIT", "120"))
LSOB_LOOKBACK    = int(os.getenv("LSOB_LOOKBACK", "40"))   # candles to define prior swing
LSOB_SCAN_DEPTH  = int(os.getenv("LSOB_SCAN_DEPTH", "25")) # how far back to scan for sweep

# ---- Bot Behaviour ---------------------------------------------------------
LOOP_INTERVAL_SECONDS = 15  # How often the bot checks prices (seconds)

# After a position closes (TP or SL), the agent scanner will not re-enter
# the same symbol for this many seconds. Prevents immediate re-entry into
# the same setup that just lost. Set to 0 to disable.
AGENT_COOLDOWN_SECONDS = int(os.getenv("AGENT_COOLDOWN_SECONDS", "1800"))  # 30 min

# ---- Telegram Notifications ------------------------------------------------
# Optional – leave empty to disable. See notifications.py for setup instructions.
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---- Email (SMTP) ----------------------------------------------------------
# Used to send invite codes. Leave empty to disable email sending.
SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER",     "")          # sender address
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")          # app password
SMTP_FROM     = os.getenv("SMTP_FROM",     SMTP_USER)   # display sender

# ---- Crypto Payments -------------------------------------------------------
# Set at least one wallet address to enable the checkout flow.
# Each buyer receives a unique micro-amount (e.g. 49.07 USDT) so payments
# can be matched without a memo/tag.

# Tron (TRC20) — accepts USDT + USDC. No API key required.
CRYPTO_WALLET_TRX = os.getenv("CRYPTO_WALLET_TRX",
                               os.getenv("CRYPTO_WALLET_ADDRESS", ""))  # backward-compat alias

# BASE + Ethereum (ERC20) — same EVM address works for both chains.
# BASE is monitored if BASESCAN_API_KEY is set.
# Ethereum mainnet is additionally monitored if ETHERSCAN_API_KEY is set.
CRYPTO_WALLET_EVM    = os.getenv("CRYPTO_WALLET_EVM", "")
BASESCAN_API_KEY     = os.getenv("BASESCAN_API_KEY",  "")
ETHERSCAN_API_KEY    = os.getenv("ETHERSCAN_API_KEY", "")

# Solana — accepts USDT + USDC (SPL). Requires a Helius API key (free tier).
CRYPTO_WALLET_SOL = os.getenv("CRYPTO_WALLET_SOL", "")
HELIUS_API_KEY    = os.getenv("HELIUS_API_KEY",    "")

# Optional TronGrid Pro API key — increases rate limits for Tron.
TRONGRID_API_KEY  = os.getenv("TRONGRID_API_KEY", "")

# Minimum qualifying payment in USDT (inclusive). Payments below this are ignored.
CRYPTO_MIN_USDT   = float(os.getenv("CRYPTO_MIN_USDT",   "48.0"))
# Base price shown on the checkout page.
CRYPTO_PRICE_USDT = float(os.getenv("CRYPTO_PRICE_USDT", "49.0"))

# Legacy alias used in run.py start condition
CRYPTO_WALLET_ADDRESS = CRYPTO_WALLET_TRX or CRYPTO_WALLET_EVM or CRYPTO_WALLET_SOL

# ---- Circuit Breakers ------------------------------------------------------
# Pause ALL trading when today's realized PnL drops below this value (UTC day).
DAILY_LOSS_LIMIT_USDT     = float(os.getenv("DAILY_LOSS_LIMIT_USDT",     "-30.0"))
# Auto-disable a strategy after this many consecutive SL hits.
MAX_CONSECUTIVE_LOSSES    = int(os.getenv("MAX_CONSECUTIVE_LOSSES",    "4"))
