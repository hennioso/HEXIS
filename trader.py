"""
Trader: connects strategy, risk manager, and exchange.
Executes orders and manages the current position.
"""

import logging
import uuid
from typing import Optional

from exchange import BitunixClient
from strategy import Signal
from risk_manager import RiskManager, TradeParams
from strategy_sniper import SniperSignal
import database as db

logger = logging.getLogger(__name__)


def _qty_str(qty: float) -> str:
    """Format qty without trailing .0 for integer quantities (e.g. 188.0 → '188')."""
    return str(int(qty)) if qty == int(qty) else str(qty)


class Trader:
    def __init__(self, client: BitunixClient, risk_manager: RiskManager, symbol: str):
        self.client = client
        self.rm = risk_manager
        self.symbol = symbol
        self.current_position: Optional[dict] = None
        self._current_trade_id: Optional[str] = None  # DB trade ID of the open position
        db.init_db()
        self._recover_open_position()

    def _recover_open_position(self):
        """
        On startup, check if there's an open DB trade for this symbol that still
        has a live position on the exchange. If so, restore _current_trade_id so
        that monitor_sniper_tps() continues working after a bot restart.
        """
        open_trades = [
            t for t in db.get_all_trades(limit=50)
            if t["status"] == "open" and t["symbol"] == self.symbol
        ]
        if not open_trades:
            return

        # Verify the position is still live on the exchange
        try:
            pos = self.refresh_position()
        except Exception:
            return

        if pos is None:
            return

        # Restore the most recent open trade ID
        trade = open_trades[0]
        self._current_trade_id = trade["trade_id"]
        logger.info(
            f"Position recovered after restart | trade_id: {trade['trade_id']} | "
            f"strategy: {trade.get('strategy', 'unknown')} | "
            f"Qty: {pos.get('qty')} | Side: {pos.get('side')}"
        )

    def refresh_position(self) -> Optional[dict]:
        """Fetches the current open position from the exchange."""
        positions = self.client.get_open_positions(self.symbol)
        if positions:
            for pos in positions:
                if float(pos.get("qty", 0)) > 0:
                    self.current_position = pos
                    return pos
        self.current_position = None
        return None

    def has_open_position(self) -> bool:
        prev_trade_id = self._current_trade_id
        pos = self.refresh_position()
        if pos is None and prev_trade_id:
            # Position was closed externally (TP/SL hit) -> sync DB
            self._sync_closed_position(prev_trade_id)
        return pos is not None

    def _sync_closed_position(self, trade_id: str):
        """Detects externally closed positions and updates status + PnL in DB."""
        trade = db.get_trade(trade_id)

        # Try to get actual exit price from order history (most accurate)
        exit_price = self._get_actual_exit_price(trade_id, trade)

        # Fallback: current ticker price (less accurate — price may have moved)
        if not exit_price:
            try:
                ticker = self.client.get_ticker(self.symbol)
                exit_price = float(ticker.get("lastPrice", ticker.get("close", 0)))
            except Exception:
                exit_price = 0.0

        if not exit_price:
            self._current_trade_id = None
            return

        # Determine TP or SL status from actual exit price vs stored levels
        status = "closed"
        if trade:
            tp = float(trade.get("tp_price") or 0)
            sl = float(trade.get("sl_price") or 0)
            direction = trade.get("direction", "long")
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

        db.close_trade(trade_id=trade_id, exit_price=exit_price, status=status)
        logger.info(
            f"Externally closed position detected | Status: {status} | "
            f"Exit: {exit_price:.4f} | trade_id: {trade_id}"
        )
        self._current_trade_id = None

    def _get_actual_exit_price(self, trade_id: str, trade: dict | None) -> float:
        """
        Attempts to find the actual execution price of the last CLOSE order
        from Bitunix order history. Returns 0.0 if not found.
        """
        if not trade:
            return 0.0
        try:
            orders = self.client.get_order_history(self.symbol, limit=20)
            entry_time = trade.get("entry_time", "")
            # Look for the most recent filled CLOSE order after entry
            for order in orders:
                order_trade_side = order.get("tradeSide", order.get("trade_side", ""))
                order_status = order.get("status", "")
                avg_price = float(order.get("avgPrice", order.get("avg_price", 0)) or 0)
                order_time = order.get("updateTime", order.get("createTime", ""))

                if (
                    order_trade_side.upper() == "CLOSE"
                    and order_status.upper() in ("FILLED", "FULL_FILLED")
                    and avg_price > 0
                    and str(order_time) >= entry_time.replace("-", "").replace("T", "").replace(":", "")[:12]
                ):
                    logger.debug(f"Actual exit price from order history: {avg_price:.4f}")
                    return avg_price
        except Exception as e:
            logger.debug(f"Order history lookup failed: {e}")
        return 0.0

    def get_available_balance(self) -> float:
        balance = self.client.get_balance("USDT")
        return float(balance.get("available", 0))

    def open_position(self, signal: Signal, sl_price_override: float = None) -> Optional[dict]:
        """
        Opens a new position based on the signal.
        Returns the order response or None on error.
        """
        if self.has_open_position():
            logger.warning("Position already open – skipping new trade.")
            return None

        # DB-level duplicate check (catches edge cases where exchange and DB are out of sync)
        existing = [t for t in db.get_all_trades(limit=20)
                    if t["status"] == "open" and t["symbol"] == self.symbol]
        if existing:
            logger.warning(f"DB already has an open trade for {self.symbol} – skipping duplicate.")
            return None

        balance = self.get_available_balance()
        if balance < 10:
            logger.error(f"Insufficient capital: {balance:.2f} USDT")
            return None

        trade_count = db.get_trade_count()
        trade_params = self.rm.calculate(
            direction=signal.direction,
            entry_price=signal.price,
            available_balance=balance,
            trade_count=trade_count,
            sl_price_override=sl_price_override,
        )
        if trade_params is None:
            logger.warning("Position size too small – trade skipped.")
            return None

        side = "BUY" if signal.direction == "long" else "SELL"
        trade_id = f"bot_{uuid.uuid4().hex[:16]}"

        logger.info(
            f"TRADE OPEN | {signal.direction.upper()} | "
            f"Qty: {trade_params.qty} | "
            f"~{trade_params.notional_usdt:.0f} USDT Notional | "
            f"Entry: {trade_params.entry_price:.4f} | "
            f"TP: {trade_params.tp_price} | SL: {trade_params.sl_price} | "
            f"RSI(5m): {signal.rsi_5m:.1f} | Trend(15m): {signal.trend_15m}"
        )

        try:
            result = self.client.place_order(
                symbol=self.symbol,
                side=side,
                trade_side="OPEN",
                qty=trade_params.qty,
                order_type="MARKET",
                tp_price=trade_params.tp_price,
                sl_price=trade_params.sl_price,
                client_id=trade_id,
            )
            order_id = result.get("orderId", "")
            logger.info(f"Order placed: {result}")

            # Save to database
            db.open_trade(
                trade_id=trade_id,
                order_id=order_id,
                symbol=self.symbol,
                direction=signal.direction,
                qty=float(trade_params.qty),
                entry_price=trade_params.entry_price,
                tp_price=float(trade_params.tp_price),
                sl_price=float(trade_params.sl_price),
                rsi_entry=signal.rsi_5m,
                trend_15m=signal.trend_15m,
            )
            self._current_trade_id = trade_id
            return result
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return None

    def open_sniper_position(self, sniper: SniperSignal) -> Optional[dict]:
        """
        Opens a SNIPER position at Fib 0.882 with a structural SL.
        No exchange TP is set — partial TPs are managed by monitor_sniper_tps().
        """
        if self.has_open_position():
            logger.warning("Position already open – skipping SNIPER trade.")
            return None

        existing = [t for t in db.get_all_trades(limit=20)
                    if t["status"] == "open" and t["symbol"] == self.symbol]
        if existing:
            logger.warning(f"DB already has an open trade for {self.symbol} – skipping SNIPER.")
            return None

        balance = self.get_available_balance()
        if balance < 10:
            logger.error(f"Insufficient capital: {balance:.2f} USDT")
            return None

        trade_count = db.get_trade_count()

        # Dynamic price precision
        entry_price = sniper.price
        if entry_price >= 10_000:
            price_prec = 1
        elif entry_price >= 100:
            price_prec = 2
        elif entry_price >= 1:
            price_prec = 4
        else:
            price_prec = 5

        # Position sizing: risk based on structural SL distance
        risk_usdt = balance * self.rm.risk_per_trade
        sl_dist = abs(entry_price - sniper.sl_price)
        if sl_dist == 0:
            return None
        qty = round(risk_usdt / sl_dist, self.rm.qty_precision)

        # Hard margin cap: never use more than max_margin_pct of balance per trade
        max_qty_margin = round(
            (balance * self.rm.max_margin_pct * self.rm.leverage) / entry_price,
            self.rm.qty_precision,
        )
        if qty > max_qty_margin:
            qty = max_qty_margin

        # Margin cap for learning phase
        if (
            self.rm.max_margin_usdt is not None
            and self.rm.max_margin_trades > 0
            and trade_count < self.rm.max_margin_trades
        ):
            max_qty = round(
                (self.rm.max_margin_usdt * self.rm.leverage) / entry_price,
                self.rm.qty_precision,
            )
            if qty > max_qty:
                qty = max_qty

        if qty < self.rm.min_qty:
            logger.warning("SNIPER position size too small – skipping.")
            return None

        # Safety: notional cap
        max_notional = balance * self.rm.leverage
        if qty * entry_price > max_notional:
            qty = round(max_notional / entry_price, self.rm.qty_precision)
            if qty < self.rm.min_qty:
                return None

        side = "BUY" if sniper.direction == "long" else "SELL"
        trade_id = f"sniper_{uuid.uuid4().hex[:16]}"
        sl_str = str(round(sniper.sl_price, price_prec))

        tp3_str = str(round(sniper.tp3_price, price_prec))

        logger.info(
            f"SNIPER OPEN | {sniper.direction.upper()} | "
            f"Qty: {qty} | Entry: {entry_price:.4f} | "
            f"SL: {sl_str} (structural) | "
            f"TP1: {sniper.tp1_price:.4f} (20%) | "
            f"TP2: {sniper.tp2_price:.4f} (50%) | "
            f"TP3: {sniper.tp3_price:.4f} (25%) | "
            f"Swing: {sniper.swing_low:.4f}–{sniper.swing_high:.4f}"
        )

        try:
            result = self.client.place_order(
                symbol=self.symbol,
                side=side,
                trade_side="OPEN",
                qty=str(qty),
                order_type="MARKET",
                sl_price=sl_str,
                client_id=trade_id,
            )
            order_id = result.get("orderId", "")
            logger.info(f"SNIPER order placed: {result}")

            db.open_trade(
                trade_id=trade_id,
                order_id=order_id,
                symbol=self.symbol,
                direction=sniper.direction,
                qty=float(qty),
                entry_price=entry_price,
                tp_price=sniper.tp3_price,   # store TP3 as the final reference TP
                sl_price=sniper.sl_price,
                strategy="sniper",
                tp1_price=sniper.tp1_price,
                tp2_price=sniper.tp2_price,
                tp3_price=sniper.tp3_price,
            )
            self._current_trade_id = trade_id
            return result
        except Exception as e:
            logger.error(f"SNIPER order failed: {e}")
            return None

    def monitor_sniper_tps(self) -> None:
        """
        Called every loop tick when a SNIPER position is open.
        Checks TP1/TP2/TP3 levels and executes partial closes:
          - TP1 (Fib 0.786) → close 20% + move SL to Break Even
          - TP2 (Fib 0.650) → close 50%
          - TP3 (Fib 0.500) → close 25%
          - 5% stays open (protected by BE stop)
        """
        trade_id = self._current_trade_id
        if not trade_id:
            return

        trade = db.get_trade(trade_id)
        if not trade or trade.get("strategy") != "sniper":
            return

        # Fetch current price
        try:
            ticker = self.client.get_ticker(self.symbol)
            price = float(ticker.get("lastPrice", ticker.get("close", 0)))
        except Exception:
            return

        if not price:
            return

        direction   = trade["direction"]
        entry_price = float(trade["entry_price"])
        qty_total   = float(trade["qty"])
        tp1_price   = float(trade["tp1_price"])
        tp2_price   = float(trade["tp2_price"])
        tp3_price   = float(trade["tp3_price"])
        tp1_hit     = bool(trade.get("tp1_hit", 0))
        tp2_hit     = bool(trade.get("tp2_hit", 0))
        tp3_hit     = bool(trade.get("tp3_hit", 0))
        be_moved    = bool(trade.get("be_moved", 0))

        def _tp_reached(tp: float) -> bool:
            if direction == "long":
                return price >= tp
            return price <= tp

        close_side = "SELL" if direction == "long" else "BUY"

        # Dynamic price precision
        if entry_price >= 10_000:
            price_prec = 1
        elif entry_price >= 100:
            price_prec = 2
        elif entry_price >= 1:
            price_prec = 4
        else:
            price_prec = 5

        # --- TP1: 20% close + move SL to Break Even ---
        if not tp1_hit and _tp_reached(tp1_price):
            qty_tp1 = round(qty_total * 0.20, self.rm.qty_precision)
            if qty_tp1 >= self.rm.min_qty:
                try:
                    self.client.place_order(
                        symbol=self.symbol,
                        side=close_side,
                        trade_side="OPEN",
                        qty=_qty_str(qty_tp1),
                        order_type="MARKET",
                        reduce_only=True,
                    )
                    db.mark_sniper_tp(trade_id, 1)
                    logger.info(
                        f"SNIPER TP1 | {self.symbol} | Closed 20% ({qty_tp1}) @ {price:.4f}"
                    )
                except Exception as e:
                    logger.error(f"SNIPER TP1 close failed: {e}")
                    return

            # Move SL to Break Even
            if not be_moved:
                be_sl = str(round(entry_price, price_prec))
                pos_side = "BUY" if direction == "long" else "SELL"
                try:
                    self.client.set_position_sl(self.symbol, be_sl, side=pos_side)
                    db.mark_sniper_be_moved(trade_id, entry_price)
                    logger.info(
                        f"SNIPER SL → BE | {self.symbol} | SL moved to {be_sl}"
                    )
                except Exception as e:
                    logger.warning(f"SNIPER BE move failed (non-critical): {e}")

        # --- TP2: 50% close ---
        elif tp1_hit and not tp2_hit and _tp_reached(tp2_price):
            qty_tp2 = round(qty_total * 0.50, self.rm.qty_precision)
            if qty_tp2 >= self.rm.min_qty:
                try:
                    self.client.place_order(
                        symbol=self.symbol,
                        side=close_side,
                        trade_side="OPEN",
                        qty=_qty_str(qty_tp2),
                        order_type="MARKET",
                        reduce_only=True,
                    )
                    db.mark_sniper_tp(trade_id, 2)
                    logger.info(
                        f"SNIPER TP2 | {self.symbol} | Closed 50% ({qty_tp2}) @ {price:.4f}"
                    )
                except Exception as e:
                    logger.error(f"SNIPER TP2 close failed: {e}")

        # --- TP3: 25% close ---
        elif tp1_hit and tp2_hit and not tp3_hit and _tp_reached(tp3_price):
            qty_tp3 = round(qty_total * 0.25, self.rm.qty_precision)
            if qty_tp3 >= self.rm.min_qty:
                try:
                    self.client.place_order(
                        symbol=self.symbol,
                        side=close_side,
                        trade_side="OPEN",
                        qty=_qty_str(qty_tp3),
                        order_type="MARKET",
                        reduce_only=True,
                    )
                    db.mark_sniper_tp(trade_id, 3)
                    logger.info(
                        f"SNIPER TP3 | {self.symbol} | Closed 25% ({qty_tp3}) @ {price:.4f} | "
                        f"5% running open-end with BE stop"
                    )
                except Exception as e:
                    logger.error(f"SNIPER TP3 close failed: {e}")

    def close_position(self, reason: str = "manual", exit_price: float = None) -> Optional[dict]:
        """Closes the current open position with a market order."""
        pos = self.refresh_position()
        if pos is None:
            logger.info("No open position to close.")
            return None

        position_side = pos.get("side", "")
        qty = pos.get("qty", "0")
        close_side = "SELL" if position_side == "BUY" else "BUY"

        logger.info(f"TRADE CLOSE | Reason: {reason} | Qty: {qty} | Side: {close_side}")

        try:
            result = self.client.place_order(
                symbol=self.symbol,
                side=close_side,
                trade_side="CLOSE",
                qty=qty,
                order_type="MARKET",
                reduce_only=True,
            )
            logger.info(f"Position closed: {result}")

            # Update database
            if self._current_trade_id and exit_price:
                status_map = {"tp": "tp_hit", "sl": "sl_hit", "manual": "closed"}
                db.close_trade(
                    trade_id=self._current_trade_id,
                    exit_price=exit_price,
                    status=status_map.get(reason, "closed"),
                )
                self._current_trade_id = None

            self.current_position = None
            return result
        except Exception as e:
            logger.error(f"Close failed: {e}")
            return None
