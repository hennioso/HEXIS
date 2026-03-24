"""
Hyperliquid API Connector
Provides the same interface as BitunixClient so Trader/strategies work unchanged.
Uses the official hyperliquid-python-sdk for signing + requests.

Symbol mapping: BTCUSDT → BTC, ETHUSDT → ETH, etc.
Authentication: Ethereum private key (no separate API key/secret).
"""

import time
import logging
from typing import Optional

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hyperliquid.xyz"

# Symbols that Hyperliquid does NOT support (e.g. metals)
_UNSUPPORTED: set[str] = {"XAUTUSDT", "XAGUSDT"}

# Qty decimal precision per coin on Hyperliquid (szDecimals from meta)
_SZ_DECIMALS: dict[str, int] = {
    "BTC":  5,
    "ETH":  4,
    "SOL":  2,
    "XRP":  0,
    "BNB":  3,
    "HYPE": 2,
    "ADA":  0,
}

# Maximum leverage per coin on Hyperliquid
_MAX_LEVERAGE: dict[str, int] = {
    "BTC":  40,
    "ETH":  25,
    "SOL":  20,
    "XRP":  20,
    "BNB":  10,
    "HYPE": 10,
    "ADA":  10,
}

# Interval string map  Bitunix → Hyperliquid (same notation)
_INTERVAL_MS: dict[str, int] = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}


def _sym(bitunix_symbol: str) -> str:
    """BTCUSDT → BTC"""
    return bitunix_symbol.replace("USDT", "")


