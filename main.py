"""
HEXIS – Autonomous Crypto Agent
Exchange: Bitunix Futures
Symbols:  BTC, ETH, SOL, XRP
Strategy: RSI + EMA Crossover on 5m/15m charts

Start:  python main.py
Stop:   CTRL+C
"""

import logging
import time
import sys
import threading

import config
from exchange import BitunixClient
from strategy import check_signal
from strategy_scalp import check_scalp_signal, ScalpSignal
from strategy_fib import check_fib_signal
from strategy import Signal
from risk_manager import RiskManager
from trader import Trader


# ---- Logging Setup ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-10s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


def symbol_loop(
    symbol: str,
    strategy: str,
    client: BitunixClient,
    risk_manager: RiskManager,
    stop_event: threading.Event,
):
    """Trading loop for a single symbol – runs in its own thread."""
    log = logging.getLogger(symbol)
    trader = Trader(client=client, risk_manager=risk_manager, symbol=symbol)
    log.info(f"Thread started | Strategy: {strategy.upper()}")

    while not stop_event.is_set():
        try:
            klines_5m = client.get_klines(symbol, config.FAST_TF, limit=config.KLINE_LIMIT)

            has_position = trader.has_open_position()

            if has_position:
                pos = trader.current_position
                log.info(
                    f"Position open | Side: {pos.get('side')} | "
                    f"Qty: {pos.get('qty')} | "
                    f"uPNL: {pos.get('unrealizedPNL', 'N/A')}"
                )
            elif strategy == "fib":
                fib = check_fib_signal(
                    klines_5m=klines_5m,
                    lookback=config.FIB_LOOKBACK,
                )
                if fib:
                    log.info(
                        f"FIB SIGNAL: {fib.direction.upper()} | "
                        f"Price: {fib.price:.4f} | "
                        f"Level: {fib.fib_level} @ {fib.fib_price:.4f} | "
                        f"RSI: {fib.rsi:.1f} | "
                        f"Swing: {fib.swing_low:.4f}–{fib.swing_high:.4f}"
                    )
                    signal = Signal(
                        direction=fib.direction,
                        price=fib.price,
                        rsi_5m=fib.rsi,
                        ema_fast_5m=0,
                        ema_slow_5m=0,
                        trend_15m="fib",
                    )
                    trader.open_position(signal)
                else:
                    log.debug("No Fibonacci signal.")
            elif strategy == "scalp":
                scalp = check_scalp_signal(
                    klines_5m=klines_5m,
                    bb_period=config.SCALP_BB_PERIOD,
                    bb_std=config.SCALP_BB_STD,
                    rsi_period=config.SCALP_RSI_PERIOD,
                    vol_period=config.SCALP_VOL_PERIOD,
                )
                if scalp:
                    log.info(
                        f"SCALP SIGNAL: {scalp.direction.upper()} | "
                        f"Price: {scalp.price:.4f} | "
                        f"RSI(7): {scalp.rsi_7:.1f} | "
                        f"BB%: {scalp.bb_pct:.2f} | "
                        f"Vol: {scalp.vol_ratio:.2f}x"
                    )
                    signal = Signal(
                        direction=scalp.direction,
                        price=scalp.price,
                        rsi_5m=scalp.rsi_7,
                        ema_fast_5m=0,
                        ema_slow_5m=0,
                        trend_15m="scalp",
                    )
                    trader.open_position(signal)
                else:
                    log.debug("No scalp signal.")
            else:
                klines_15m = client.get_klines(symbol, config.SLOW_TF, limit=config.KLINE_LIMIT)
                signal = check_signal(
                    klines_5m=klines_5m,
                    klines_15m=klines_15m,
                    fast_ema=config.EMA_FAST,
                    slow_ema=config.EMA_SLOW,
                    rsi_period=config.RSI_PERIOD,
                )
                if signal:
                    log.info(
                        f"TREND SIGNAL: {signal.direction.upper()} | "
                        f"Price: {signal.price:.4f} | "
                        f"RSI: {signal.rsi_5m:.1f} | "
                        f"Trend: {signal.trend_15m}"
                    )
                    trader.open_position(signal)
                else:
                    log.debug("No signal.")

        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)

        stop_event.wait(config.LOOP_INTERVAL_SECONDS)


def main():
    logger.info("=" * 60)
    logger.info("  HEXIS – Autonomous Crypto Agent")
    for sym, strat in zip(config.SYMBOLS, config.STRATEGIES):
        logger.info(f"  {sym:<10} → {strat.upper()}")
    logger.info(f"  Trend-SL/TP: {config.STOP_LOSS_PCT*100:.1f}% / {config.TAKE_PROFIT_PCT*100:.1f}%")
    logger.info(f"  Scalp-SL/TP: {config.SCALP_STOP_LOSS_PCT*100:.1f}% / {config.SCALP_TAKE_PROFIT_PCT*100:.1f}%")
    logger.info(f"  Leverage: {config.LEVERAGE}x | Learning phase: {config.MAX_MARGIN_USDT:.0f} USDT × {config.MAX_MARGIN_TRADES} trades")
    logger.info("=" * 60)

    client = BitunixClient(config.API_KEY, config.SECRET_KEY)

    # Three RiskManager instances – one per strategy
    risk_manager_trend = RiskManager(
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.STOP_LOSS_PCT,
        take_profit_pct=config.TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
    )
    risk_manager_scalp = RiskManager(
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.SCALP_STOP_LOSS_PCT,
        take_profit_pct=config.SCALP_TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
    )

    # Connection test
    try:
        balance = client.get_balance("USDT")
        logger.info(f"Connection OK | Available capital: {float(balance.get('available', 0)):.2f} USDT")
    except Exception as e:
        logger.error(f"Connection error: {e}")
        logger.error("Check your API keys in the .env file.")
        sys.exit(1)

    logger.info(f"Bot running – {len(config.SYMBOLS)} symbols, checking every {config.LOOP_INTERVAL_SECONDS}s...")
    logger.info("Press CTRL+C to stop\n")

    stop_event = threading.Event()
    threads = []

    risk_manager_fib = RiskManager(
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.FIB_STOP_LOSS_PCT,
        take_profit_pct=config.FIB_TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
    )

    for symbol, strategy in zip(config.SYMBOLS, config.STRATEGIES):
        if strategy == "scalp":
            rm = risk_manager_scalp
        elif strategy == "fib":
            rm = risk_manager_fib
        else:
            rm = risk_manager_trend
        t = threading.Thread(
            target=symbol_loop,
            args=(symbol, strategy, client, rm, stop_event),
            name=symbol,
            daemon=True,
        )
        threads.append(t)
        t.start()
        time.sleep(1)  # Stagger starts slightly to avoid API burst

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\nStop signal received – shutting down all threads...")
        stop_event.set()
        for t in threads:
            t.join(timeout=10)
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
