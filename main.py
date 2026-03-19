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
import strategy_state
import strategy_scanner
import trade_analyst
import circuit_breaker
from exchange import BitunixClient
from strategy import check_signal
from strategy_scalp import check_scalp_signal, ScalpSignal
from strategy_sniper import check_sniper_signal
from strategy_lsob import check_lsob_signal
from strategy_fvg import check_fvg_signal
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
    user_id: int = None,
):
    """Trading loop for a single symbol – runs in its own thread."""
    log = logging.getLogger(symbol)
    trader = Trader(client=client, risk_manager=risk_manager, symbol=symbol, user_id=user_id)
    log.info(f"Thread started | Strategy: {strategy.upper()}")
    _last_strategy = strategy  # track for change detection

    while not stop_event.is_set():
        try:
            # Read strategy fresh each tick — allows hot-swap from dashboard
            strategy = strategy_state.get_strategy(symbol)
            if strategy != _last_strategy:
                log.info(
                    f"Strategy changed: {_last_strategy.upper()} → {strategy.upper()} "
                    f"(dashboard hot-swap)"
                )
                _last_strategy = strategy

            # AUTO: handled entirely by the global Agent Scanner thread
            if strategy == "auto":
                stop_event.wait(config.LOOP_INTERVAL_SECONDS)
                continue

            klines_5m = client.get_klines(symbol, config.FAST_TF, limit=config.KLINE_LIMIT)

            has_position = trader.has_open_position()

            if has_position:
                pos = trader.current_position
                log.info(
                    f"Position open | Side: {pos.get('side')} | "
                    f"Qty: {pos.get('qty')} | "
                    f"uPNL: {pos.get('unrealizedPNL', 'N/A')}"
                )
                trader.monitor_open_position()
            elif strategy == "sniper":
                # SNIPER uses 15m candles with longer lookback for meaningful swings
                klines_sniper = client.get_klines(
                    symbol, config.SNIPER_TF, limit=config.SNIPER_KLINE_LIMIT
                )
                # 15m klines for EMA50 trend filter (don't short uptrends / long downtrends)
                klines_15m_filter = client.get_klines(symbol, "15m", limit=100)
                sniper = check_sniper_signal(
                    klines_5m=klines_sniper,
                    lookback=config.FIB_LOOKBACK,
                    klines_15m=klines_15m_filter,
                )
                if sniper:
                    log.info(
                        f"SNIPER SIGNAL: {sniper.direction.upper()} | "
                        f"Price: {sniper.price:.4f} | "
                        f"Fib 0.882 @ {sniper.fib_price:.4f} | "
                        f"SL: {sniper.sl_price:.4f} (structural) | "
                        f"TP1: {sniper.tp1_price:.4f} | "
                        f"TP2: {sniper.tp2_price:.4f} | "
                        f"TP3: {sniper.tp3_price:.4f} | "
                        f"Swing: {sniper.swing_low:.4f}–{sniper.swing_high:.4f}"
                    )
                    trader.open_sniper_position(sniper)
                else:
                    log.debug("No SNIPER signal.")
            elif strategy == "lsob":
                klines_lsob = client.get_klines(
                    symbol, config.LSOB_TF, limit=config.LSOB_KLINE_LIMIT
                )
                lsob = check_lsob_signal(
                    klines=klines_lsob,
                    lookback=config.LSOB_LOOKBACK,
                    scan_depth=config.LSOB_SCAN_DEPTH,
                )
                if lsob:
                    log.info(
                        f"LSOB SIGNAL: {lsob.direction.upper()} | "
                        f"Price: {lsob.price:.4f} | "
                        f"OB: [{lsob.ob_bottom:.4f} – {lsob.ob_top:.4f}] | "
                        f"Sweep: {lsob.sweep_price:.4f} | "
                        f"SL: {lsob.sl_price:.4f} | TP: {lsob.tp_price:.4f}"
                    )
                    trader.open_lsob_position(lsob)
                else:
                    log.debug("No LSOB signal.")
            elif strategy == "fvg":
                klines_fvg = client.get_klines(
                    symbol, config.FVG_TF, limit=config.FVG_KLINE_LIMIT
                )
                klines_15m_filter = client.get_klines(symbol, "15m", limit=100)
                fvg = check_fvg_signal(
                    klines=klines_fvg,
                    klines_15m=klines_15m_filter,
                )
                if fvg:
                    log.info(
                        f"FVG SIGNAL: {fvg.direction.upper()} | "
                        f"Price: {fvg.price:.4f} | "
                        f"Gap: [{fvg.fvg_bottom:.4f}–{fvg.fvg_top:.4f}] "
                        f"({fvg.gap_pct*100:.2f}%) | "
                        f"Age: {fvg.candle_age} candles | "
                        f"SL: {fvg.sl_price:.4f} | TP: {fvg.tp_price:.4f}"
                    )
                    trader.open_fvg_position(fvg)
                else:
                    log.debug("No FVG signal.")
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
                    trader.open_position(signal, strategy="scalp")
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
                    trader.open_position(signal, strategy="trend")
                else:
                    log.debug("No signal.")

        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)

        stop_event.wait(config.LOOP_INTERVAL_SECONDS)


