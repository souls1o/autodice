import asyncio
import re
import time
import requests
import config

COIN_MAP = {
    "bitcoin": "btc", "btc": "btc",
    "ethereum": "eth", "eth": "eth",
    "litecoin": "ltc", "ltc": "ltc",
}

COINGECKO_IDS = {"btc": "bitcoin", "eth": "ethereum", "ltc": "litecoin"}
UNITS = {
    "btc": 100_000_000,
    "eth": 10**18,
    "ltc": 100_000_000,
    # Apirone stablecoin smallest units (1 USD = 1 token).
    "usdt@eth": 10**6,
    "usdc@eth": 10**6,
    "usdt@bnb": 10**18,
    "usdc@bnb": 10**18,
    "usdt@trx": 10**6,
    "usdc@trx": 10**6,
    "usdt@ton": 10**6,
}

STABLECOINS = {
    "usdt@eth", "usdc@eth",
    "usdt@bnb", "usdc@bnb",
    "usdt@trx", "usdc@trx",
    "usdt@ton",
}

WITHDRAW_COINS = {"btc", "eth", "ltc"} | STABLECOINS

_STABLE_ALIASES = {
    "usdteth": "usdt@eth",
    "usdt-eth": "usdt@eth",
    "usdcbnb": "usdc@bnb",
    "usdc-bnb": "usdc@bnb",
    "usdceth": "usdc@eth",
    "usdc-eth": "usdc@eth",
    "usdtbnb": "usdt@bnb",
    "usdt-bnb": "usdt@bnb",
    "usdttrx": "usdt@trx",
    "usdt-trx": "usdt@trx",
    "usdctrx": "usdc@trx",
    "usdc-trx": "usdc@trx",
    "usdtton": "usdt@ton",
    "usdt-ton": "usdt@ton",
}

_BECH32_CHARS = r"qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_ADDRESS_PATTERNS = {
    "btc": (
        re.compile(rf"(bc1[{_BECH32_CHARS}]{{25,87}})", re.IGNORECASE),
        re.compile(r"([13][1-9A-HJ-NP-Za-km-z]{25,34})"),
    ),
    "eth": (
        re.compile(r"(0x[a-fA-F0-9]{40})"),
    ),
    "ltc": (
        re.compile(rf"(ltc1[{_BECH32_CHARS}]{{25,87}})", re.IGNORECASE),
        re.compile(r"([LM3][1-9A-HJ-NP-Za-km-z]{26,33})"),
    ),
}

_PRICE_CACHE = {}
_LAST_UPDATE = 0
CACHE_SECONDS = 180
_PRICE_REFRESH_LOCK = asyncio.Lock()
_PRICE_REFRESH_TASK = None


def normalize_coin(coin_str):
    raw = (coin_str or "").strip().lower()
    if raw in _STABLE_ALIASES:
        return _STABLE_ALIASES[raw]
    if "@" in raw:
        return raw
    return COIN_MAP.get(raw, raw)


def extract_crypto_address(text, coin):
    coin = normalize_coin(coin)
    for pattern in _ADDRESS_PATTERNS.get(coin, ()):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def get_max_bet(form):
    responses = form.get("responses", {})
    game = responses.get("game")
    gamemode = responses.get("gamemode")
    if gamemode == "lead_10":
        gamemode = "lead"
    first_to = responses.get("first_to")
    if gamemode == "7s_ties":
        return 75
    if gamemode == "7s":
        return 150
    if gamemode in ("fair", "ties", "plus1", "lead"):
        return 300
    return 50


def format_bet_display(value):
    num = round(float(value), 2)
    if num == int(num):
        return str(int(num))
    return f"{num:.2f}"


def is_rakeback_bet(form):
    if form.get("rakeback_bet"):
        return True
    parts = (form.get("responses", {}).get("bet") or "").strip().split()
    return len(parts) >= 2 and parts[-1].lower() == "rakeback"


def player_rakeback_stake_usd(form):
    """
    Player's rakeback ledger stake (e.g. $60), never house my_bet (e.g. $180).
    Uses the amount locked in at bet time (`rakeback_stake` / `X rakeback`).
    """
    try:
        stored = round(float(form.get("rakeback_stake") or 0), 2)
    except (TypeError, ValueError):
        stored = 0.0
    if stored > 0:
        return stored

    parts = (form.get("responses", {}).get("bet") or "").strip().split()
    if len(parts) >= 2 and parts[-1].lower() == "rakeback":
        try:
            return round(float(parts[0]), 2)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _fair_house_bet(his_bet, form):
    # Prefer edge frozen for this match so mid-game level-ups cannot resize the stake.
    edge = form.get("match_fair_edge")
    if edge is None:
        edge = form.get("fair_edge", 0.10)
    edge = float(edge)
    edge = min(max(edge, 0.07), 0.10)
    return round(his_bet * (1.0 - edge), 2)


