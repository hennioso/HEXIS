<p align="center">
  <img src="static/logo.svg" alt="HEXIS Logo" width="480"/>
</p>

# HEXIS – Autonomous Crypto Agent

An autonomous futures trading agent for the **Bitunix** exchange. Trades multiple symbols in parallel using five strategies — trend-following, scalping, Fibonacci sniper, liquidity sweep orderblock, and fair value gap — with a live web dashboard, Telegram notifications, circuit breakers, and an **AI Trade Analyst** that automatically tunes parameters based on performance.

> **Status: Active Test Phase** — The agent is currently running live with real capital in a controlled test environment. Position sizes are intentionally limited while strategies are being validated and refined.

![HEXIS Dashboard](screenshot.png)

![HEXIS Terminal](screenshot_terminal.png)

---

## Features

### Trading Engine
- **Multi-symbol trading** — BTC, ETH, SOL, XRP, HYPE, ADA, BNB running in parallel
- **Five strategies** configurable per symbol (hot-swappable without restart):
  - `trend` — RSI + EMA Crossover with 5m/15m multi-timeframe filter
  - `scalp` — Bollinger Bands + RSI(7) + Volume confirmation
  - `sniper` — Fibonacci retracement entries (Fib 0.882) with partial TP cascade and Break Even stop
  - `lsob` — Liquidity Sweep + Orderblock re-entry
  - `fvg` — Fair Value Gap retest (price imbalance entries in trend direction)
- **Agent Mode** — global scanner evaluates all 7 symbols × 5 strategies (35 combos) per tick, opens only the single best-scoring setup above a configurable threshold
- **Fixed fractional risk sizing** — position size based on % account risk / SL distance
- **Break-Even stop** — LSOB and FVG positions automatically move SL to entry after covering 50% of TP distance
- **Trailing stop** — TREND and SCALP positions activate a trailing stop at 60% of TP distance, trailing at the original SL distance
- **Learning phase** — margin capped at 25 USDT for the first 10 trades

### AI Trade Analyst (Multi-Model Consensus)
- **Three-AI panel** — Claude (Opus 4.6), GPT-4o, and Gemini analyze independently in parallel, then form a consensus
  - Claude: conservative quant focus (extended thinking enabled)
  - GPT-4o: risk management and drawdown focus *(requires `OPENAI_API_KEY`)*
  - Gemini: pattern recognition and strategy correlation *(requires `GOOGLE_API_KEY`)*
- **Consensus logic** — `MIN_OPEN_SCORE` is the rounded average of all AI recommendations; per-symbol strategy changes require a majority vote (≥2 AIs)
- **Graceful degradation** — runs with Claude alone if OpenAI/Google keys are not set
- **Context-aware prompt** — includes drawdown metrics, current win/loss streak, per-strategy and per-symbol breakdown, and hourly UTC performance distribution
- **Time-of-day insight** — flags UTC hours with consistently low win rate (advisory, not auto-applied)
- **Safety guards** — never adjusts while positions are open, requires min. 5 closed trades
- **All 5 strategies** are known to the analyst — can pin or unpin any symbol to/from FVG, SNIPER, LSOB, SCALP, TREND, or AUTO

### Circuit Breakers
- **Daily Loss Guard** — pauses ALL trading when today's realized PnL drops below a configurable threshold (default: −30 USDT). Resets automatically at UTC midnight
- **Consecutive Loss Guard** — auto-disables a strategy after N consecutive SL hits (default: 4). Re-enables when the strategy books a profit
- Both circuit breakers are visible and resettable from the dashboard

### Telegram Notifications
- **Trade opened** — symbol, direction, strategy, entry, TP, SL
- **Trade closed** — exit price, PnL, status (TP / SL / manual)
- **Sniper partial TP hits** — notified for each cascade level
- **Alerts** — configurable for custom events
- Fire-and-forget (non-blocking) — never delays trade execution

