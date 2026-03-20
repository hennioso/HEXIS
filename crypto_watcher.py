"""
HEXIS — Crypto Payment Watcher

Monitors wallets on multiple chains for incoming USDT / USDC payments.
Supported networks (enable each by setting the wallet address in .env):

  TRC20  (Tron)     — CRYPTO_WALLET_TRX  — free TronGrid API, no key needed
  BASE              — CRYPTO_WALLET_EVM  — Basescan API key (free)
  Solana            — CRYPTO_WALLET_SOL  — Helius API key (free tier)
  ERC20  (Ethereum) — CRYPTO_WALLET_EVM  — Etherscan API key (free)
                       (same EVM address for ETH mainnet + BASE)

When a qualifying payment arrives (>= CRYPTO_MIN_USDT) and matches a
pending payment request, it:
  1. Generates an invite code
  2. Emails it to the buyer
  3. Records the transaction in the DB so it is never processed twice
"""

import logging
import secrets
import string

import requests

import config
import database as db
import mailer

log = logging.getLogger("CryptoWatcher")


# ── Contract addresses ────────────────────────────────────────────────────────

# TRC20 (Tron mainnet)
_TRX_CONTRACTS = {
    "USDT": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    "USDC": "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8",
}

# EVM — Ethereum mainnet
_ETH_CONTRACTS = {
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
}

# EVM — BASE mainnet
_BASE_CONTRACTS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # native USDC on Base
    "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",  # bridged USDT on Base
}

# Solana SPL
_SOL_MINTS = {
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}


# ── Chain fetchers ────────────────────────────────────────────────────────────

def _fetch_trc20(wallet: str) -> list[dict]:
    """Return normalised incoming transfers on Tron (USDT + USDC)."""
    import time as _time
    result = []
    headers = {}
    if config.TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = config.TRONGRID_API_KEY
    for i, (token, contract) in enumerate(_TRX_CONTRACTS.items()):
        if i > 0:
            _time.sleep(1.5)  # avoid back-to-back requests hitting rate limit
        try:
            r = requests.get(
                f"https://api.trongrid.io/v1/accounts/{wallet}/transactions/trc20",
                params={"limit": 50, "contract_address": contract, "only_to": "true"},
                headers=headers, timeout=15,
            )
            if r.status_code == 429:
                log.debug(f"TronGrid ({token}) rate limited — will retry next cycle.")
                continue
            r.raise_for_status()
            for tx in r.json().get("data", []):
                if tx.get("to", "").lower() != wallet.lower():
                    continue
                result.append({
                    "txid":   tx["transaction_id"],
                    "amount": int(tx.get("value", 0)) / 1_000_000,
                    "token":  token,
                    "from":   tx.get("from", ""),
                    "chain":  "TRC20",
                })
        except Exception as exc:
            log.debug(f"TronGrid ({token}) error: {exc}")
    return result