def agent_scanner_loop(
    client: BitunixClient,
    risk_managers: dict,
    stop_event: threading.Event,
    user_id: int = None,
):
    """
    Global scanner for all symbols currently in AUTO mode.

    Every tick:
      1. Reads which symbols are currently set to "auto".
      2. Monitors any open positions on those symbols (incl. SNIPER partial TPs).
      3. Fetches klines for all AUTO symbols without an open position.
      4. Scores all (symbol × strategy) combinations in one pass.
      5. Opens an order for the top-ranked opportunity if its score >= MIN_OPEN_SCORE.
    """
    log = logging.getLogger("AgentScanner")

    # Create one Trader per symbol (covers any symbol that may become "auto" at runtime)
    traders = {
        sym: Trader(client=client, risk_manager=risk_managers["trend"], symbol=sym, user_id=user_id)
        for sym in config.SYMBOLS
    }
    log.info("Agent Scanner started — waiting for AUTO symbols.")

    while not stop_event.is_set():
        try:
            # ---- 1. Determine which symbols are currently in AUTO mode ----
            auto_symbols = [
                sym for sym in config.SYMBOLS
                if strategy_state.get_strategy(sym) == "auto"
            ]
            if not auto_symbols:
                stop_event.wait(config.LOOP_INTERVAL_SECONDS)
                continue

            # ---- 2. Monitor open positions ----
            for sym in auto_symbols:
                trader = traders[sym]
                if trader.has_open_position():
                    pos = trader.current_position
                    log.info(
                        f"{sym} | Position open | Side: {pos.get('side')} | "
                        f"Qty: {pos.get('qty')} | uPNL: {pos.get('unrealizedPNL', 'N/A')}"
                    )
                    trader.monitor_open_position()

            # ---- 3. Find candidates (AUTO + no open position) ----
            candidates = [
                sym for sym in auto_symbols
                if not traders[sym].has_open_position()
            ]
            if not candidates:
                log.debug("All AUTO symbols have open positions — skipping scan.")
                stop_event.wait(config.LOOP_INTERVAL_SECONDS)
                continue

            # ---- 4. Fetch klines for all candidates ----
            klines_map: dict[str, dict] = {}
            for sym in candidates:
                klines_map[sym] = {
                    "5m":  client.get_klines(sym, "5m",  limit=120),
                    "15m": client.get_klines(sym, "15m", limit=120),
                }

            # ---- 5. Score all (symbol × strategy) combinations ----
            opps = strategy_scanner.scan_opportunities(candidates, klines_map)

            # Log the top 5 scored setups for visibility
            log.info(
                f"Agent scan complete | {len(candidates)} symbols × 5 strategies | "
                f"Top score: {opps[0].score if opps else 0} "
                f"(threshold: {strategy_scanner.MIN_OPEN_SCORE})"
            )
            for opp in opps[:5]:
                log.info(
                    f"  {opp.symbol:<10} {opp.strategy.upper():<7} score={opp.score:2d} | "
                    + ", ".join(opp.reasons[:2])
                )

            # ---- 6. Execute the best opportunity if above threshold ----
            best = opps[0] if opps else None
            if not best or best.score < strategy_scanner.MIN_OPEN_SCORE:
                if best:
                    log.debug(
                        f"Best: {best.symbol} {best.strategy.upper()} score={best.score} "
                        f"— below threshold {strategy_scanner.MIN_OPEN_SCORE}, no order."
                    )
                stop_event.wait(config.LOOP_INTERVAL_SECONDS)
                continue

            log.info(
                f"AGENT ENTRY → {best.symbol} {best.strategy.upper()} "
                f"score={best.score} | {', '.join(best.reasons[:3])}"
            )

            trader = traders[best.symbol]
            klines_5m  = klines_map[best.symbol]["5m"]
            klines_15m = klines_map[best.symbol]["15m"]

            if best.strategy == "sniper":
                trader.rm = risk_managers["sniper"]
                # Reuse already-fetched 15m klines — same data scorer used, no extra API call.
                # lookback=50 matches strategy_scanner so Fib levels are identical.
                sniper = check_sniper_signal(
                    klines_5m=klines_15m,
                    lookback=50,
                    klines_15m=klines_15m,
                )
                if sniper:
                    trader.open_sniper_position(sniper)
                else:
                    log.info(
                        f"AGENT: {best.symbol} SNIPER scored {best.score} "
                        f"but signal no longer active — skipping."
                    )

            elif best.strategy == "lsob":
                trader.rm = risk_managers["lsob"]
                klines_lsob = client.get_klines(
                    best.symbol, config.LSOB_TF, limit=config.LSOB_KLINE_LIMIT
                )
                lsob = check_lsob_signal(
                    klines=klines_lsob,
                    lookback=config.LSOB_LOOKBACK,
                    scan_depth=config.LSOB_SCAN_DEPTH,
                )
                if lsob:
                    trader.open_lsob_position(lsob)
                else:
                    log.info(
                        f"AGENT: {best.symbol} LSOB scored {best.score} "
                        f"but signal no longer active — skipping."
                    )

            elif best.strategy == "scalp":
                trader.rm = risk_managers["scalp"]
                scalp = check_scalp_signal(
                    klines_5m=klines_5m,
                    bb_period=config.SCALP_BB_PERIOD,
                    bb_std=config.SCALP_BB_STD,
                    rsi_period=config.SCALP_RSI_PERIOD,
                    vol_period=config.SCALP_VOL_PERIOD,
                )
                if scalp:
                    signal = Signal(
                        direction=scalp.direction,
                        price=scalp.price,
                        rsi_5m=scalp.rsi_7,
                        ema_fast_5m=0,
                        ema_slow_5m=0,
                        trend_15m="scalp",
                    )
                    trader.open_position(signal, strategy="scalp")
                else:
                    log.info(
                        f"AGENT: {best.symbol} SCALP scored {best.score} "
                        f"but signal no longer active — skipping."
                    )

            elif best.strategy == "fvg":
                trader.rm = risk_managers["fvg"]
                # Reuse already-fetched 15m klines — same data scorer used
                fvg = check_fvg_signal(
                    klines=klines_15m,
                    klines_15m=klines_15m,
                )
                if fvg:
                    trader.open_fvg_position(fvg)
                else:
                    log.info(
                        f"AGENT: {best.symbol} FVG scored {best.score} "
                        f"but signal no longer active — skipping."
                    )

            else:  # trend
                trader.rm = risk_managers["trend"]
                signal = check_signal(
                    klines_5m=klines_5m,
                    klines_15m=klines_15m,
                    fast_ema=config.EMA_FAST,
                    slow_ema=config.EMA_SLOW,
                    rsi_period=config.RSI_PERIOD,
                )
                if signal:
                    trader.open_position(signal, strategy="trend")
                else:
                    log.info(
                        f"AGENT: {best.symbol} TREND scored {best.score} "
                        f"but signal no longer active — skipping."
                    )

        except Exception as e:
            log.error(f"Agent scanner error: {e}", exc_info=True)

        stop_event.wait(config.LOOP_INTERVAL_SECONDS)


