"""Wallet env — reads from project config / os.environ."""

import os

import config as app_config


def mnemonic():
    return (getattr(app_config, "HOUSE_MNEMONIC", None) or os.getenv("HOUSE_MNEMONIC") or "").strip()


def eth_rpc_url():
    return (
        getattr(app_config, "ETH_RPC_URL", None)
        or os.getenv("ETH_RPC_URL")
        or "https://ethereum.publicnode.com"
    ).strip()


def sol_rpc_url():
    return (
        getattr(app_config, "SOL_RPC_URL", None)
        or os.getenv("SOL_RPC_URL")
        or "https://api.mainnet-beta.solana.com"
    ).strip()


def bnb_rpc_url():
    return (
        getattr(app_config, "BNB_RPC_URL", None)
        or os.getenv("BNB_RPC_URL")
        or "https://bsc-dataseed.binance.org"
    ).strip()


def ltc_rpc_url():
    """Esplora-compatible Litecoin HTTP base (no API key)."""
    return (
        getattr(app_config, "LTC_RPC_URL", None)
        or os.getenv("LTC_RPC_URL")
        or getattr(app_config, "LTC_API_URL", None)  # back-compat
        or os.getenv("LTC_API_URL")
        or "https://litecoinspace.org/api"
    ).strip().rstrip("/")


def require_mnemonic():
    m = mnemonic()
    if not m:
        raise RuntimeError("HOUSE_MNEMONIC is not configured")
    return m
