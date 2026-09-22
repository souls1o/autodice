"""Wallet env — reads from project config / os.environ."""

import os

import config as app_config


def mnemonic():
    return (getattr(app_config, "HOUSE_MNEMONIC", None) or os.getenv("HOUSE_MNEMONIC") or "").strip()


def eth_rpc_url():
    return (getattr(app_config, "ETH_RPC_URL", None) or os.getenv("ETH_RPC_URL") or "").strip()


def sol_rpc_url():
    return (getattr(app_config, "SOL_RPC_URL", None) or os.getenv("SOL_RPC_URL") or "").strip()


def bnb_rpc_url():
    return (getattr(app_config, "BNB_RPC_URL", None) or os.getenv("BNB_RPC_URL") or "").strip()


def ltc_api_url():
    """Base URL for Litecoin explorer/API (BlockCypher-compatible or electrs)."""
    return (getattr(app_config, "LTC_API_URL", None) or os.getenv("LTC_API_URL") or "").strip().rstrip("/")


def ltc_api_token():
    return (getattr(app_config, "LTC_API_TOKEN", None) or os.getenv("LTC_API_TOKEN") or "").strip()


def require_mnemonic():
    m = mnemonic()
    if not m:
        raise RuntimeError("HOUSE_MNEMONIC is not configured")
    return m