import database as db   # noqa: E402 (needed for user manager)

# ── Per-user thread registry ─────────────────────────────────────────────────
_user_instances: dict[int, dict] = {}   # user_id → {stop: Event, threads: list}
_instances_lock = threading.Lock()


def _make_risk_managers():
    """Create a fresh set of RiskManager instances (one per strategy)."""
    rm_trend = RiskManager(
        position_margin_pct=config.POSITION_MARGIN_PCT,
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.STOP_LOSS_PCT,
        take_profit_pct=config.TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
        max_margin_pct=config.MAX_MARGIN_PCT,
    )
    rm_scalp = RiskManager(
        position_margin_pct=config.POSITION_MARGIN_PCT,
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.SCALP_STOP_LOSS_PCT,
        take_profit_pct=config.SCALP_TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
        max_margin_pct=config.MAX_MARGIN_PCT,
    )
    rm_fib = RiskManager(
        position_margin_pct=config.POSITION_MARGIN_PCT,
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.FIB_STOP_LOSS_PCT,
        take_profit_pct=config.FIB_TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
        max_margin_pct=config.MAX_MARGIN_PCT,
    )
    return {
        "trend":  rm_trend,
        "scalp":  rm_scalp,
        "sniper": rm_fib,
        "lsob":   rm_fib,
        "fvg":    rm_fib,
    }