### Web Dashboard
- **Secure login page** — custom HTML login with Flask session auth
- **Balance banner** — Available, In Margin, Unrealized PnL, Total (Est.)
- **Tab navigation** — Overview, Analytics, Backtest
- **Overview tab**:
  - Live symbol strip with price, 24h change, and per-symbol strategy buttons (TREND / SCALP / SNIPER / LSOB / FVG / AUTO)
  - Performance stats cards — Total PnL, ROI, Win Rate, Trades, Open Positions, Avg Win/Loss, Best/Worst Trade
  - Time filter — 1D / 5D / 7D / 14D / 30D / All
  - PnL chart + strategy distribution chart
  - Active Trades panel with real-time uPnL and manual close button
  - Trade History with pagination (10 per page)
  - **Manual Trade Entry** — add manually opened positions directly from the dashboard
- **Analytics tab**:
  - Equity curve (cumulative PnL over time)
  - Drawdown metrics
  - Per-strategy performance breakdown (trades, win rate, avg win/loss)
  - Per-symbol performance breakdown
- **Backtest tab** — run strategy backtests from the dashboard with equity curve visualization
- **Circuit Breaker banner** — prominent warning when trading is paused
- **Agent Mode toggle** — enable/disable the global scanner from the dashboard
- **Mobile responsive** — full support for phones and tablets

### Security
- Custom login page with Flask session authentication
- Credentials stored in `.env` — never in code
- API keys excluded from git via `.gitignore`
- `debug=False` enforced in production

---

## Requirements