class HyperliquidClient:
    """
    Drop-in replacement for BitunixClient targeting Hyperliquid perps.

    constructor args:
        wallet_address  – Ethereum address (0x…)
        private_key     – hex private key for signing (never stored plaintext)
    """

    def __init__(self, wallet_address: str, private_key: str):
        self.wallet_address = wallet_address.lower()
        self._private_key   = private_key
        self._info          = Info(BASE_URL, skip_ws=True)
        self._exchange_obj: Optional[Exchange] = None
        # Track TP/SL order IDs per coin so we can cancel/replace them
        # {coin: {"tp_oid": int|None, "sl_oid": int|None}}
        self._tpsl_oids: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _exc(self) -> Exchange:
        """Lazy-init Exchange (needs private key → eth_account.Account)."""
        if self._exchange_obj is None:
            from eth_account import Account
            account = Account.from_key(self._private_key)
            self._exchange_obj = Exchange(account, BASE_URL)
        return self._exchange_obj

    def is_supported(self, symbol: str) -> bool:
        """Return False for symbols not available on Hyperliquid (e.g. metals)."""
        return symbol not in _UNSUPPORTED

    def _check_supported(self, symbol: str):
        if symbol in _UNSUPPORTED:
            raise ValueError(f"Hyperliquid does not support {symbol}")

    def _trigger_order(self, coin: str, is_buy: bool, sz: float,
                       trigger_px: float, tpsl: str) -> Optional[int]:
        """
        Place a TP or SL trigger order. Returns the oid or None on failure.
        tpsl: 'tp' | 'sl'
        """
        # Slippage limit: for tp (sell) go 5% below trigger; for sl go 5% below too
        limit_px = trigger_px * 0.95 if not is_buy else trigger_px * 1.05
        try:
            res = self._exc().order(
                coin,
                is_buy=is_buy,
                sz=sz,
                limit_px=round(limit_px, 6),
                order_type={"trigger": {
                    "triggerPx": str(trigger_px),
                    "isMarket": True,
                    "tpsl": tpsl,
                }},
                reduce_only=True,
            )
            statuses = (res.get("response", {})
                           .get("data", {})
                           .get("statuses", [{}]))
            oid = (statuses[0].get("resting", {}) or
                   statuses[0].get("triggered", {}) or {}).get("oid")
            return oid
        except Exception as e:
            logger.warning(f"HL trigger order ({tpsl}) failed for {coin}: {e}")
            return None

    def _cancel_oid(self, coin: str, oid: int):
        try:
            self._exc().cancel(coin, oid)
        except Exception as e:
            logger.debug(f"HL cancel oid {oid} for {coin}: {e}")

    # ------------------------------------------------------------------
    # Market Data  (read-only, no auth needed)
    # ------------------------------------------------------------------

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        """Fetch OHLCV candles. Returns Bitunix-compatible list of dicts."""
        self._check_supported(symbol)
        coin = _sym(symbol)
        interval_ms = _INTERVAL_MS.get(interval, 300_000)
        now   = int(time.time() * 1000)
        start = now - limit * interval_ms * 2  # fetch 2× to ensure we have enough

        candles = self._info.candles_snapshot(coin, interval, start, now)
        candles = candles[-limit:]  # trim to requested limit

        return [{
            "time":     c["t"],
            "open":     c["o"],
            "high":     c["h"],
            "low":      c["l"],
            "close":    c["c"],
            "baseVol":  c["v"],
            "quoteVol": str(float(c["v"]) * float(c["c"])),
        } for c in candles]

    def get_ticker(self, symbol: str) -> dict:
        """Return latest mid price. Bitunix-compatible ticker dict."""
        self._check_supported(symbol)
        coin  = _sym(symbol)
        mids  = self._info.all_mids()
        price = mids.get(coin, "0")
        return {
            "symbol":     symbol,
            "lastPr":     price,
            "lastPrice":  price,
            "indexPrice": price,
            "markPrice":  price,
            "close":      price,
        }

    def get_orderbook(self, symbol: str, limit: int = 5) -> dict:
        """Return top-N bids/asks. Returns Bitunix-compatible dict."""
        self._check_supported(symbol)
        coin  = _sym(symbol)
        snap  = self._info.l2_snapshot(coin)
        levels = snap.get("levels", [[], []])
        bids = [[lvl["px"], lvl["sz"]] for lvl in levels[0][:limit]]
        asks = [[lvl["px"], lvl["sz"]] for lvl in levels[1][:limit]]
        return {"bids": bids, "asks": asks}

    def get_mark_price(self, symbol: str) -> dict:
        self._check_supported(symbol)
        coin  = _sym(symbol)
        mids  = self._info.all_mids()
        price = mids.get(coin, "0")
        return {"markPrice": price}

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_balance(self, margin_coin: str = "USDT") -> dict:
        """Return Bitunix-compatible balance dict."""
        state   = self._info.user_state(self.wallet_address)
        summary = state.get("marginSummary", {})
        equity  = float(summary.get("accountValue", 0))
        used    = float(summary.get("totalMarginUsed", 0))
        avail   = max(equity - used, 0.0)
        return {
            "available":      str(round(avail, 4)),
            "crossedBalance": str(round(equity, 4)),
            "equity":         str(round(equity, 4)),
            "marginCoin":     "USDT",
        }

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_open_positions(self, symbol: str = None) -> list[dict]:
        """Return open perp positions. Bitunix-compatible list."""
        state = self._info.user_state(self.wallet_address)
        out   = []
        for ap in state.get("assetPositions", []):
            pos  = ap.get("position", {})
            szi  = float(pos.get("szi", 0))
            if szi == 0:
                continue
            coin      = pos["coin"]
            hl_symbol = coin + "USDT"
            if symbol and hl_symbol != symbol:
                continue
            lev_info  = pos.get("leverage", {})
            leverage  = int(lev_info.get("value", 1)) if isinstance(lev_info, dict) else 1
            out.append({
                "symbol":      hl_symbol,
                "side":        "long" if szi > 0 else "short",
                "qty":         str(abs(szi)),
                "openPrice":   pos.get("entryPx", "0"),
                "unrealizedPL": pos.get("unrealizedPnl", "0"),
                "leverage":    leverage,
                "positionId":  coin,      # coin name used as position identifier
                "margin":      pos.get("marginUsed", "0"),
                "liquidationPx": pos.get("liquidationPx", "0"),
            })
        return out

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,           # 'BUY' | 'SELL'
        trade_side: str,     # 'OPEN' | 'CLOSE'
        qty: str,
        order_type: str = "MARKET",
        price: str = None,
        tp_price: str = None,
        sl_price: str = None,
        reduce_only: bool = False,
        client_id: str = None,
        leverage: int = None,
    ) -> dict:
        """Place a market order. Returns dict with orderId + avgPrice."""
        self._check_supported(symbol)
        coin   = _sym(symbol)
        exc    = self._exc()
        sz     = float(qty)
        is_buy = (side.upper() == "BUY")

        if trade_side.upper() == "OPEN":
            # Set leverage before opening — respect HL per-asset max
            import config as _cfg
            desired = leverage or _cfg.LEVERAGE or 10
            max_lev = _MAX_LEVERAGE.get(coin, 10)
            lev = min(desired, max_lev)
            try:
                exc.update_leverage(lev, coin, is_cross=False)
            except Exception as e:
                logger.warning(f"HL set leverage failed for {coin}: {e}")

            res = exc.market_open(coin, is_buy=is_buy, sz=sz)
        else:
            # CLOSE
            res = exc.market_close(coin, sz=sz)

        # Parse response
        statuses  = (res.get("response", {})
                        .get("data", {})
                        .get("statuses", [{}]))
        filled    = statuses[0].get("filled", {}) if statuses else {}
        oid       = str(filled.get("oid", ""))
        avg_price = str(filled.get("avgPx", price or "0"))

        logger.debug(f"HL place_order {coin} {side}/{trade_side} sz={sz} oid={oid} avg={avg_price}")

        # Place TP / SL trigger orders after opening
        if trade_side.upper() == "OPEN":
            # For a LONG open: TP=sell, SL=sell; for SHORT: TP=buy, SL=buy
            close_is_buy = not is_buy
            oids = self._tpsl_oids.setdefault(coin, {"tp_oid": None, "sl_oid": None})

            if tp_price:
                tp_oid = self._trigger_order(coin, close_is_buy, sz,
                                              float(tp_price), "tp")
                oids["tp_oid"] = tp_oid

            if sl_price:
                sl_oid = self._trigger_order(coin, close_is_buy, sz,
                                              float(sl_price), "sl")
                oids["sl_oid"] = sl_oid

        return {"orderId": oid, "avgPrice": avg_price}

    def modify_position_sl(self, symbol: str, position_id: str, sl_price: str) -> dict:
        """
        Cancel existing SL order and place a new one (move SL to break-even).
        position_id is the coin name on Hyperliquid.
        """
        coin = _sym(symbol)
        oids = self._tpsl_oids.get(coin, {})

        # Cancel old SL
        old_sl = oids.get("sl_oid")
        if old_sl:
            self._cancel_oid(coin, old_sl)

        # Determine position size and direction from open positions
        positions = self.get_open_positions(symbol)
        if not positions:
            return {}
        pos      = positions[0]
        szi      = float(pos["qty"])
        is_short = pos["side"] == "short"
        close_buy = is_short  # closing a short = buy

        new_sl_oid = self._trigger_order(coin, close_buy, szi, float(sl_price), "sl")
        oids["sl_oid"] = new_sl_oid
        return {"slOid": new_sl_oid}

    def place_position_tpsl(self, symbol: str, position_id: str,
                             sl_price: str = None, tp_price: str = None,
                             sl_qty: str = None, tp_qty: str = None) -> dict:
        """Place TP/SL for an existing position (e.g. after BE move)."""
        coin      = _sym(symbol)
        positions = self.get_open_positions(symbol)
        if not positions:
            return {}
        pos       = positions[0]
        sz        = float(sl_qty or tp_qty or pos["qty"])
        is_short  = pos["side"] == "short"
        close_buy = is_short

        oids = self._tpsl_oids.setdefault(coin, {"tp_oid": None, "sl_oid": None})

        result: dict = {}
        if tp_price:
            tp_oid = self._trigger_order(coin, close_buy, sz, float(tp_price), "tp")
            oids["tp_oid"] = tp_oid
            result["tpOid"] = tp_oid
        if sl_price:
            sl_oid = self._trigger_order(coin, close_buy, sz, float(sl_price), "sl")
            oids["sl_oid"] = sl_oid
            result["slOid"] = sl_oid
        return result

    def cancel_all_orders(self, symbol: str) -> dict:
        """Cancel all open orders for a symbol."""
        coin   = _sym(symbol)
        orders = self._info.open_orders(self.wallet_address)
        oids_to_cancel = [
            {"coin": o["coin"], "oid": o["oid"]}
            for o in orders
            if o.get("coin") == coin
        ]
        if oids_to_cancel:
            self._exc().bulk_cancel(oids_to_cancel)
        self._tpsl_oids.pop(coin, None)
        return {}

    def get_pending_orders(self, symbol: str = None) -> list[dict]:
        """Return all open orders, Bitunix-compatible."""
        orders = self._info.open_orders(self.wallet_address)
        if symbol:
            coin   = _sym(symbol)
            orders = [o for o in orders if o.get("coin") == coin]
        return [{
            "orderId":   str(o.get("oid", "")),
            "symbol":    o.get("coin", "") + "USDT",
            "side":      "BUY" if o.get("side") == "A" else "SELL",
            "qty":       str(o.get("sz", "")),
            "price":     str(o.get("limitPx", "")),
            "status":    "PENDING",
        } for o in orders]

    def get_order_history(self, symbol: str, limit: int = 20) -> list[dict]:
        """
        Return recent fills for a symbol. Bitunix-compatible.
        Used by _fetch_fill_price() in trader.py.
        """
        coin  = _sym(symbol)
        fills = self._info.user_fills(self.wallet_address)
        # filter by coin and take most recent
        fills = [f for f in fills if f.get("coin") == coin][-limit:]
        return [{
            "orderId":   str(f.get("oid", "")),
            "avgPrice":  str(f.get("px", "0")),
            "qty":       str(f.get("sz", "0")),
            "tradeSide": "OPEN" if f.get("dir", "") in ("Open Long", "Open Short") else "CLOSE",
            "status":    "FILLED",
            "updateTime": str(f.get("time", "")),
        } for f in fills]

    def get_history_positions(self, symbol: str = None, limit: int = 100) -> list[dict]:
        """
        Return recently closed positions/fills. Bitunix-compatible.
        Used by hourly reconciliation in main.py.
        """
        fills = self._info.user_fills(self.wallet_address)
        if symbol:
            coin  = _sym(symbol)
            fills = [f for f in fills if f.get("coin") == coin]
        # Group by closedPnl fills only (direction contains "Close")
        closed = [f for f in fills if "Close" in f.get("dir", "")][-limit:]
        return [{
            "symbol":     f.get("coin", "") + "USDT",
            "side":       "long" if "Long" in f.get("dir", "") else "short",
            "closePrice": str(f.get("px", "0")),
            "realizedPnl": str(f.get("closedPnl", "0")),
            "closeTime":  str(f.get("time", "")),
            "qty":        str(f.get("sz", "0")),
        } for f in closed]