def _start_user_trading(user: dict, global_stop: threading.Event):
    """Start all trading threads for a registered user with their own API keys."""
    uid      = user["id"]
    username = user["username"]
    log      = logging.getLogger(f"user.{username}")

    try:
        client = BitunixClient(user["api_key"], user["secret"])
        balance_data = client.get_balance("USDT")
        log.info(f"User '{username}' connected | available: {float(balance_data.get('available', 0)):.2f} USDT")
    except Exception as e:
        log.warning(f"User '{username}' API keys failed: {e} — skipping.")
        return

    stop_event   = threading.Event()
    risk_managers = _make_risk_managers()
    threads      = []

    # Agent scanner
    scanner = threading.Thread(
        target=agent_scanner_loop,
        args=(client, risk_managers, stop_event, uid),
        name=f"AgentScanner-{uid}",
        daemon=True,
    )
    threads.append(scanner)
    scanner.start()

    # Per-symbol loops
    for symbol, strategy in zip(config.SYMBOLS, config.STRATEGIES):
        rm = risk_managers.get(strategy, risk_managers["trend"])
        t = threading.Thread(
            target=symbol_loop,
            args=(symbol, strategy, client, rm, stop_event, uid),
            name=f"{symbol}-{uid}",
            daemon=True,
        )
        threads.append(t)
        t.start()
        time.sleep(0.5)

    with _instances_lock:
        _user_instances[uid] = {"stop": stop_event, "threads": threads}
    log.info(f"Trading started for user '{username}'.")


def _stop_user_trading(user_id: int):
    with _instances_lock:
        entry = _user_instances.pop(user_id, None)
    if entry:
        entry["stop"].set()
        logging.getLogger("UserManager").info(f"Stopped trading for user_id={user_id}.")


def user_manager_loop(global_stop: threading.Event):
    """
    Background loop: detects registered users with API keys and starts/stops
    their individual trading instances automatically.
    """
    log = logging.getLogger("UserManager")
    known: set[int] = set()

    while not global_stop.is_set():
        try:
            active_users = db.get_users_with_api_keys()
            active_ids   = {u["id"] for u in active_users}

            # Start new users
            for user in active_users:
                uid = user["id"]
                if uid not in known:
                    log.info(f"New user with API keys: '{user['username']}' — starting trading.")
                    _start_user_trading(user, global_stop)
                    known.add(uid)

            # Stop removed/deactivated users
            with _instances_lock:
                running_ids = set(_user_instances.keys())
            for uid in running_ids - active_ids:
                _stop_user_trading(uid)
                known.discard(uid)

        except Exception as e:
            log.error(f"User manager error: {e}", exc_info=True)

        global_stop.wait(60)   # check every 60 seconds


