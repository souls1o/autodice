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
UNITS = {"btc": 100_000_000, "eth": 10**18, "ltc": 100_000_000}

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


def normalize_coin(coin_str):
    return COIN_MAP.get(coin_str.lower(), coin_str.lower())


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
    if game == "coinflip":
        return 200
    gamemode = responses.get("gamemode")
    first_to = responses.get("first_to")
    if gamemode == "7s_ties" and first_to == "ft5":
        return 65
    if (gamemode == "7s" and first_to == "ft5") or (gamemode == "7s_ties" and first_to == "ft3"):
        return 75
    if gamemode == "7s" and first_to == "ft3":
        return 200
    if gamemode == "fair":
        return 200
    if gamemode == "ties":
        return 200
    if gamemode == "plus1":
        return 200
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
    Player's rakeback ledger stake (the $60), never the house side (the $180).
    """
    parts = (form.get("responses", {}).get("bet") or "").strip().split()
    from_bet = 0.0
    if parts:
        try:
            from_bet = round(float(parts[0]), 2)
        except (TypeError, ValueError):
            from_bet = 0.0

    try:
        stored = round(float(form.get("rakeback_stake") or 0), 2)
    except (TypeError, ValueError):
        stored = 0.0

    my_bet = calculate_my_bet(form) or 0.0
    candidates = []
    for amount in (stored, from_bet):
        if amount > 0 and abs(amount - my_bet) > 0.001:
            candidates.append(amount)
    if candidates:
        return min(candidates)
    if stored > 0:
        return stored
    return from_bet if from_bet > 0 else 0.0


def calculate_my_bet(form):
    responses = form.get("responses", {})
    try:
        his_bet = float(responses.get("bet", "0").split()[0])
    except (ValueError, IndexError):
        his_bet = 0.0

    game = responses.get("game")
    first_to = responses.get("first_to")
    if game == "coinflip":
        return round(his_bet * 0.85, 2)
    if game != "dice":
        return None

    gamemode = responses.get("gamemode")
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
        edge = float(form.get("fair_edge", 0.10))
        edge = min(max(edge, 0.07), 0.10)
        return round(his_bet * (1.0 - edge), 2)
    return None


def get_bet_info(form):
    parts = form.get("responses", {}).get("bet", "0 ltc").split()
    his_bet_usd = float(parts[0])
    if len(parts) >= 2 and parts[-1].lower() == "rakeback":
        coin = normalize_coin(form.get("winnings_coin") or "ltc")
    else:
        coin = normalize_coin(parts[-1])
    my_bet_usd = calculate_my_bet(form) or 0.0
    return his_bet_usd, my_bet_usd, coin


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


def get_price(coin):
    global _LAST_UPDATE
    coin = coin.lower()
    if coin not in COINGECKO_IDS:
        raise ValueError(f"Unsupported coin: {coin}")

    now = time.time()
    if now - _LAST_UPDATE > CACHE_SECONDS:
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
        _LAST_UPDATE = now

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
    return max(round(float(form.get("winnings_usd", 0)), 2), 0.0)


def bet_validator(response, form=None):
    text = response.strip().lower()
    if text in ("rakeback", "rb"):
        return True
    parts = response.strip().split()
    if len(parts) != 2:
        return False
    try:
        amount = float(parts[0])
    except ValueError:
        return False
    if normalize_coin(parts[1]) not in ("btc", "eth", "ltc"):
        return False
    if not form:
        return 1 <= amount <= 50
    return 1 <= amount <= get_max_bet(form)
