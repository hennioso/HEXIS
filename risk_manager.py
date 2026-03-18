"""
Risk Management
Calculates position size, stop loss, and take profit.
"""

from dataclasses import dataclass


@dataclass
class TradeParams:
    qty: str              # Amount in base coin (as string for API)
    tp_price: str         # Take profit price
    sl_price: str         # Stop loss price
    entry_price: float
    notional_usdt: float  # Approximate trade value in USDT


class RiskManager:
    def __init__(
        self,
        position_margin_pct: float = 0.075, # Margin per trade as % of total equity (7.5%)
        risk_per_trade: float = 0.02,
        stop_loss_pct: float = 0.015,
        take_profit_pct: float = 0.030,
        leverage: int = 10,
        min_qty: float = 0.001,
        qty_precision: int = 3,
        price_precision: int = 1,
        max_margin_usdt: float = None,   # Margin cap in USDT for learning phase
        max_margin_trades: int = 0,      # Number of trades the cap applies to
        max_margin_pct: float = 0.05,    # Hard cap: margin never exceeds this % of balance
    ):
        self.position_margin_pct = position_margin_pct
        self.risk_per_trade = risk_per_trade
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.leverage = leverage
        self.min_qty = min_qty
        self.qty_precision = qty_precision
        self.price_precision = price_precision
        self.max_margin_usdt = max_margin_usdt
        self.max_margin_trades = max_margin_trades
        self.max_margin_pct = max_margin_pct

    def calculate(
        self,
        direction: str,
        entry_price: float,
        available_balance: float,   # total equity passed here
        trade_count: int = 0,
        sl_price_override: float = None,
    ) -> TradeParams | None:
        """
        Calculates qty, TP and SL for a trade.
        Uses equity-based sizing: margin = total_equity × position_margin_pct.
        Returns None if the position size is too small.
        """
        # Equity-based sizing: fixed % of total equity as margin
        margin_usdt = available_balance * self.position_margin_pct
        qty = round((margin_usdt * self.leverage) / entry_price, self.qty_precision)

        # Learning phase cap: first N trades limited to max_margin_usdt
        if (
            self.max_margin_usdt is not None
            and self.max_margin_trades > 0
            and trade_count < self.max_margin_trades
        ):
            max_qty_from_cap = round(
                (self.max_margin_usdt * self.leverage) / entry_price,
                self.qty_precision,
            )
            if qty > max_qty_from_cap:
                qty = max_qty_from_cap

        if qty < self.min_qty:
            return None  # Position too small

        # Notional value (without leverage)
        notional = qty * entry_price

        # Safety check: notional must not exceed available_balance * leverage
        max_notional = available_balance * self.leverage
        if notional > max_notional:
            qty = round(max_notional / entry_price, self.qty_precision)
            if qty < self.min_qty:
                return None

        # Dynamic price precision based on price magnitude
        # BTC ~85000 -> 1, ETH ~2000 -> 2, SOL ~85 -> 3, XRP ~1.3 -> 4, DOGE ~0.1 -> 5
        if entry_price >= 10_000:
            price_prec = 1
        elif entry_price >= 100:
            price_prec = 2
        elif entry_price >= 1:
            price_prec = 4
        else:
            price_prec = 5

        # Stop loss & take profit
        if sl_price_override is not None:
            sl_price = sl_price_override
            # TP keeps the standard ratio but derived from the actual SL distance
            sl_dist_pct = abs(entry_price - sl_price) / entry_price
            tp_mult = self.take_profit_pct / self.stop_loss_pct  # e.g. 2.0 for 2:1 R:R
            if direction == "long":
                tp_price = entry_price * (1 + sl_dist_pct * tp_mult)
            else:
                tp_price = entry_price * (1 - sl_dist_pct * tp_mult)
        elif direction == "long":
            sl_price = entry_price * (1 - self.stop_loss_pct)
            tp_price = entry_price * (1 + self.take_profit_pct)
        else:  # short
            sl_price = entry_price * (1 + self.stop_loss_pct)
            tp_price = entry_price * (1 - self.take_profit_pct)

        return TradeParams(
            qty=str(qty),
            tp_price=str(round(tp_price, price_prec)),
            sl_price=str(round(sl_price, price_prec)),
            entry_price=entry_price,
            notional_usdt=round(qty * entry_price, 2),
        )
