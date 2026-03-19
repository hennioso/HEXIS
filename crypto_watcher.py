"""
HEXIS — Crypto Payment Watcher

Monitors a TRC20 wallet for incoming USDT / USDC payments.
Polls the TronGrid public API every 60 seconds (no API key required).

When a qualifying payment arrives (>= CRYPTO_MIN_USDT) and matches a
pending payment request, it:
  1. Generates an invite code
  2. Emails it to the buyer
  3. Records the transaction in the DB so it is never processed twice
"""

import logging
import secrets
import string
import time

import requests

import config
import database as db
import mailer

log = logging.getLogger("CryptoWatcher")

# TRC20 contract addresses on Tron mainnet
_CONTRACTS = {
    "USDT": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    "USDC": "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8",
}

_TRONGRID = "https://api.trongrid.io"


def _fetch_trc20(wallet: str, contract: str) -> list[dict]:
    """Fetch the 50 most recent incoming TRC20 token transfers for wallet."""
    url = f"{_TRONGRID}/v1/accounts/{wallet}/transactions/trc20"
    params = {"limit": 50, "contract_address": contract, "only_to": "true"}
    headers = {}
    if config.TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = config.TRONGRID_API_KEY
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as exc:
        log.warning(f"TronGrid request failed: {exc}")
        return []


def _new_invite_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


def check_payments() -> None:
    wallet = config.CRYPTO_WALLET_ADDRESS
    if not wallet:
        return

    for token, contract in _CONTRACTS.items():
        transfers = _fetch_trc20(wallet, contract)
        for tx in transfers:
            txid = tx.get("transaction_id")
            if not txid or db.is_crypto_payment_processed(txid):
                continue

            # USDT / USDC use 6 decimal places on Tron
            amount = int(tx.get("value", 0)) / 1_000_000
            to_addr = tx.get("to", "")

            if to_addr.lower() != wallet.lower():
                continue

            if amount < config.CRYPTO_MIN_USDT:
                log.debug(f"Skipping small payment: {amount:.2f} {token} txid={txid}")
                continue

            # Try to match to a pending payment by expected amount
            pending = db.match_pending_payment(amount)
            if pending:
                code = _new_invite_code()
                db.create_invite_code(code, pending["email"])
                db.record_crypto_payment(
                    txid=txid,
                    from_address=tx.get("from", ""),
                    amount=amount,
                    token=token,
                    email=pending["email"],
                    invite_code=code,
                )
                db.delete_pending_payment(pending["id"])
                mailer.send_invite_code(pending["email"], code)
                log.info(
                    f"Payment matched: {amount:.2f} {token} txid={txid[:16]}… "
                    f"→ invite code sent to {pending['email']}"
                )
            else:
                # No pending request matched — record for manual review
                db.record_crypto_payment(
                    txid=txid,
                    from_address=tx.get("from", ""),
                    amount=amount,
                    token=token,
                    email=None,
                    invite_code=None,
                )
                log.warning(
                    f"Unmatched payment: {amount:.2f} {token} txid={txid[:16]}… "
                    f"— no pending payment request found (manual review needed)"
                )


def watcher_loop(stop_event) -> None:
    wallet = config.CRYPTO_WALLET_ADDRESS
    if not wallet:
        log.info("CRYPTO_WALLET_ADDRESS not configured — watcher disabled.")
        return
    log.info(f"Crypto watcher started — monitoring {wallet} on TRC20 (USDT + USDC)")
    while not stop_event.is_set():
        try:
            check_payments()
        except Exception as exc:
            log.error(f"Unexpected watcher error: {exc}", exc_info=True)
        stop_event.wait(60)