def _fetch_evm(wallet: str, chain: str) -> list[dict]:
    """Return normalised incoming ERC-20 transfers on Ethereum or BASE."""
    if chain == "BASE":
        base_url = "https://api.basescan.org/api"
        api_key  = config.BASESCAN_API_KEY
        contracts = _BASE_CONTRACTS
        decimals  = {"USDT": 6, "USDC": 6}
    else:  # ETH
        base_url = "https://api.etherscan.io/api"
        api_key  = config.ETHERSCAN_API_KEY
        contracts = _ETH_CONTRACTS
        decimals  = {"USDT": 6, "USDC": 6}

    if not api_key:
        return []

    result = []
    for token, contract in contracts.items():
        try:
            r = requests.get(base_url, params={
                "module":          "account",
                "action":          "tokentx",
                "address":         wallet,
                "contractaddress": contract,
                "sort":            "desc",
                "apikey":          api_key,
            }, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "1":
                continue
            for tx in data.get("result", []):
                if tx.get("to", "").lower() != wallet.lower():
                    continue
                dec = int(tx.get("tokenDecimal", decimals.get(token, 6)))
                result.append({
                    "txid":   tx["hash"],
                    "amount": int(tx["value"]) / (10 ** dec),
                    "token":  token,
                    "from":   tx.get("from", ""),
                    "chain":  chain,
                })
        except Exception as exc:
            log.warning(f"{chain} ({token}) error: {exc}")
    return result


def _fetch_solana(wallet: str) -> list[dict]:
    """Return normalised incoming SPL-token transfers on Solana via Helius API."""
    if not config.HELIUS_API_KEY:
        return []
    result = []
    try:
        r = requests.get(
            f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
            params={"api-key": config.HELIUS_API_KEY, "type": "TRANSFER", "limit": 50},
            timeout=15,
        )
        r.raise_for_status()
        for tx in r.json():
            for transfer in tx.get("tokenTransfers", []):
                if transfer.get("toUserAccount", "").lower() != wallet.lower():
                    continue
                mint = transfer.get("mint", "")
                token = None
                for name, m in _SOL_MINTS.items():
                    if m == mint:
                        token = name
                        break
                if not token:
                    continue
                result.append({
                    "txid":   tx["signature"],
                    "amount": float(transfer.get("tokenAmount", 0)),
                    "token":  token,
                    "from":   transfer.get("fromUserAccount", ""),
                    "chain":  "Solana",
                })
    except Exception as exc:
        log.warning(f"Helius (Solana) error: {exc}")
    return result


# ── Core logic ────────────────────────────────────────────────────────────────

def _new_invite_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


def _process_transfer(tx: dict) -> None:
    """Match one transfer against pending payments and handle accordingly."""
    txid   = tx["txid"]
    amount = tx["amount"]
    token  = tx["token"]
    chain  = tx["chain"]

    if db.is_crypto_payment_processed(txid):
        return
    if amount < config.CRYPTO_MIN_USDT:
        log.debug(f"Skipping small payment: {amount:.2f} {token} on {chain} txid={txid[:16]}…")
        return

    pending = db.match_pending_payment(amount)
    if pending:
        code = _new_invite_code()
        db.create_invite_code(code, pending["email"])
        db.record_crypto_payment(
            txid=txid,
            from_address=tx["from"],
            amount=amount,
            token=f"{token} ({chain})",
            email=pending["email"],
            invite_code=code,
        )
        db.delete_pending_payment(pending["id"])
        mailer.send_invite_code(pending["email"], code)
        log.info(
            f"Payment matched: {amount:.2f} {token} on {chain} txid={txid[:16]}… "
            f"→ invite code sent to {pending['email']}"
        )
    else:
        db.record_crypto_payment(
            txid=txid,
            from_address=tx["from"],
            amount=amount,
            token=f"{token} ({chain})",
            email=None,
            invite_code=None,
        )
        log.warning(
            f"Unmatched payment: {amount:.2f} {token} on {chain} txid={txid[:16]}… "
            f"— no pending payment request found (manual review needed)"
        )


def check_payments(skip_trc20: bool = False) -> None:
    transfers: list[dict] = []

    if config.CRYPTO_WALLET_TRX and not skip_trc20:
        transfers += _fetch_trc20(config.CRYPTO_WALLET_TRX)

    if config.CRYPTO_WALLET_EVM:
        transfers += _fetch_evm(config.CRYPTO_WALLET_EVM, "BASE")
        if config.ETHERSCAN_API_KEY:
            transfers += _fetch_evm(config.CRYPTO_WALLET_EVM, "ETH")

    if config.CRYPTO_WALLET_SOL:
        transfers += _fetch_solana(config.CRYPTO_WALLET_SOL)

    for tx in transfers:
        _process_transfer(tx)


def active_networks() -> list[dict]:
    """Return a list of configured networks for the checkout page."""
    nets = []
    if config.CRYPTO_WALLET_TRX:
        nets.append({
            "name":    "Tron (TRC20)",
            "tokens":  "USDT, USDC",
            "wallet":  config.CRYPTO_WALLET_TRX,
            "fees":    "~0.1 USDT",
            "chain_id": "trx",
        })
    if config.CRYPTO_WALLET_EVM:
        nets.append({
            "name":    "BASE",
            "tokens":  "USDC, USDT",
            "wallet":  config.CRYPTO_WALLET_EVM,
            "fees":    "~0.01 USDT",
            "chain_id": "base",
        })
        if config.ETHERSCAN_API_KEY:
            nets.append({
                "name":    "Ethereum (ERC20)",
                "tokens":  "USDT, USDC",
                "wallet":  config.CRYPTO_WALLET_EVM,
                "fees":    "~5–15 USDT",
                "chain_id": "eth",
            })
    if config.CRYPTO_WALLET_SOL and config.HELIUS_API_KEY:
        nets.append({
            "name":    "Solana",
            "tokens":  "USDT, USDC",
            "wallet":  config.CRYPTO_WALLET_SOL,
            "fees":    "~0.01 USDT",
            "chain_id": "sol",
        })
    return nets


_TRC20_INTERVAL = 120   # TronGrid free tier: max ~1 req/2s → poll every 2 min
_EVM_SOL_INTERVAL = 60  # Basescan/Etherscan/Helius: 60s is fine

def watcher_loop(stop_event) -> None:
    nets = active_networks()
    if not nets:
        log.info("No crypto wallet addresses configured — watcher disabled.")
        return
    names = ", ".join(n["name"] for n in nets)
    log.info(f"Crypto watcher started — monitoring: {names}")
    _last_trc20 = 0.0
    import time as _time
    while not stop_event.is_set():
        try:
            now = _time.time()
            # Only fetch TRC20 every 2 minutes to stay within free rate limit
            skip_trc20 = (now - _last_trc20) < _TRC20_INTERVAL
            check_payments(skip_trc20=skip_trc20)
            if not skip_trc20:
                _last_trc20 = now
        except Exception as exc:
            log.error(f"Unexpected watcher error: {exc}", exc_info=True)
        stop_event.wait(_EVM_SOL_INTERVAL)
