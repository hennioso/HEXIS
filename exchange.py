"""
Bitunix API Connector
Handles authentication and all HTTP requests to fapi.bitunix.com
"""

import hashlib
import time
import uuid
import json
import requests
from typing import Optional


BASE_URL = "https://fapi.bitunix.com"


class BitunixClient:
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "language": "en-US",
        })

    def _sign(self, nonce: str, timestamp: str, query_params: str, body: str) -> str:
        """
        Bitunix double-SHA256 signature:
          digest = SHA256(nonce + timestamp + apiKey + queryParams + body)
          sign   = SHA256(digest + secretKey)
        """
        digest_input = nonce + timestamp + self.api_key + query_params + body
        digest = hashlib.sha256(digest_input.encode()).hexdigest()
        sign = hashlib.sha256((digest + self.secret_key).encode()).hexdigest()
        return sign

    def _build_query_string(self, params: dict) -> str:
        """
        Sort params by key (ascending ASCII) and concatenate as key1value1key2value2.
        This is what Bitunix expects for the signature calculation.
        """
        if not params:
            return ""
        sorted_items = sorted(params.items(), key=lambda x: x[0])
        return "".join(f"{k}{v}" for k, v in sorted_items)

    def _auth_headers(self, query_params: dict = None, body: dict = None) -> dict:
        nonce = uuid.uuid4().hex  # 32-char random string
        timestamp = str(int(time.time() * 1000))

        query_str = self._build_query_string(query_params or {})
        body_str = json.dumps(body, separators=(",", ":")) if body else ""

        sign = self._sign(nonce, timestamp, query_str, body_str)

        return {
            "api-key": self.api_key,
            "sign": sign,
            "nonce": nonce,
            "timestamp": timestamp,
        }

    def _backoff(self, attempt: int, rate_limited: bool = False) -> float:
        """Exponential backoff: 1s, 2s, 4s. Rate limit: 10s, 20s, 40s."""
        base = 10 if rate_limited else 1
        return min(base * (2 ** attempt), 60)

    def _get(self, path: str, params: dict = None, _retries: int = 3) -> dict:
        headers = self._auth_headers(query_params=params)
        url = BASE_URL + path
        for attempt in range(_retries + 1):
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=20)

                if response.status_code == 429:
                    # Rate limited — back off longer, then retry
                    if attempt < _retries:
                        time.sleep(self._backoff(attempt, rate_limited=True))
                        headers = self._auth_headers(query_params=params)
                        continue
                    response.raise_for_status()

                if response.status_code >= 500:
                    # Server error — retry with backoff
                    if attempt < _retries:
                        time.sleep(self._backoff(attempt))
                        headers = self._auth_headers(query_params=params)
                        continue
                    response.raise_for_status()

                response.raise_for_status()
                data = response.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"Bitunix API Error [{data.get('code')}]: {data.get('msg')}")
                return data
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < _retries:
                    time.sleep(self._backoff(attempt))
                    headers = self._auth_headers(query_params=params)
                    continue
                raise

    def _post(self, path: str, body: dict, _retries: int = 3) -> dict:
        headers = self._auth_headers(body=body)
        url = BASE_URL + path
        body_str = json.dumps(body, separators=(",", ":"))
        for attempt in range(_retries + 1):
            try:
                response = self.session.post(url, data=body_str, headers=headers, timeout=20)

                if response.status_code == 429:
                    if attempt < _retries:
                        time.sleep(self._backoff(attempt, rate_limited=True))
                        headers = self._auth_headers(body=body)
                        continue
                    response.raise_for_status()

                if response.status_code >= 500:
                    if attempt < _retries:
                        time.sleep(self._backoff(attempt))
                        headers = self._auth_headers(body=body)
                        continue
                    response.raise_for_status()

                response.raise_for_status()
                data = response.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"Bitunix API Error [{data.get('code')}]: {data.get('msg')}")
                return data
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < _retries:
                    time.sleep(self._backoff(attempt))
                    headers = self._auth_headers(body=body)
                    continue
                raise

    # -------------------------------------------------------------------------
    # Market Data
    # -------------------------------------------------------------------------

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        """
        Fetch OHLCV candles.
        interval: '1m', '5m', '15m', '30m', '1h', ...
        Returns list of dicts with keys: time, open, high, low, close, baseVol, quoteVol
        """
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        data = self._get("/api/v1/futures/market/kline", params=params)
        return data["data"]

    def get_ticker(self, symbol: str) -> dict:
        """Get latest ticker price for a symbol."""
        params = {"symbol": symbol}
        data = self._get("/api/v1/futures/market/tickers", params=params)
        tickers = data["data"]
        if isinstance(tickers, list):
            for t in tickers:
                if t.get("symbol") == symbol:
                    return t
            return tickers[0] if tickers else {}
        return tickers

    # -------------------------------------------------------------------------
    # Account
    # -------------------------------------------------------------------------

    def get_balance(self, margin_coin: str = "USDT") -> dict:
        """Returns account balance info."""
        params = {"marginCoin": margin_coin}
        data = self._get("/api/v1/futures/account", params=params)
        return data["data"]

    # -------------------------------------------------------------------------
    # Positions
    # -------------------------------------------------------------------------

    def get_open_positions(self, symbol: str = None) -> list[dict]:
        """Returns list of currently open positions."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/position/get_pending_positions", params=params)
        result = data.get("data", [])
        # API returns either a list directly or {"positionList": [...]}
        if isinstance(result, list):
            return result
        return result.get("positionList", [])

    # -------------------------------------------------------------------------
    # Orders
    # -------------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        trade_side: str,
        qty: str,
        order_type: str = "MARKET",
        price: str = None,
        tp_price: str = None,
        sl_price: str = None,
        reduce_only: bool = False,
        client_id: str = None,
    ) -> dict:
        """
        Place a futures order.
        side:       'BUY' | 'SELL'
        trade_side: 'OPEN' | 'CLOSE'
        qty:        amount in base coin (BTC for BTCUSDT)
        """
        body = {
            "symbol": symbol,
            "side": side,
            "tradeSide": trade_side,
            "qty": qty,
            "orderType": order_type,
        }
        if price:
            body["price"] = price
        if tp_price:
            body["tpPrice"] = tp_price
            body["tpStopType"] = "MARK_PRICE"
            body["tpOrderType"] = "MARKET"
        if sl_price:
            body["slPrice"] = sl_price
            body["slStopType"] = "MARK_PRICE"
            body["slOrderType"] = "MARKET"
        if reduce_only:
            body["reduceOnly"] = True
        if client_id:
            body["clientId"] = client_id

        data = self._post("/api/v1/futures/trade/place_order", body=body)
        return data["data"]

    def modify_position_sl(self, symbol: str, position_id: str, sl_price: str) -> dict:
        """
        Modify the stop-loss of an existing position via the correct TPSL endpoint.
        Used to move SL to Break Even after TP1 is hit.
        Requires the positionId from get_open_positions().
        """
        body = {
            "symbol": symbol,
            "positionId": position_id,
            "slPrice": sl_price,
            "slStopType": "MARK_PRICE",
        }
        data = self._post("/api/v1/futures/tpsl/position/modify_order", body=body)
        return data.get("data", {})

    def place_position_tpsl(self, symbol: str, position_id: str,
                             sl_price: str = None, tp_price: str = None,
                             sl_qty: str = None, tp_qty: str = None) -> dict:
        """
        Place a new TP/SL order for an existing position.
        Used when no TP/SL was set at entry, or to add a BE stop after TP1.
        """
        body: dict = {
            "symbol": symbol,
            "positionId": position_id,
        }
        if tp_price:
            body["tpPrice"] = tp_price
            body["tpStopType"] = "MARK_PRICE"
            if tp_qty:
                body["tpQty"] = tp_qty
        if sl_price:
            body["slPrice"] = sl_price
            body["slStopType"] = "MARK_PRICE"
            if sl_qty:
                body["slQty"] = sl_qty
        data = self._post("/api/v1/futures/tpsl/place_order", body=body)
        return data.get("data", {})

    def get_history_positions(self, symbol: str = None, limit: int = 100) -> list[dict]:
        """
        Fetch recently closed positions from the exchange.
        Used for hourly reconciliation of closed trades.
        """
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        try:
            data = self._get("/api/v1/futures/position/get_history_positions", params=params)
            result = data.get("data", [])
            if isinstance(result, list):
                return result
            return result.get("positionList", result.get("list", []))
        except Exception:
            return []

    def get_order_history(self, symbol: str, limit: int = 20) -> list[dict]:
        """
        Fetch recent filled/closed orders for a symbol.
        Used to get the actual exit price of a closed position.
        """
        params = {"symbol": symbol, "limit": limit}
        try:
            data = self._get("/api/v1/futures/trade/get_order_list", params=params)
            result = data.get("data", [])
            if isinstance(result, list):
                return result
            return result.get("orderList", [])
        except Exception:
            return []

    def get_orderbook(self, symbol: str, limit: int = 5) -> dict:
        """
        Fetch the current order book (bids/asks) for a symbol.
        Returns dict with keys 'bids' and 'asks', each a list of [price, qty] strings.
        limit: depth levels to return (5, 10, 20, 50).
        """
        params = {"symbol": symbol, "limit": limit}
        data = self._get("/api/v1/futures/market/depth", params=params)
        return data.get("data", {"bids": [], "asks": []})

    def cancel_all_orders(self, symbol: str) -> dict:
        """Cancel all open orders for a symbol."""
        body = {"symbol": symbol}
        data = self._post("/api/v1/futures/trade/cancel_orders", body=body)
        return data.get("data", {})

    def get_pending_orders(self, symbol: str = None) -> list[dict]:
        """Get all open (pending) orders."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/futures/trade/get_pending_orders", params=params)
        result = data.get("data", [])
        if isinstance(result, list):
            return result
        return result.get("orderList", [])

    def get_mark_price(self, symbol: str) -> dict:
        """Fetch current mark price for a symbol."""
        params = {"symbol": symbol}
        data = self._get("/api/v1/futures/market/mark_price", params=params)
        return data.get("data", {})
