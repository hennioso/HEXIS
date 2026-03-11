"""
HEXIS Backtester
Replays historical klines through the strategy and simulates trades.

Usage:
  python backtest.py                         # BTC trend, last 7 days
  python backtest.py --symbol XRPUSDT --strategy scalp --days 14
  python backtest.py --symbol SOLUSDT --strategy scalp --days 30
"""

import argparse

import config
from exchange import BitunixClient
from strategy import check_signal
from strategy_scalp import check_scalp_signal
from risk_manager import RiskManager


def run_backtest(symbol: str, strategy: str, days: int = 7):
    client = BitunixClient(config.API_KEY, config.SECRET_KEY)

    print(f"\n{'=' * 60}")
    print(f"  HEXIS Backtester")
    print(f"  Symbol:   {symbol}")
    print(f"  Strategy: {strategy.upper()}")
    print(f"  Period:   ~{days} days")
    print(f"{'=' * 60}\n")

    # Fetch as many 5m candles as possible (max 1000)
    limit_5m = min(days * 288, 1000)  # 288 x 5m candles per day
    klines_5m = client.get_klines(symbol, "5m", limit=limit_5m)

    if len(klines_5m) < 60:
        print("Not enough data to run backtest (need at least 60 candles).")
        return

    klines_15m = []
    if strategy == "trend":
        limit_15m = min(days * 96, 500)
        klines_15m = client.get_klines(symbol, "15m", limit=limit_15m)
        if len(klines_15m) < 30:
            print("Not enough 15m data for trend strategy.")
            return

    sl_pct = config.STOP_LOSS_PCT if strategy == "trend" else config.SCALP_STOP_LOSS_PCT
    tp_pct = config.TAKE_PROFIT_PCT if strategy == "trend" else config.SCALP_TAKE_PROFIT_PCT

    rm = RiskManager(
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=sl_pct,
        take_profit_pct=tp_pct,
        leverage=config.LEVERAGE,
        max_margin_usdt=99999,   # disable learning phase cap
        max_margin_trades=99999,
    )

    balance = 1000.0  # simulated starting balance in USDT
    trades = []
    in_trade = False
    entry_price = tp_price = sl_price = 0.0
    direction = ""

    for i in range(50, len(klines_5m)):
        candle = klines_5m[i]
        high  = float(candle.get("high",  candle.get("h", 0)))
        low   = float(candle.get("low",   candle.get("l", 0)))

        if in_trade:
            if direction == "long":
                if high >= tp_price:
                    pnl_pct = (tp_price - entry_price) / entry_price * config.LEVERAGE
                    balance *= (1 + pnl_pct * config.RISK_PER_TRADE)
                    trades.append({"result": "tp_hit", "pnl_pct": pnl_pct * 100})
                    in_trade = False
                elif low <= sl_price:
                    pnl_pct = (sl_price - entry_price) / entry_price * config.LEVERAGE
                    balance *= (1 + pnl_pct * config.RISK_PER_TRADE)
                    trades.append({"result": "sl_hit", "pnl_pct": pnl_pct * 100})
                    in_trade = False
            else:  # short
                if low <= tp_price:
                    pnl_pct = (entry_price - tp_price) / entry_price * config.LEVERAGE
                    balance *= (1 + pnl_pct * config.RISK_PER_TRADE)
                    trades.append({"result": "tp_hit", "pnl_pct": pnl_pct * 100})
                    in_trade = False
                elif high >= sl_price:
                    pnl_pct = (entry_price - sl_price) / entry_price * config.LEVERAGE
                    balance *= (1 + pnl_pct * config.RISK_PER_TRADE)
                    trades.append({"result": "sl_hit", "pnl_pct": pnl_pct * 100})
                    in_trade = False
            continue

        # Check for entry signal
        window_5m = klines_5m[:i + 1]
        sig = None

        if strategy == "scalp":
            sig = check_scalp_signal(
                klines_5m=window_5m,
                bb_period=config.SCALP_BB_PERIOD,
                bb_std=config.SCALP_BB_STD,
                rsi_period=config.SCALP_RSI_PERIOD,
                vol_period=config.SCALP_VOL_PERIOD,
            )
        else:
            window_15m = klines_15m[:max(1, i // 3 + 1)]
            sig = check_signal(
                klines_5m=window_5m,
                klines_15m=window_15m,
                fast_ema=config.EMA_FAST,
                slow_ema=config.EMA_SLOW,
                rsi_period=config.RSI_PERIOD,
            )

        if sig:
            params = rm.calculate(sig.direction, sig.price, balance, len(trades))
            if params:
                entry_price = float(params.entry_price)
                tp_price    = float(params.tp_price)
                sl_price    = float(params.sl_price)
                direction   = sig.direction
                in_trade    = True

    # ---- Results ----
    if not trades:
        print("No trades generated in this period.")
        return

    wins   = [t for t in trades if t["result"] == "tp_hit"]
    losses = [t for t in trades if t["result"] == "sl_hit"]
    win_rate   = len(wins) / len(trades) * 100
    avg_win    = sum(t["pnl_pct"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss   = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    total_pnl  = sum(t["pnl_pct"] for t in trades)
    gross_win  = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses)) or 1
    profit_factor = gross_win / gross_loss

    print(f"  Total trades:    {len(trades)}  ({len(wins)} wins / {len(losses)} losses)")
    print(f"  Win Rate:        {win_rate:.1f}%")
    print(f"  Avg Win:         +{avg_win:.2f}%")
    print(f"  Avg Loss:        {avg_loss:.2f}%")
    print(f"  Total PnL:       {total_pnl:+.2f}%")
    print(f"  Profit Factor:   {profit_factor:.2f}")
    print(f"  Final Balance:   ${balance:.2f}  (start: $1000.00)")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HEXIS Backtester")
    parser.add_argument("--symbol",   default="BTCUSDT",
                        help="Trading symbol (default: BTCUSDT)")
    parser.add_argument("--strategy", default="trend", choices=["trend", "scalp"],
                        help="Strategy to test (default: trend)")
    parser.add_argument("--days",     type=int, default=7,
                        help="Approx. number of days to backtest (default: 7, max ~3 with 1000 candles)")
    args = parser.parse_args()
    run_backtest(args.symbol, args.strategy, args.days)
