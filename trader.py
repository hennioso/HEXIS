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
import database as db

logger = logging.getLogger(__name__)


class Trader:
    def __init__(self, client: BitunixClient, risk_manager: RiskManager, symbol: str):
        self.client = client
        self.rm = risk_manager
        self.symbol = symbol
        self.current_position: Optional[dict] = None
        self._current_trade_id: Optional[str] = None  # DB trade ID of the open position
        db.init_db()

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
        try:
            ticker = self.client.get_ticker(self.symbol)
            exit_price = float(ticker.get("lastPrice", ticker.get("close", 0)))
        except Exception:
            exit_price = 0.0

        if not exit_price:
            self._current_trade_id = None
            return

        # Determine TP or SL from DB entry
        trade = db.get_trade(trade_id)
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
