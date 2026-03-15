"""
HEXIS Web Dashboard
Start: python web_dashboard.py
Open:  http://localhost:5000
"""

import time
import threading
import logging
from flask import Flask, render_template, jsonify, request
import database as db
import config
import strategy_state
import circuit_breaker
from exchange import BitunixClient
from indicators import klines_to_df, add_indicators

app = Flask(__name__)
_client = BitunixClient(config.API_KEY, config.SECRET_KEY)

# Circuit breakers for the dashboard process
circuit_breaker.init(
    daily_limit_usdt=config.DAILY_LOSS_LIMIT_USDT,
    max_consecutive_losses=config.MAX_CONSECUTIVE_LOSSES,
)

_last_sync: float = 0.0
_SYNC_INTERVAL: float = 10.0  # seconds between open-position syncs

_last_closed_sync: float = 0.0
_CLOSED_SYNC_INTERVAL: float = 3600.0  # 1 hour between closed-trade reconciliation


@app.route("/", methods=["GET", "POST"])
def index():
    return render_template("dashboard.html")


def _sync_open_trades():
    """
    Compares all 'open' DB trades with the exchange.
    Trades no longer on the exchange are closed in the DB.
    Rate-limited to once every _SYNC_INTERVAL seconds.
    """
    global _last_sync
    now = time.time()
    if now - _last_sync < _SYNC_INTERVAL:
        return
    _last_sync = now

    open_trades = [t for t in db.get_all_trades(limit=500) if t["status"] == "open"]
    if not open_trades:
        return

    # Alle offenen Exchange-Positionen einmalig abrufen
    try:
        all_positions = _client.get_open_positions()
        pos_by_symbol = {p.get("symbol"): p for p in all_positions if float(p.get("qty", 0)) > 0}
    except Exception:
        return

    for trade in open_trades:
        symbol = trade["symbol"]
        pos = pos_by_symbol.get(symbol)
        exchange_qty = float(pos.get("qty", 0)) if pos else 0

        # Position auf Exchange noch offen?
        if exchange_qty > 0:
            # Sync qty if it changed (e.g. partial TP or manual adjustment)
            db_qty = float(trade.get("qty", 0))
            if abs(exchange_qty - db_qty) > 0.0001:
                db.update_trade_qty(trade["trade_id"], exchange_qty)

            # Sync unrealized PnL from exchange position
            unrealized = 0.0
            for field in ("unrealizedPNL", "unRealizedPNL", "pnl", "unrealisedPnl"):
                val = pos.get(field)
                if val is not None:
                    unrealized = float(val)
                    if unrealized != 0:
                        break
            db.update_unrealized_pnl(trade["trade_id"], unrealized)

            # Sync margin + leverage from exchange
            margin_val = pos.get("margin")
            lev_val    = pos.get("leverage")
            if margin_val is not None and lev_val is not None:
                try:
                    db.update_trade_margin(trade["trade_id"], float(margin_val), int(lev_val))
                except Exception:
                    pass
            continue  # noch offen, nichts tun

        # Nicht mehr auf Exchange → schließen
        try:
            ticker = _client.get_ticker(symbol)
            exit_price = float(ticker.get("lastPrice", ticker.get("close", 0)))
        except Exception:
            exit_price = 0.0

        if not exit_price:
            continue

        # TP/SL-Status ableiten
        tp  = float(trade.get("tp_price") or 0)
        sl  = float(trade.get("sl_price") or 0)
        direction = trade.get("direction", "long")
        status = "closed"
        if direction == "long":
            if tp and exit_price >= tp * 0.998:
                status = "tp_hit"
            elif sl and exit_price <= sl * 1.002:
                status = "sl_hit"
        else:
            if tp and exit_price <= tp * 1.002:
                status = "tp_hit"
            elif sl and exit_price >= sl * 0.998:
                status = "sl_hit"

        db.close_trade(trade_id=trade["trade_id"], exit_price=exit_price, status=status)


