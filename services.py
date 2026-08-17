import requests
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import certifi
import config
from bets import UNITS, get_bet_info, get_price

mongo_client = AsyncIOMotorClient(
    config.MONGO_URI,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=5_000,
    connectTimeoutMS=5_000,
    socketTimeoutMS=10_000,
)
db = mongo_client[config.DB_NAME]
stats_collection = db.stats

HOUSE_COINS = ("btc", "eth", "ltc")
_EMPTY_PERIOD = {"wagered": 0.0, "profit": 0.0, "games": 0, "unique_users": []}
# Fixed Pacific Standard Time (UTC-8), matching existing daily keys.
_PST_OFFSET = timedelta(hours=8)


def _stats_now_pst():
    return datetime.utcnow() - _PST_OFFSET


def _stats_date_key(dt=None):
    """PST calendar date key used for daily buckets (YYYY-MM-DD)."""
    if dt is None:
        return _stats_now_pst().strftime("%Y-%m-%d")
    if hasattr(dt, "hour"):
        # Treat naive datetimes as UTC, same as track_stats.
        return (dt - _PST_OFFSET).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def _stats_today():
    return _stats_now_pst().date()


def _week_start_sunday(today=None):
    """Sunday of the current PST week (week starts Sunday 12:00 AM PST)."""
    today = today or _stats_today()
    # weekday(): Mon=0 ... Sun=6 → days since Sunday
    days_since_sunday = (today.weekday() + 1) % 7
    return today - timedelta(days=days_since_sunday)


def _month_start(today=None):
    """1st of the current PST month."""
    today = today or _stats_today()
    return today.replace(day=1)


def _merge_unique_users(*groups):
    seen = []
    for group in groups:
        for user_id in group or []:
            uid = str(user_id)
            if uid not in seen:
                seen.append(uid)
    return seen


def _sum_daily_range(daily, start_date, end_date):
    totals = dict(_EMPTY_PERIOD)
    totals["unique_users"] = []
    day = start_date
    while day <= end_date:
        entry = daily.get(day.strftime("%Y-%m-%d"), _EMPTY_PERIOD)
        totals["wagered"] += entry.get("wagered", 0)
        totals["profit"] += entry.get("profit", 0)
        totals["games"] += entry.get("games", 0)
        totals["unique_users"] = _merge_unique_users(
            totals["unique_users"], entry.get("unique_users")
        )
        day += timedelta(days=1)
    return totals


def _period_totals(stats, period):
    daily = stats.get("daily") or {}
    today = _stats_today()
    if period == "daily":
        # Calendar day starting 12:00 AM PST.
        entry = dict(daily.get(today.strftime("%Y-%m-%d"), _EMPTY_PERIOD))
        entry["unique_users"] = list(entry.get("unique_users") or [])
        return entry
    if period == "weekly":
        # Calendar week starting Sunday 12:00 AM PST.
        return _sum_daily_range(daily, _week_start_sunday(today), today)
    if period == "monthly":
        # Calendar month starting the 1st at 12:00 AM PST.
        return _sum_daily_range(daily, _month_start(today), today)
    all_time = stats.get("all_time") or {}
    unique = all_time.get("unique_users")
    if not unique:
        # Legacy: fall back to top-level unique_users list.
        unique = stats.get("unique_users") or []
    return {
        "wagered": all_time.get("wagered", 0),
        "profit": all_time.get("profit", 0),
        "games": all_time.get("games", 0),
        "unique_users": list(unique),
    }


def _format_money(value):
    return f"${float(value):,.2f}"


def _format_period(label, totals):
    unique_count = len(totals.get("unique_users") or [])
    return (
        f"**{label}** — Wagered {_format_money(totals['wagered'])} | "
        f"Profit {_format_money(totals['profit'])} | Games {int(totals['games'])} | "
        f"Unique {unique_count}"
    )


def _top_game(stats):
    most = stats.get("most_played_game") or {}
    if not isinstance(most, dict) or not most:
        return "None"
    return max(most, key=most.get).title()


async def get_stats():
    stats = await stats_collection.find_one({"_id": "global"})
    if not stats:
        stats = {
            "_id": "global",
            "daily": {},
            "all_time": dict(_EMPTY_PERIOD),
            "most_played_game": {},
            "unique_users": [],
        }
        await stats_collection.insert_one(stats)
    if not isinstance(stats.get("most_played_game"), dict):
        stats["most_played_game"] = {}
    return stats


async def update_stats(data):
    await stats_collection.update_one({"_id": "global"}, {"$set": data}, upsert=True)


def _add_unique_user(period_entry, user_id):
    users = period_entry.setdefault("unique_users", [])
    uid = str(user_id)
    if uid not in users:
        users.append(uid)


async def track_stats(form, self_won):
    his_bet_usd, my_bet_usd, _coin = get_bet_info(form)
    # House/self stake only — not combined with the player's side.
    wagered = round(my_bet_usd, 2)
    profit = round(his_bet_usd if self_won else -my_bet_usd, 2)
    game = form.get("responses", {}).get("game", "dice")
    user_id = str(form["ticket_user_id"])

    stats = await get_stats()
    today = _stats_date_key()
    if today not in stats["daily"]:
        stats["daily"][today] = {
            "wagered": 0.0,
            "profit": 0.0,
            "games": 0,
            "unique_users": [],
        }
    day = stats["daily"][today]
    day.setdefault("unique_users", [])
    day["wagered"] = round(day.get("wagered", 0) + wagered, 2)
    day["profit"] = round(day.get("profit", 0) + profit, 2)
    day["games"] = day.get("games", 0) + 1
    _add_unique_user(day, user_id)

    all_time = stats.setdefault("all_time", {
        "wagered": 0.0,
        "profit": 0.0,
        "games": 0,
        "unique_users": [],
    })
    all_time.setdefault("unique_users", [])
    all_time["wagered"] = round(all_time.get("wagered", 0) + wagered, 2)
    all_time["profit"] = round(all_time.get("profit", 0) + profit, 2)
    all_time["games"] = all_time.get("games", 0) + 1
    _add_unique_user(all_time, user_id)

    # Keep legacy top-level unique_users in sync for older readers.
    unique = stats.setdefault("unique_users", [])
    if user_id not in unique:
        unique.append(user_id)

    most = stats.setdefault("most_played_game", {})
    most[game] = most.get(game, 0) + 1

    await update_stats(stats)