def _lead_house_bet(his_bet, first_to):
    if first_to == "ft2":
        return round(his_bet * 2, 2)
    return round(his_bet * 1.5, 2)


def calculate_my_bet(form):
    responses = form.get("responses", {})
    if is_rakeback_bet(form):
        his_bet = player_rakeback_stake_usd(form)
    else:
        try:
            his_bet = float(str(responses.get("bet", "0")).split()[0])
        except (ValueError, IndexError, TypeError):
            his_bet = 0.0

    game = responses.get("game")
    first_to = responses.get("first_to")
    gamemode = responses.get("gamemode")
    if gamemode == "lead_10":
        gamemode = "lead"
        first_to = first_to or "ft2"

    if gamemode == "lead":
        return _lead_house_bet(his_bet, first_to or "ft3")

    if game == "coinflip":
        if gamemode == "fair":
            return _fair_house_bet(his_bet, form)
        return _lead_house_bet(his_bet, first_to or "ft2")

    if game != "dice":
        return None

    if gamemode == "7s" and first_to == "ft3":
        return round(his_bet * 2, 2)
    if (gamemode == "7s" and first_to == "ft5") or (gamemode == "7s_ties" and first_to == "ft3"):
        return round(his_bet * 3, 2)
    if gamemode == "7s_ties" and first_to == "ft5":
        return round(his_bet * 3.5, 2)
    if gamemode == "ties" and first_to == "ft3":
        return round(his_bet * 1.2, 2)
    if gamemode == "ties" and first_to == "ft5":
        return round(his_bet * 1.3, 2)
    if gamemode == "plus1" and first_to == "ft3":
        return round(his_bet * 1.5, 2)
    if gamemode == "plus1" and first_to == "ft5":
        return round(his_bet * 2, 2)
    if gamemode == "fair":
        return _fair_house_bet(his_bet, form)
    return None


def get_bet_info(form):
    """Return (player_bet_usd, house_bet_usd, coin). Safe for rakeback bets."""
    if is_rakeback_bet(form):
        his_bet_usd = player_rakeback_stake_usd(form)
        my_bet_usd = calculate_my_bet(form) or 0.0
        return round(float(his_bet_usd or 0), 2), round(float(my_bet_usd or 0), 2), "ltc"

    raw = (form.get("responses", {}).get("bet") or "0 ltc").strip()
    parts = raw.split()
    try:
        his_bet_usd = float(parts[0]) if parts else 0.0
    except (TypeError, ValueError):
        his_bet_usd = 0.0
    my_bet_usd = calculate_my_bet(form) or 0.0
    return round(his_bet_usd, 2), round(float(my_bet_usd or 0), 2), "ltc"


def get_match_bets(form):
    """
    Stakes for the active/just-finished match.
    Prefers amounts frozen at confirm/funding so mid-match level/perk changes cannot
    rewrite fair house edge (auto-log AND hold settlement).
    Returns (his_bet_usd, my_bet_usd, coin, rakeback).
    """
    settled = (form or {}).get("settled_bets") or {}
    if settled and (
        settled.get("my_bet_usd") is not None or settled.get("his_bet_usd") is not None
    ):
        return (
            round(float(settled.get("his_bet_usd") or 0), 2),
            round(float(settled.get("my_bet_usd") or 0), 2),
            "ltc",
            bool(settled.get("rakeback")),
        )
    his_bet_usd, my_bet_usd, coin = get_bet_info(form)
    return (
        round(float(his_bet_usd or 0), 2),
        round(float(my_bet_usd or 0), 2),
        coin or "ltc",
        is_rakeback_bet(form),
    )


def freeze_match_fair_edge(form):
    """Lock the fair edge used for this match's house stake (idempotent)."""
    if form is None:
        return None
    if form.get("match_fair_edge") is not None:
        return float(form["match_fair_edge"])
    edge = float(form.get("fair_edge", 0.10) or 0.10)
    form["match_fair_edge"] = edge
    return edge


def clear_match_stake_freeze(form):
    if not form:
        return
    form.pop("settled_bets", None)
    form.pop("match_fair_edge", None)
    form.pop("hold_stake_deducted", None)


def get_match_his_display(form):
    settled = (form or {}).get("settled_bets") or {}
    if settled:
        return round(float(settled.get("his_display_usd", settled.get("his_bet_usd") or 0)), 2)
    return display_his_bet_usd(form)