def _sync_closed_trades(force: bool = False):
    """
    Hourly: fetch position history from the exchange and reconcile with DB.

    Two cases handled:
      1. Trade still 'open' in DB but no longer on the exchange → auto-close with
         the actual exit price and PnL from the exchange.
      2. Trade already closed in DB but exit price differs by >0.5% from exchange
         → correct the price and PnL so the stats remain accurate.

    Matching logic: same symbol + direction + entry price within 0.5%.
    """
    global _last_closed_sync
    now = time.time()
    if not force and now - _last_closed_sync < _CLOSED_SYNC_INTERVAL:
        return
    _last_closed_sync = now

    log = logging.getLogger("closed_sync")
    log.info("Hourly closed trade sync starting...")

    try:
        history = _client.get_history_positions(limit=100)
    except Exception as e:
        log.warning(f"Hourly closed sync — exchange fetch failed: {e}")
        return

    if not history:
        log.info("Hourly closed sync — no history returned from exchange")
        return

    # Fetch currently live positions ONCE — used to guard against closing
    # partially-TP'd trades that still appear in history (partial close events)
    try:
        live_positions = _client.get_open_positions()
        # Key: (symbol, side) — e.g. ("BNBUSDT", "BUY")
        live_keys = {
            (p.get("symbol", ""), p.get("side", "").upper())
            for p in live_positions
            if float(p.get("qty", 0)) > 0
        }
    except Exception:
        live_keys = set()

    all_trades = db.get_all_trades(limit=200)
    closed_now = 0
    corrected = 0

    for pos in history:
        symbol    = pos.get("symbol", "")
        side      = pos.get("side", "").upper()
        direction = "long" if side == "BUY" else "short"

        # Bitunix may use different field names across versions
        avg_open = float(
            pos.get("openPrice") or pos.get("avgOpenPrice") or
            pos.get("entryPrice") or pos.get("avgEntryPrice") or 0
        )
        avg_close = float(
            pos.get("closePrice") or pos.get("avgClosePrice") or
            pos.get("exitPrice") or pos.get("avgExitPrice") or 0
        )
        ex_pnl = float(
            pos.get("realizedPNL") or pos.get("realizedPnl") or
            pos.get("pnl") or pos.get("profit") or 0
        )
        close_time = pos.get("closeTime") or pos.get("updateTime")

        if not avg_open or not avg_close:
            continue

        for trade in all_trades:
            if trade["symbol"] != symbol or trade["direction"] != direction:
                continue
            # Only consider trades that are still OPEN in the DB.
            # Already-closed trades are never overwritten by the sync —
            # their exit price and PnL are considered final.
            if trade.get("status") != "open":
                continue
            db_entry = float(trade.get("entry_price") or 0)
            if not db_entry or abs(avg_open - db_entry) / db_entry > 0.005:
                continue

            # ── Match found: open DB trade closed on exchange ─────────────────
            # Skip if the position is still live on the exchange.
            # Partial-TP events (SNIPER TP1/TP2) produce history entries
            # but the remaining qty is still open — we must NOT close those.
            exchange_side = "BUY" if direction == "long" else "SELL"
            if (symbol, exchange_side) in live_keys:
                break  # still open, leave DB as-is
            db.correct_closed_trade(trade["trade_id"], avg_close, ex_pnl, close_time)
            log.info(
                f"[AUTO-CLOSE] {symbol} {direction} | "
                f"exit={avg_close:.4f} | pnl={ex_pnl:+.4f} USDT"
            )
            closed_now += 1
            break  # stop searching after first match for this exchange position

    log.info(
        f"Hourly closed sync done — "
        f"{closed_now} auto-closed, {corrected} price correction(s), "
        f"{len(history)} positions checked"
    )


def _start_background_sync():
    """Start the background thread for hourly closed-trade reconciliation."""
    def _loop():
        while True:
            try:
                _sync_closed_trades()
            except Exception as e:
                logging.getLogger("closed_sync").error(f"Sync thread error: {e}", exc_info=True)
            time.sleep(60)  # check every minute; rate-limited by _CLOSED_SYNC_INTERVAL

    t = threading.Thread(target=_loop, daemon=True, name="closed_sync")
    t.start()