| Requirement | Details |
|---|---|
| Python | 3.10+ |
| Bitunix account | Futures trading enabled, API key with Read + Trade permissions |
| Anthropic API key | Required for AI Trade Analyst — [console.anthropic.com](https://console.anthropic.com) |
| OpenAI API key | Optional — enables GPT-4o as second analyst — [platform.openai.com](https://platform.openai.com) |
| Google AI API key | Optional — enables Gemini as third analyst — [aistudio.google.com](https://aistudio.google.com) |
| Telegram bot | Optional — for trade notifications |

### Estimated running costs

| Service | Cost |
|---|---|
| Bitunix trading fees | ~0.02% per trade (maker/taker) |
| Anthropic API (Claude) | ~$3/month at default 4h interval with claude-opus-4-6 |
| OpenAI API (GPT-4o, optional) | ~$2–4/month at default 4h interval |
| Google AI API (Gemini, optional) | ~$1/month at default 4h interval |
| Server / VPS | Optional — can run locally; a small VPS (~$5/month) ensures 24/7 uptime |

> The $5 Anthropic free credit covers roughly 2 weeks at the default interval. To reduce costs, increase `ANALYSIS_INTERVAL_MINUTES` in `trade_analyst.py` or switch to `claude-haiku-4-5` (~$0.20/month).

---

## Setup

### 1. Clone & install dependencies

```bash
git clone https://github.com/hennioso/HEXIS.git
cd HEXIS
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Required
BITUNIX_API_KEY=your_api_key
BITUNIX_SECRET_KEY=your_secret_key
ANTHROPIC_API_KEY=your_anthropic_key

# Dashboard authentication (recommended)
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=your_password
FLASK_SECRET_KEY=generate_a_random_string

# Telegram notifications (optional)
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Circuit breakers (optional — defaults shown)
DAILY_LOSS_LIMIT_USDT=-30.0
MAX_CONSECUTIVE_LOSSES=4
```

#### How to get your Bitunix API Key

1. Create an account at [bitunix.com](https://www.bitunix.com/register?inviteCode=vefzzy) *(referral link — appreciated but not required)*
2. Top-right corner → **Avatar → API Management**
3. Click **Create API Key**, enable **Read** and **Trade** permissions
4. Copy **API Key** and **Secret Key** into `.env`

#### How to set up Telegram notifications

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into `TELEGRAM_TOKEN`
2. Send any message to your new bot, then open:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Copy the `id` from the `chat` object into `TELEGRAM_CHAT_ID`

#### How to generate a Flask Secret Key

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Initialise the database

```bash
python init_db.py
```

### 4. Start the agent

```bash
python main.py
```

### 5. Open the dashboard

```bash
python web_dashboard.py
```

Open [http://localhost:5000](http://localhost:5000) — you will be redirected to the login page if `DASHBOARD_USER` and `DASHBOARD_PASSWORD` are set in `.env`.

---

## Configuration

All settings are in `config.py`. Key parameters:

| Parameter | Default | Description |
|---|---|---|
| `SYMBOLS` | 7 symbols | BTC, ETH, SOL, XRP, BNB, HYPE, ADA |
| `STRATEGIES` | all `auto` | Agent Mode by default |
| `LEVERAGE` | 10x | Futures leverage (must be set on Bitunix per symbol) |
| `RISK_PER_TRADE` | 5% | Capital risk per trade |
| `STOP_LOSS_PCT` | 2.5% | Stop loss (trend strategy) |
| `TAKE_PROFIT_PCT` | 5.0% | Take profit (trend strategy, 2:1 R:R) |
| `SCALP_STOP_LOSS_PCT` | 0.8% | Stop loss (scalp strategy) |
| `SCALP_TAKE_PROFIT_PCT` | 1.6% | Take profit (scalp strategy, 2:1 R:R) |
| `MAX_MARGIN_TRADES` | 10 | Learning phase trade count |
| `MAX_MARGIN_USDT` | 25 USDT | Max margin per trade during learning phase |
| `DAILY_LOSS_LIMIT_USDT` | −30 USDT | Circuit breaker daily loss threshold |
| `MAX_CONSECUTIVE_LOSSES` | 4 | Consecutive losses before strategy pause |
| `LOOP_INTERVAL_SECONDS` | 15 | Price check interval |

All parameters can be overridden via environment variables in `.env`.

### Agent Mode scoring thresholds

The Agent Scanner scores all (symbol × strategy) combinations and only opens a trade when the best score exceeds `MIN_OPEN_SCORE` (default: 7):

| Strategy | Max score |
|---|---|
| SNIPER (Fibonacci 0.882) | 10 |
| LSOB (Liquidity Sweep OB) | 9 |
| SCALP (BB + RSI + Volume) | 9 |
| FVG (Fair Value Gap retest) | 9 |
| TREND (EMA 9/21/50) | 7 |

> Raising the threshold to 8+ effectively excludes TREND entries. The AI Analyst automatically tunes this threshold based on win rate.

---

## Strategies

### Trend (RSI + EMA Crossover)

Uses a **15m trend filter** + **5m entry signal**:

- **Long**: 15m bullish (EMA9 > EMA21) + 5m EMA crossover up + RSI was oversold
- **Short**: 15m bearish (EMA9 < EMA21) + 5m EMA crossover down + RSI was overbought

### Scalp (Bollinger Bands + RSI + Volume)

Mean-reversion on the **5m chart**:

- **Long**: Price at lower BB + RSI(7) < 32 + RSI turning up + volume spike
- **Short**: Price at upper BB + RSI(7) > 68 + RSI turning down + volume spike

### Sniper (Fibonacci Retracement)

Precision entries at key Fibonacci levels with a partial TP cascade:

- **Entry**: Price within 0.5% of Fib 0.882 (deep retracement into prior swing)
- **Structural SL**: Below swing low (long) / above swing high (short)
- **TP1** (Fib 0.820) → close 30% → move SL to Break Even
- **TP2** (Fib 0.650) → close 50% of remaining
- **TP3** (Fib 0.500) → close 25% of remaining — final 5% runs protected

### LSOB (Liquidity Sweep Orderblock)

Smart money entry logic:

- **Sweep**: Price wicks past a prior swing high/low (liquidity grab), then closes back inside
- **Orderblock**: The last opposing candle before the sweep impulse
- **Entry**: Price re-enters the OB zone after the sweep
- **SL**: Structurally placed beyond the sweep wick
- **TP**: Prior liquidity / swing level on the opposite side

### FVG (Fair Value Gap)

Price imbalance entry in the direction of the original impulse:

- **Bullish FVG**: Three-candle pattern where candle 3's low > candle 1's high (gap above)
- **Bearish FVG**: Three-candle pattern where candle 3's high < candle 1's low (gap below)
- **Entry**: Price retraces into the unfilled gap zone
- **Invalidation**: Gap is discarded if any subsequent candle closes beyond the gap boundary
- **SL**: 0.2% beyond the gap boundary (structural)
- **TP**: 2× the gap size from entry (2:1 R:R)
- **Trend filter**: EMA50 on 15m — LONG only above EMA50, SHORT only below

---

## AI Trade Analyst

**What it does:**
- Runs every **4 hours** (first run 30 minutes after startup)
- Reads the last 100 closed trades from the database
- Dispatches analysis **in parallel** to all configured AI models:
  - **Claude** (claude-opus-4-6, extended thinking) — always active; conservative quant perspective
  - **GPT-4o** (optional, set `OPENAI_API_KEY`) — risk management and drawdown focus
  - **Gemini** (optional, set `GOOGLE_API_KEY`) — pattern recognition and strategy correlation
- Forms a **consensus**: rounded average for `MIN_OPEN_SCORE`, majority vote (≥2 AIs) for symbol strategy changes
- Applies structured recommendations automatically

**What it adjusts:**
- `MIN_OPEN_SCORE` — the Agent Scanner threshold (bounded 5–9)
- Per-symbol strategy — pin to a specific strategy or revert to `auto` (all 5 strategies supported)

**Safety:**
- Never adjusts while any position is open
- Requires at least 5 closed trades before first analysis
- Each AI's individual reasoning + the final consensus are logged to `analyst.log`
- "No change" is always a valid recommendation
- Gracefully falls back to Claude-only analysis if OpenAI/Google keys are absent

---

## Project Structure

```
HEXIS/
├── main.py               # Entry point — launches all threads
├── config.py             # All configuration parameters
├── exchange.py           # Bitunix API connector
├── strategy.py           # Trend strategy (RSI + EMA Crossover)
├── strategy_scalp.py     # Scalp strategy (Bollinger Bands + Volume)
├── strategy_sniper.py    # Sniper strategy (Fibonacci retracement)
├── strategy_lsob.py      # LSOB strategy (Liquidity Sweep Orderblock)
├── strategy_fvg.py       # FVG strategy (Fair Value Gap retest)
├── strategy_scanner.py   # Global opportunity scanner (Agent Mode)
├── strategy_selector.py  # Per-strategy scoring functions
├── strategy_state.py     # Per-symbol strategy state (hot-swap)
├── trade_analyst.py      # AI Trade Analyst (Claude API integration)
├── circuit_breaker.py    # Daily loss guard + consecutive loss guard
├── notifications.py      # Telegram trade notifications
├── backtest.py           # Strategy backtester
├── indicators.py         # EMA, RSI, Bollinger Bands, Fibonacci
├── risk_manager.py       # Position sizing, TP/SL calculation
├── trader.py             # Order execution, position management
├── database.py           # SQLite trade history + analytics
├── web_dashboard.py      # Flask dashboard API + exchange sync
├── init_db.py            # Database initialisation script
├── templates/
│   ├── dashboard.html    # Dashboard frontend (vanilla JS + CSS)
│   └── login.html        # Login page
├── static/
│   └── logo.svg          # HEXIS logo
├── .env.example          # Environment variable template
└── requirements.txt      # Python dependencies
```

---

## Disclaimer

This agent trades real money on live markets. Use at your own risk. Past performance does not guarantee future results. Always start with small position sizes and monitor the agent closely. The AI Trade Analyst makes automated parameter changes — review `analyst.log` regularly to understand what it is adjusting and why.