def normalize_bet_response(response):
    """Store player bets as '<amount> ltc' (LTC-only wagers)."""
    text = (response or "").strip()
    if text.lower() in ("rakeback", "rb"):
        return text.lower()
    try:
        amount = float(text.split()[0] if text.split() else text)
    except ValueError:
        return text
    return f"{format_bet_display(amount)} ltc"


def get_self_hold_usd(form_or_session):
    data = form_or_session or {}
    if "self_hold_usd" in data:
        val = data["self_hold_usd"]
    else:
        val = data.get("winnings_usd", 0)
    return max(round(float(val or 0), 2), 0.0)


def get_player_hold_usd(form_or_session):
    data = form_or_session or {}
    return max(round(float(data.get("player_hold_usd", 0) or 0), 2), 0.0)


def sync_legacy_winnings(form):
    """Keep legacy winnings_usd aligned with self hold for funding paths."""
    form["self_hold_usd"] = round(float(form.get("self_hold_usd", form.get("winnings_usd", 0) or 0)), 8)
    form["player_hold_usd"] = round(float(form.get("player_hold_usd", 0) or 0), 8)
    form["winnings_usd"] = form["self_hold_usd"]
    form.setdefault("winnings_coin", "ltc")


def add_self_hold_usd(form, usd):
    sync_legacy_winnings(form)
    try:
        add = round(float(usd or 0), 8)
    except (TypeError, ValueError):
        return 0.0
    if add <= 0:
        return 0.0
    form["self_hold_usd"] = round(float(form.get("self_hold_usd") or 0) + add, 8)
    form["winnings_usd"] = form["self_hold_usd"]
    try:
        form["winnings_crypto"] = round(
            float(form.get("winnings_crypto") or 0) + usd_to_crypto_amount(add, "ltc"), 8
        )
    except Exception:
        pass
    return add


def add_player_hold_usd(form, usd):
    sync_legacy_winnings(form)
    form["player_hold_usd"] = round(form["player_hold_usd"] + float(usd), 8)


def subtract_player_hold_usd(form, usd):
    sync_legacy_winnings(form)
    deduct = min(float(usd), form["player_hold_usd"])
    if deduct <= 0:
        return 0.0
    form["player_hold_usd"] = round(form["player_hold_usd"] - deduct, 8)
    return deduct


def apply_player_hold_stake(form):
    """
    At game start, stake the player's wager from player hold when available.
    Returns the USD amount deducted. Skipped for rakeback bets.
    """
    if is_rakeback_bet(form):
        form["player_stake_from_hold"] = 0.0
        return 0.0
    his_bet_usd, _my_bet, _coin = get_bet_info(form)
    available = get_player_hold_usd(form)
    deduct = round(min(available, float(his_bet_usd or 0)), 2)
    if deduct > 0:
        deduct = subtract_player_hold_usd(form, deduct)
    form["player_stake_from_hold"] = deduct
    return deduct


def subtract_self_hold_usd(form, usd):
    sync_legacy_winnings(form)
    deduct = min(float(usd), form["self_hold_usd"])
    if deduct <= 0:
        return 0.0
    form["self_hold_usd"] = round(form["self_hold_usd"] - deduct, 8)
    form["winnings_usd"] = form["self_hold_usd"]
    try:
        form["winnings_crypto"] = round(
            max(form.get("winnings_crypto", 0) - usd_to_crypto_amount(deduct, "ltc"), 0), 8
        )
    except Exception:
        pass
    return deduct


def deduct_self_hold_on_channel(channel_id, usd_amount):
    """Subtract USD from ticket self hold (form or session). Returns amount deducted."""
    from state import active_forms, get_ticket_session, save_session_from_form

    usd = round(float(usd_amount or 0), 2)
    if usd <= 0:
        return 0.0
    form = active_forms.get(channel_id)
    if form:
        deducted = subtract_self_hold_usd(form, usd)
        sync_winnings_crypto(form)
        save_session_from_form(channel_id, form)
        return round(float(deducted), 2)
    session = get_ticket_session(channel_id)
    available = get_self_hold_usd(session)
    deducted = round(min(available, usd), 2)
    if deducted <= 0:
        return 0.0
    session["self_hold_usd"] = round(available - deducted, 2)
    session["winnings_usd"] = session["self_hold_usd"]
    return deducted


def clear_self_hold(form):
    sync_legacy_winnings(form)
    form["self_hold_usd"] = 0.0
    form["winnings_usd"] = 0.0
    form["winnings_crypto"] = 0.0


