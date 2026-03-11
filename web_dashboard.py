"""
HEXIS Web Dashboard
Start: python web_dashboard.py
Open:  http://localhost:5000
"""

import time
from flask import Flask, render_template, jsonify, request
import database as db
import config
from exchange import BitunixClient
from indicators import klines_to_df, add_indicators

app = Flask(__name__)
_client = BitunixClient(config.API_KEY, config.SECRET_KEY)

_last_sync: float = 0.0
_SYNC_INTERVAL: float = 10.0  # seconds between exchange syncs


@app.route("/")
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
        open_qtys = {p.get("symbol"): float(p.get("qty", 0)) for p in all_positions}
    except Exception:
        return

    for trade in open_trades:
        symbol = trade["symbol"]
        # Position auf Exchange noch offen?
        if open_qtys.get(symbol, 0) > 0:
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
        return jsonify({
            "symbol":     symbol,
            "price":      float(ticker.get("lastPrice", ticker.get("close", 0))),
            "change_pct": float(ticker.get("priceChangePercent", ticker.get("change", 0))),
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
            results.append({
                "symbol":     symbol,
                "price":      float(t.get("lastPrice", t.get("close", 0))),
                "change_pct": float(t.get("priceChangePercent", t.get("change", 0))),
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
                trade_side="CLOSE",
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


if __name__ == "__main__":
    db.init_db()
    print("Dashboard running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