def main():
    logger.info("=" * 60)
    logger.info("  HEXIS – Autonomous Crypto Agent")
    for sym, strat in zip(config.SYMBOLS, config.STRATEGIES):
        logger.info(f"  {sym:<10} → {strat.upper()}")
    logger.info(f"  Trend-SL/TP: {config.STOP_LOSS_PCT*100:.1f}% / {config.TAKE_PROFIT_PCT*100:.1f}%")
    logger.info(f"  Scalp-SL/TP: {config.SCALP_STOP_LOSS_PCT*100:.1f}% / {config.SCALP_TAKE_PROFIT_PCT*100:.1f}%")
    logger.info(f"  Leverage: {config.LEVERAGE}x | Learning phase: {config.MAX_MARGIN_USDT:.0f} USDT × {config.MAX_MARGIN_TRADES} trades")
    logger.info("=" * 60)

    circuit_breaker.init(
        daily_limit_usdt=config.DAILY_LOSS_LIMIT_USDT,
        max_consecutive_losses=config.MAX_CONSECUTIVE_LOSSES,
    )

    client = BitunixClient(config.API_KEY, config.SECRET_KEY)

    # Three RiskManager instances – one per strategy
    risk_manager_trend = RiskManager(
        position_margin_pct=config.POSITION_MARGIN_PCT,
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.STOP_LOSS_PCT,
        take_profit_pct=config.TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
        max_margin_pct=config.MAX_MARGIN_PCT,
    )
    risk_manager_scalp = RiskManager(
        position_margin_pct=config.POSITION_MARGIN_PCT,
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.SCALP_STOP_LOSS_PCT,
        take_profit_pct=config.SCALP_TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
        max_margin_pct=config.MAX_MARGIN_PCT,
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
        position_margin_pct=config.POSITION_MARGIN_PCT,
        risk_per_trade=config.RISK_PER_TRADE,
        stop_loss_pct=config.FIB_STOP_LOSS_PCT,
        take_profit_pct=config.FIB_TAKE_PROFIT_PCT,
        leverage=config.LEVERAGE,
        max_margin_usdt=config.MAX_MARGIN_USDT,
        max_margin_trades=config.MAX_MARGIN_TRADES,
        max_margin_pct=config.MAX_MARGIN_PCT,
    )

    # Shared risk manager map used by the Agent Scanner
    risk_managers = {
        "trend":  risk_manager_trend,
        "scalp":  risk_manager_scalp,
        "sniper": risk_manager_fib,
        "lsob":   risk_manager_fib,
        "fvg":    risk_manager_fib,   # structural SL — same sizing as SNIPER/LSOB
    }

    # Start the global Agent Scanner (handles all AUTO symbols centrally)
    scanner_thread = threading.Thread(
        target=agent_scanner_loop,
        args=(client, risk_managers, stop_event),
        name="AgentScanner",
        daemon=True,
    )
    threads.append(scanner_thread)
    scanner_thread.start()

    # Start the AI Trade Analyst (analyzes performance and auto-adjusts parameters)
    analyst_thread = threading.Thread(
        target=trade_analyst.run_analysis_loop,
        args=(stop_event,),
        name="TradeAnalyst",
        daemon=True,
    )
    threads.append(analyst_thread)
    analyst_thread.start()

    # Start the User Manager (starts/stops trading instances for registered users)
    user_mgr_thread = threading.Thread(
        target=user_manager_loop,
        args=(stop_event,),
        name="UserManager",
        daemon=True,
    )
    threads.append(user_mgr_thread)
    user_mgr_thread.start()

    # Start per-symbol threads for non-AUTO strategies
    # (AUTO symbols are skipped inside symbol_loop; the scanner handles them)
    for symbol, strategy in zip(config.SYMBOLS, config.STRATEGIES):
        if strategy == "scalp":
            rm = risk_manager_scalp
        elif strategy in ("fib", "sniper"):
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