def clear_player_hold(form):
    sync_legacy_winnings(form)
    form["player_hold_usd"] = 0.0


def display_his_bet_usd(form):
    """Player side shown in XvY — 0 when staking rakeback (no crypto wager)."""
    if is_rakeback_bet(form):
        return 0.0
    return get_bet_info(form)[0]


def format_matchup(form):
    """House bet vs displayed player bet, e.g. `9v0` for a rakeback stake."""
    _his, my_bet_usd, _coin = get_bet_info(form)
    return (
        f"{format_bet_display(my_bet_usd)}v"
        f"{format_bet_display(display_his_bet_usd(form))}"
    )


def _refresh_prices_sync():
    global _LAST_UPDATE
    ids = ",".join(COINGECKO_IDS.values())
    r = requests.get(
        f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd",
        headers={"accept": "application/json", "x-cg-demo-api-key": config.COINGECKO_API_KEY},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    for symbol, coin_id in COINGECKO_IDS.items():
        _PRICE_CACHE[symbol] = float(data[coin_id]["usd"])
    _LAST_UPDATE = time.time()


def _prices_fresh():
    return bool(_PRICE_CACHE) and (time.time() - _LAST_UPDATE) <= CACHE_SECONDS


def _schedule_price_refresh():
    """Refresh CoinGecko off the event loop; never block Discord handling."""
    global _PRICE_REFRESH_TASK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _refresh_prices_sync()
        return
    task = _PRICE_REFRESH_TASK
    if task is not None and not task.done():
        return

    async def _run():
        async with _PRICE_REFRESH_LOCK:
            if _prices_fresh():
                return
            await asyncio.to_thread(_refresh_prices_sync)

    _PRICE_REFRESH_TASK = loop.create_task(_run())


def get_price(coin):
    coin = coin.lower()
    if coin not in COINGECKO_IDS:
        raise ValueError(f"Unsupported coin: {coin}")

    if coin in _PRICE_CACHE:
        if not _prices_fresh():
            _schedule_price_refresh()
        return _PRICE_CACHE[coin]

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        # Never block the Discord loop on a first-time fetch.
        _schedule_price_refresh()
        raise RuntimeError("price cache not ready")

    _refresh_prices_sync()
    return _PRICE_CACHE[coin]


async def get_price_async(coin):
    coin = coin.lower()
    if coin not in COINGECKO_IDS:
        raise ValueError(f"Unsupported coin: {coin}")
    if _prices_fresh() and coin in _PRICE_CACHE:
        return _PRICE_CACHE[coin]
    async with _PRICE_REFRESH_LOCK:
        if not _prices_fresh() or coin not in _PRICE_CACHE:
            await asyncio.to_thread(_refresh_prices_sync)
    return _PRICE_CACHE[coin]


def usd_to_crypto_amount(usd, coin):
    return usd / get_price(coin)


def usd_to_smallest_unit(usd, coin, price_usd):
    return int((usd / price_usd) * UNITS[coin])


def get_wager_usd(form):
    return get_bet_info(form)[1]


def add_wagered_usd(form, amount=None):
    if amount is None:
        amount = get_wager_usd(form)
    form["total_wagered_usd"] = round(form.get("total_wagered_usd", 0) + amount, 8)


def add_winnings_usd(form, usd, coin):
    form["winnings_usd"] = round(form.get("winnings_usd", 0) + usd, 8)
    form["winnings_crypto"] = round(form.get("winnings_crypto", 0) + usd_to_crypto_amount(usd, coin), 8)


def subtract_winnings_usd(form, usd, coin):
    deducted = min(float(usd), max(form.get("winnings_usd", 0), 0))
    if deducted <= 0:
        return 0.0
    form["winnings_usd"] = round(form.get("winnings_usd", 0) - deducted, 8)
    form["winnings_crypto"] = round(
        form.get("winnings_crypto", 0) - usd_to_crypto_amount(deducted, coin),
        8,
    )
    return deducted


def sync_winnings_crypto(form):
    coin = form.get("winnings_coin", "ltc")
    usd = max(form.get("winnings_usd", 0), 0)
    try:
        form["winnings_crypto"] = round(usd_to_crypto_amount(usd, coin), 8)
    except Exception:
        pass


def get_ticket_hold_usd(form):
    return get_self_hold_usd(form)


def bet_validator(response, form=None):
    text = response.strip().lower()
    if text in ("rakeback", "rb"):
        return True
    try:
        amount = float(text.split()[0] if text.split() else text)
    except ValueError:
        return False
    if not form:
        return 5 <= amount <= 50
    return 5 <= amount <= get_max_bet(form)
