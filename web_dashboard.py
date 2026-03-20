"""
HEXIS Web Dashboard
Start: python web_dashboard.py
Open:  http://localhost:5000
"""

import time
import threading
import logging
import os
import secrets
import string
from flask import Flask, render_template, jsonify, request, Response, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
import database as db
import config
import strategy_state
import circuit_breaker
import mailer
from exchange import BitunixClient
from indicators import klines_to_df, add_indicators

import random

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)

# ── Auth (DB-based) ──────────────────────────────────────────────────────────
_PUBLIC_ROUTES = {"login", "register", "checkout", "create_payment", "payment_status", "forgot_password", "reset_password", "static"}

@app.before_request
def check_auth():
    if request.endpoint in _PUBLIC_ROUTES:
        return
    if not session.get("user_id"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"]   = user["id"]
            session["username"]  = user["username"]
            session["is_admin"]  = bool(user["is_admin"])
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


def _generate_invite_code(length: int = 10) -> str:
    """Generate a random uppercase alphanumeric invite code."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    first_user = db.count_users() == 0  # first user gets admin without code

    if request.method == "POST":
        username    = request.form.get("username", "").strip()
        email       = request.form.get("email", "").strip() or None
        password    = request.form.get("password", "")
        confirm     = request.form.get("confirm", "")
        invite_code = request.form.get("invite_code", "").strip().upper()

        if not username or not password:
            error = "Username and password are required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif db.get_user_by_username(username):
            error = "Username already taken."
        elif not first_user:
            # Non-admin registrations require a valid unused invite code
            code_row = db.get_invite_code(invite_code) if invite_code else None
            if not code_row:
                error = "Invalid invite code."
            elif code_row["used"]:
                error = "This invite code has already been used."
            elif code_row.get("expires_at") and code_row["expires_at"] < \
                    __import__("datetime").datetime.utcnow().isoformat():
                error = "This invite code has expired."
            else:
                pw_hash = generate_password_hash(password)
                user_id = db.create_user(username, pw_hash, email, is_admin=False)
                db.use_invite_code(invite_code, user_id)
                session["user_id"]  = user_id
                session["username"] = username
                session["is_admin"] = False
                return redirect(url_for("index"))
        else:
            # First user → admin, no code required
            pw_hash = generate_password_hash(password)
            user_id = db.create_user(username, pw_hash, email, is_admin=True)
            session["user_id"]  = user_id
            session["username"] = username
            session["is_admin"] = True
            return redirect(url_for("index"))

    return render_template("register.html", error=error, first_user=first_user)


# ── Admin: invite code management ────────────────────────────────────────────

def _require_admin():
    if not session.get("is_admin"):
        return jsonify({"error": "admin only"}), 403
    return None


@app.route("/api/admin/invite", methods=["GET", "POST"])
def api_admin_invite():
    """GET: list all codes. POST: generate a new code and optionally email it."""
    err = _require_admin()
    if err:
        return err

    if request.method == "GET":
        return jsonify(db.get_all_invite_codes())

    data  = request.get_json(force=True) or {}
    email = data.get("email", "").strip() or None
    code  = _generate_invite_code()
    db.create_invite_code(code, email)

    sent = False
    if email:
        sent = mailer.send_invite_code(email, code)

    return jsonify({"ok": True, "code": code, "email": email, "email_sent": sent})



@app.route("/api/admin/users")
def api_admin_users():
    err = _require_admin()
    if err:
        return err
    return jsonify(db.get_all_users())


@app.route("/api/admin/invite/resend", methods=["POST"])
def api_admin_invite_resend():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    code  = data.get("code", "").strip()
    email = data.get("email", "").strip()
    if not code or not email:
        return jsonify({"error": "code and email required"}), 400
    sent = mailer.send_invite_code(email, code)
    return jsonify({"ok": True, "email_sent": sent})


@app.route("/api/admin/invite/delete", methods=["POST"])
def api_admin_invite_delete():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    deleted = db.delete_invite_code(code)
    return jsonify({"ok": deleted, "error": None if deleted else "Code not found or already used"})

# ── Crypto payment gateway ────────────────────────────────────────────────────

@app.route("/checkout")
def checkout():
    import crypto_watcher
    return render_template(
        "checkout.html",
        networks=crypto_watcher.active_networks(),
        price=config.CRYPTO_PRICE_USDT,
    )


@app.route("/api/create_payment", methods=["POST"])
def create_payment():
    """Generate a unique payment amount for the buyer and store the pending request."""
    import crypto_watcher
    nets = crypto_watcher.active_networks()
    if not nets:
        return jsonify({"error": "Crypto payments not configured."}), 503

    data  = request.get_json(force=True) or {}
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Valid email address required."}), 400

    # Reuse an existing pending request for this email if still valid
    existing = db.get_pending_payment_by_email(email)
    if existing:
        return jsonify({
            "amount":   existing["expected_amount"],
            "networks": nets,
        })

    # Generate a unique micro-amount so the payment can be matched without a memo.
    # Variation is capped at ±5% of base price (max 0.97 USDT) so it stays proportional.
    base      = config.CRYPTO_PRICE_USDT
    max_cents = max(1, min(97, int(base * 0.05 * 100)))  # 5% of base, 1–97 cents
    amount    = base
    for _ in range(30):
        candidate = round(base + random.randint(1, max_cents) / 100, 2)
        if not db.is_amount_pending(candidate):
            amount = candidate
            break
    db.create_pending_payment(email, amount)

    return jsonify({
        "amount":   amount,
        "networks": nets,
    })


@app.route("/api/payment_status")
def payment_status():
    """Poll endpoint — returns confirmed / pending / not_found."""
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"status": "error"}), 400

    confirmed = db.get_confirmed_payment_by_email(email)
    if confirmed:
        return jsonify({"status": "confirmed", "code": confirmed["invite_code"]})

@app.route("/api/telegram/link_code", methods=["POST"])
def api_telegram_link_code():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Not logged in"}), 401
    import secrets as _sec
    code = _sec.token_hex(4).upper()
    db.save_telegram_link_code(uid, code)
    return jsonify({"code": code})


@app.route("/api/telegram/status")
def api_telegram_status():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"connected": False})
    chat_id = db.get_telegram_chat_id(uid)
    return jsonify({"connected": bool(chat_id)})


@app.route("/api/telegram/disconnect", methods=["POST"])
def api_telegram_disconnect():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Not logged in"}), 401
    db.disconnect_telegram(uid)
    return jsonify({"ok": True})


@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    msg = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user  = db.get_user_by_email(email)
        # Always show same message to prevent email enumeration
        msg = "If an account with that email exists, a reset link has been sent."
        if user:
            import secrets as _secrets
            token = _secrets.token_urlsafe(32)
            db.create_reset_token(user["id"], token)
            reset_url = f"{config.HEXIS_BASE_URL}/reset_password?token={token}"
            mailer.send_password_reset(email, reset_url)
    return render_template("forgot_password.html", msg=msg)


@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    token = request.args.get("token", "").strip()
    error = None
    if request.method == "POST":
        token    = request.form.get("token", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            from werkzeug.security import generate_password_hash
            ok = db.consume_reset_token(token, generate_password_hash(password))
            if ok:
                return redirect(url_for("login") + "?reset=1")
            error = "Reset link is invalid or has expired."
    token_row = db.get_reset_token(token) if token else None
    if not token_row and request.method == "GET":
        error = "Reset link is invalid or has expired."
    return render_template("reset_password.html", token=token, error=error)



    pending = db.get_pending_payment_by_email(email)
    if pending:
        return jsonify({"status": "pending"})

    return jsonify({"status": "not_found"})


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/user/settings", methods=["GET", "POST"])
def api_user_settings():
    """Get or update per-user trading settings (e.g. margin_pct)."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "not logged in"}), 401
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        pct  = data.get("margin_pct")
        if pct is None:
            return jsonify({"error": "margin_pct required"}), 400
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            return jsonify({"error": "margin_pct must be a number"}), 400
        if not (0.001 <= pct <= 1.0):
            return jsonify({"error": "margin_pct must be between 0.1% and 100%"}), 400
        db.update_user_margin_pct(user_id, pct)
        return jsonify({"ok": True, "margin_pct": round(pct, 4)})
    # GET
    pct = db.get_user_margin_pct(user_id)
    return jsonify({"margin_pct": pct})


@app.route("/api/user/keys", methods=["GET", "POST"])
def api_user_keys():
    """Get or update the logged-in user's Bitunix API credentials."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "not logged in"}), 401
    if request.method == "POST":
        data    = request.get_json(force=True) or {}
        api_key = data.get("api_key", "").strip()
        secret  = data.get("secret_key", "").strip()
        if not api_key or not secret:
            return jsonify({"error": "api_key and secret_key required"}), 400
        db.update_user_api_keys(user_id, api_key, secret)
        # Invalidate client cache so next request picks up new keys
        _user_clients.pop(user_id, None)
        return jsonify({"ok": True})
    user = db.get_user_by_id(user_id)
    has_keys = bool(user and user.get("api_key_enc"))
    return jsonify({"has_keys": has_keys, "username": user["username"] if user else ""})

_client = BitunixClient(config.API_KEY, config.SECRET_KEY)   # admin / fallback client

# Per-user client cache: {user_id: BitunixClient}
_user_clients: dict[int, BitunixClient] = {}


def _get_client() -> BitunixClient:
    """
    Return the BitunixClient for the currently logged-in user.
    Falls back to the admin client if the user has no API keys stored.
    """
    uid = session.get("user_id")
    if not uid:
        return _client

    # Serve from cache
    if uid in _user_clients:
        return _user_clients[uid]

    # Build client from DB keys
    try:
        users = db.get_users_with_api_keys()
        user  = next((u for u in users if u["id"] == uid), None)
        if user:
            client = BitunixClient(user["api_key"], user["secret"])
            _user_clients[uid] = client
            return client
    except Exception:
        pass

    return _client

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
        all_positions = _get_client().get_open_positions()
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
            ticker = _get_client().get_ticker(symbol)
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


def _current_user_id():
    """Return the logged-in user's id, or None for unauthenticated/admin views."""
    uid = session.get("user_id")
    # Admins see all trades; regular users see only their own
    if session.get("is_admin"):
        return None
    return uid


@app.route("/api/stats")
def api_stats():
    _sync_open_trades()
    return jsonify(db.get_stats(user_id=_current_user_id()))


@app.route("/api/trades")
def api_trades():
    _sync_open_trades()
    trades = db.get_all_trades(limit=200, user_id=_current_user_id())
    return jsonify(trades)


@app.route("/api/daily_pnl")
def api_daily_pnl():
    return jsonify(db.get_daily_pnl(user_id=_current_user_id()))



@app.route("/api/price")
def api_price():
    symbol = request.args.get("symbol", config.SYMBOL)
    try:
        ticker = _get_client().get_ticker(symbol)
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
            t = _get_client().get_ticker(symbol)
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
        uid = session.get("user_id")
        # Non-admin users without API keys should not see admin balance
        if uid and uid != 1:
            user = db.get_user_by_id(uid)
            if not user or not user.get("api_key_enc"):
                return jsonify({"no_keys": True})
        client = _get_client()
        data = client.get_balance("USDT")

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
                positions = client.get_open_positions()
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
    """Returns the current strategy for every symbol (per user)."""
    uid = session.get("user_id")
    return jsonify(strategy_state.load(uid))


@app.route("/api/strategy", methods=["POST"])
def api_set_strategy():
    """Change the strategy for one symbol (hot-swap — no bot restart needed)."""
    payload  = request.get_json(force=True)
    symbol   = payload.get("symbol", "").upper()
    strat    = payload.get("strategy", "").lower()

    if not symbol or not strat:
        return jsonify({"error": "symbol and strategy required"}), 400

    uid = session.get("user_id")
    ok = strategy_state.set_strategy(symbol, strat, uid)
    if not ok:
        return jsonify({"error": f"invalid strategy '{strat}'. Use: trend | scalp | sniper | lsob | fvg | auto"}), 400

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

    uid = session.get("user_id")
    current = strategy_state.load(uid)
    changed, skipped = [], []

    for symbol in config.SYMBOLS:
        if symbol in locked_symbols:
            skipped.append(symbol)
            continue
        if enable:
            strategy_state.set_strategy(symbol, "auto", uid)
            changed.append(symbol)
        else:
            # Only revert symbols that are currently on 'auto'
            if current.get(symbol) == "auto":
                strategy_state.set_strategy(symbol, "sniper", uid)
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

        client = _get_client()

        # Get current price for PnL calculation
        exit_price = 0.0
        try:
            ticker     = client.get_ticker(symbol)
            exit_price = float(ticker.get("lastPrice", ticker.get("close", 0)))
        except Exception:
            pass

        # Get open position on the exchange
        positions = client.get_open_positions(symbol)
        pos = next((p for p in positions if float(p.get("qty", 0)) > 0), None)

        order_result = None
        if pos is not None:
            # Position still open → place market close order
            position_side = pos.get("side", "BUY")
            qty           = str(pos.get("qty", "0"))
            close_side    = "SELL" if position_side == "BUY" else "BUY"
            order_result = client.place_order(
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
    return jsonify(db.get_equity_curve(user_id=_current_user_id()))


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
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
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