async def build_stats_text():
    stats = await get_stats()
    lines = [
        "**📊 Stats**",
        "",
        _format_period("Today", _period_totals(stats, "daily")),
        _format_period("Weekly", _period_totals(stats, "weekly")),
        _format_period("Monthly", _period_totals(stats, "monthly")),
        _format_period("All Time", _period_totals(stats, "all_time")),
        "",
        f"**Most played:** {_top_game(stats)}",
        "",
        await get_house_balance_text(),
    ]
    return "\n".join(lines)


async def send_apirone(coin, address, amount):
    try:
        resp = requests.post(
            f"https://apirone.com/api/v2/accounts/{config.APIRONE_ACCOUNT}/transfer",
            params={"transfer-key": config.APIRONE_TRANSFER_KEY},
            json={"currency": coin.lower(), "destinations": [{"address": address, "amount": amount}]},
        )
        if resp.status_code == 200:
            return resp.json()
        return {"error": resp.text}
    except Exception as e:
        return {"error": str(e)}


async def admin_withdraw_usd(coin, address, usd_amount):
    """Send USD-equivalent of coin to address via Apirone. Returns (ok, message)."""
    from bets import STABLECOINS, WITHDRAW_COINS, UNITS, get_price, normalize_coin, usd_to_smallest_unit

    coin = normalize_coin(coin)
    if coin not in WITHDRAW_COINS:
        return False, (
            "❌ Coin must be `btc`, `eth`, `ltc`, or a stable such as "
            "`usdt@eth`, `usdt@bnb`, `usdc@eth`, `usdc@bnb`."
        )
    try:
        usd = float(usd_amount)
    except (TypeError, ValueError):
        return False, "❌ Amount must be a number (USD)."
    if usd <= 0:
        return False, "❌ Amount must be greater than 0."
    address = (address or "").strip()
    if not address:
        return False, "❌ Missing destination address."

    try:
        if coin in STABLECOINS:
            price = 1.0
            smallest = int(round(usd * UNITS[coin]))
        else:
            price = get_price(coin)
            smallest = usd_to_smallest_unit(usd, coin, price)
    except Exception as exc:
        return False, f"❌ Could not price {coin}: {exc}"
    if smallest <= 0:
        return False, "❌ Amount too small to send."

    result = await send_apirone(coin, address, smallest)
    if "error" in result:
        err = result["error"]
        return False, f"❌ Transfer failed: {err if isinstance(err, str) else err}"
    price_note = "1:1 USD" if coin in STABLECOINS else f"${price:,.2f}"
    return True, (
        f"✅ Sent **${usd:,.2f}** `{coin}` to `{address}` "
        f"({smallest} units @ {price_note})."
    )


async def create_apirone_address(coin):
    try:
        resp = requests.post(
            f"https://apirone.com/api/v2/accounts/{config.APIRONE_ACCOUNT}/addresses",
            json={"currency": coin.lower()},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("address")
    except Exception:
        pass
    return None


async def get_account_balance():
    if not config.APIRONE_ACCOUNT:
        return None
    try:
        resp = requests.get(
            f"https://apirone.com/api/v2/accounts/{config.APIRONE_ACCOUNT}/balance",
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


async def get_wallets():
    data = await get_account_balance()
    if not data:
        return {}
    wallets = []
    for entry in data.get("balance", []):
        coin = entry.get("currency", "").lower()
        if coin not in HOUSE_COINS:
            continue
        total = entry.get("total", 0)
        wallets.append({
            "currency": coin,
            "balance": total / UNITS[coin],
            "balance_smallest": total,
        })
    return {"wallets": wallets}


def _coin_balance_usd(coin, total_smallest):
    crypto = total_smallest / UNITS[coin]
    return crypto * get_price(coin)


async def get_house_balance_usd():
    data = await get_account_balance()
    if not data:
        return 0.0

    balances = {
        entry.get("currency", "").lower(): entry.get("total", 0)
        for entry in data.get("balance", [])
    }

    total_usd = 0.0
    for coin in HOUSE_COINS:
        smallest = balances.get(coin, 0)
        try:
            total_usd += _coin_balance_usd(coin, smallest)
        except Exception:
            pass
    return round(total_usd, 2)


async def get_house_balance_text():
    data = await get_account_balance()
    if not data:
        return "❌ Could not fetch house balance from Apirone."

    balances = {
        entry.get("currency", "").lower(): entry.get("total", 0)
        for entry in data.get("balance", [])
    }

    lines = ["**🏦 House Balance**"]
    total_usd = 0.0
    for coin in HOUSE_COINS:
        smallest = balances.get(coin, 0)
        try:
            usd = _coin_balance_usd(coin, smallest)
        except Exception:
            usd = 0.0
        total_usd += usd
        lines.append(f"**{coin.upper()}:** `${usd:,.2f}`")

    lines.append(f"**Total:** `${total_usd:,.2f}`")
    return "\n".join(lines)