@app.route("/api/stats")
def api_stats():
    _sync_open_trades()
    return jsonify(db.get_stats())


@app.route("/api/trades")
def api_trades():
    _sync_open_trades()
    trades = db.get_all_trades(limit=200)
    return jsonify(trades)


@app.route("/api/daily_pnl")
def api_daily_pnl():
    return jsonify(db.get_daily_pnl())



@app.route("/api/price")
def api_price():
    symbol = request.args.get("symbol", config.SYMBOL)
    try:
        ticker = _client.get_ticker(symbol)
        last  = float(ticker.get("lastPrice", ticker.get("close", 0)))
        open_ = float(ticker.get("open", 0))
        change_pct = round((last - open_) / open_ * 100, 2) if open_ else 0.0
        return jsonify({
            "symbol":     symbol,
            "price":      last,
            "change_pct": change_pct,
            "high_24h":   float(ticker.get("high", 0)),
            "low_24h":    float(ticker.get("low", 0)),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/prices")
def api_prices():
    """Prices for all traded symbols at once."""
    results = []
    for symbol in config.SYMBOLS:
        try:
            t = _client.get_ticker(symbol)
            last  = float(t.get("lastPrice", t.get("close", 0)))
            open_ = float(t.get("open", 0))
            change_pct = round((last - open_) / open_ * 100, 2) if open_ else 0.0
            results.append({
                "symbol":     symbol,
                "price":      last,
                "change_pct": change_pct,
            })
        except Exception:
            results.append({"symbol": symbol, "price": 0, "change_pct": 0})
    return jsonify(results)


@app.route("/api/balance")
def api_balance():
    try:
        data = _client.get_balance("USDT")

        # Bitunix uses different field names — try all known variants
        unrealized = 0.0
        for field in ("crossUnrealizedPNL", "unrealizedPNL", "crossUnPNL",
                      "unRealizedPNL", "totalUnrealizedProfit"):
            val = data.get(field)
            if val is not None:
                unrealized = float(val)
                if unrealized != 0:
                    break

        # Fallback: calculate from open positions
        if unrealized == 0.0:
            try:
                positions = _client.get_open_positions()
                for p in positions:
                    for pf in ("unrealizedPNL", "unRealizedPNL", "pnl"):
                        v = p.get(pf)
                        if v is not None:
                            unrealized += float(v)
                            break
            except Exception:
                pass

        return jsonify({
            "available":      float(data.get("available", 0)),
            "margin":         float(data.get("margin", 0)),
            "frozen":         float(data.get("frozen", 0)),
            "unrealized_pnl": round(unrealized, 4),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/strategies")
def api_strategies():
    """Returns the current strategy for every symbol."""
    return jsonify(strategy_state.load())


@app.route("/api/strategy", methods=["POST"])
def api_set_strategy():
    """Change the strategy for one symbol (hot-swap — no bot restart needed)."""
    payload  = request.get_json(force=True)
    symbol   = payload.get("symbol", "").upper()
    strat    = payload.get("strategy", "").lower()

    if not symbol or not strat:
        return jsonify({"error": "symbol and strategy required"}), 400

    ok = strategy_state.set_strategy(symbol, strat)
    if not ok:
        return jsonify({"error": f"invalid strategy '{strat}'. Use: trend | scalp | sniper | lsob | auto"}), 400

    return jsonify({"ok": True, "symbol": symbol, "strategy": strat})


@app.route("/api/agent_mode", methods=["POST"])
def api_agent_mode():
    """
    Globally enable or disable Agent Mode (auto strategy selector).
    - enable=True:  sets all symbols WITHOUT an open position to 'auto'
    - enable=False: sets all symbols currently on 'auto' back to 'sniper'
    Symbols with open positions are never touched.
    """
    payload = request.get_json(force=True)
    enable  = bool(payload.get("enabled", True))

    # Find which symbols have an open position right now
    open_trades   = [t for t in db.get_all_trades(limit=500) if t["status"] == "open"]
    locked_symbols = {t["symbol"] for t in open_trades}

    current = strategy_state.load()
    changed, skipped = [], []

    for symbol in config.SYMBOLS:
        if symbol in locked_symbols:
            skipped.append(symbol)
            continue
        if enable:
            strategy_state.set_strategy(symbol, "auto")
            changed.append(symbol)
        else:
            # Only revert symbols that are currently on 'auto'
            if current.get(symbol) == "auto":
                strategy_state.set_strategy(symbol, "sniper")
                changed.append(symbol)

    return jsonify({
        "ok":      True,
        "enabled": enable,
        "changed": changed,
        "skipped": skipped,
    })


@app.route("/api/sync_closed", methods=["POST"])
def api_sync_closed():
    """Manually trigger the closed trade reconciliation (ignores the 1h rate limit)."""
    try:
        _sync_closed_trades(force=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/close_position", methods=["POST"])
def api_close_position():
    """Manually close an open position via market order."""
    try:
        payload  = request.get_json(force=True)
        symbol   = payload.get("symbol", "").upper()
        trade_id = payload.get("trade_id")

        if not symbol:
            return jsonify({"error": "symbol missing"}), 400

        # Get current price for PnL calculation
        exit_price = 0.0
        try:
            ticker     = _client.get_ticker(symbol)
            exit_price = float(ticker.get("lastPrice", ticker.get("close", 0)))
        except Exception:
            pass

        # Get open position on the exchange
        positions = _client.get_open_positions(symbol)
        pos = next((p for p in positions if float(p.get("qty", 0)) > 0), None)

        order_result = None
        if pos is not None:
            # Position still open → place market close order
            position_side = pos.get("side", "BUY")
            qty           = str(pos.get("qty", "0"))
            close_side    = "SELL" if position_side == "BUY" else "BUY"
            order_result = _client.place_order(
                symbol=symbol,
                side=close_side,
                trade_side="OPEN",
                qty=qty,
                order_type="MARKET",
                reduce_only=True,
            )
        # else: position already closed (TP/SL) — only update DB

        # Always close the DB entry
        if trade_id and exit_price:
            db.close_trade(trade_id=trade_id, exit_price=exit_price, status="closed")

        return jsonify({"ok": True, "order": order_result, "exit_price": exit_price})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/equity")
def api_equity():
    """Equity curve: cumulative PnL over all closed trades."""
    return jsonify(db.get_equity_curve())


@app.route("/api/analytics")
def api_analytics():
    """Per-strategy and per-symbol breakdown + drawdown metrics."""
    return jsonify(db.get_analytics())


@app.route("/api/circuit_breaker")
def api_circuit_breaker_status():
    """Current circuit breaker state."""
    status = circuit_breaker.get_status()
    ok, reason = circuit_breaker.is_trading_allowed()
    status["trading_allowed"] = ok
    status["block_reason"]    = reason
    return jsonify(status)


@app.route("/api/circuit_breaker/reset", methods=["POST"])
def api_circuit_breaker_reset():
    """Reset circuit breakers (optionally a specific strategy)."""
    payload  = request.get_json(force=True) or {}
    strategy = payload.get("strategy")   # None = reset everything
    circuit_breaker.reset(strategy)
    return jsonify({"ok": True, "reset": strategy or "all"})


@app.route("/api/backtest")
def api_backtest():
    """
    Quick backtest for trend/scalp strategies.
    Query params: symbol (default BTCUSDT), strategy (trend|scalp), days (default 7)
    """
    symbol   = request.args.get("symbol",   config.SYMBOL).upper()
    strategy = request.args.get("strategy", "trend").lower()
    days     = int(request.args.get("days",  "7"))

    if strategy not in ("trend", "scalp"):
        return jsonify({"error": "strategy must be 'trend' or 'scalp'"}), 400
    if days < 1 or days > 30:
        return jsonify({"error": "days must be 1–30"}), 400

    try:
        from backtest import run_backtest_api
        result = run_backtest_api(symbol, strategy, days)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    db.init_db()
    _start_background_sync()
    print("Dashboard running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
