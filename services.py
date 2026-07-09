import requests
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import config
from bets import UNITS, get_bet_info, get_price

mongo_client = AsyncIOMotorClient(config.MONGO_URI)
db = mongo_client[config.DB_NAME]
stats_collection = db.stats

HOUSE_COINS = ("btc", "eth", "ltc")
_EMPTY_PERIOD = {"wagered": 0.0, "profit": 0.0, "games": 0}


def _stats_date_key(dt=None):
    dt = dt or datetime.utcnow()
    return (dt - timedelta(hours=8)).strftime("%Y-%m-%d")


def _stats_today():
    return (datetime.utcnow() - timedelta(hours=8)).date()


def _period_totals(stats, period):
    daily = stats.get("daily") or {}
    if period == "daily":
        return dict(daily.get(_stats_date_key(), _EMPTY_PERIOD))
    if period == "weekly":
        totals = dict(_EMPTY_PERIOD)
        today = _stats_today()
        for i in range(7):
            entry = daily.get((today - timedelta(days=i)).strftime("%Y-%m-%d"), _EMPTY_PERIOD)
            totals["wagered"] += entry.get("wagered", 0)
            totals["profit"] += entry.get("profit", 0)
            totals["games"] += entry.get("games", 0)
        return totals
    if period == "monthly":
        totals = dict(_EMPTY_PERIOD)
        prefix = _stats_today().strftime("%Y-%m")
        for key, entry in daily.items():
            if key.startswith(prefix):
                totals["wagered"] += entry.get("wagered", 0)
                totals["profit"] += entry.get("profit", 0)
                totals["games"] += entry.get("games", 0)
        return totals
    all_time = stats.get("all_time") or {}
    return {
        "wagered": all_time.get("wagered", 0),
        "profit": all_time.get("profit", 0),
        "games": all_time.get("games", 0),
    }


def _format_money(value):
    return f"${float(value):,.2f}"


def _format_period(label, totals):
    return (
        f"**{label}** — Wagered {_format_money(totals['wagered'])} | "
        f"Profit {_format_money(totals['profit'])} | Games {int(totals['games'])}"
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


async def track_stats(form, self_won):
    his_bet_usd, my_bet_usd, _coin = get_bet_info(form)
    wagered = round(his_bet_usd + my_bet_usd, 2)
    profit = round(his_bet_usd if self_won else -my_bet_usd, 2)
    game = form.get("responses", {}).get("game", "dice")
    user_id = str(form["ticket_user_id"])

    stats = await get_stats()
    today = _stats_date_key()
    if today not in stats["daily"]:
        stats["daily"][today] = dict(_EMPTY_PERIOD)
    day = stats["daily"][today]
    day["wagered"] = round(day.get("wagered", 0) + wagered, 2)
    day["profit"] = round(day.get("profit", 0) + profit, 2)
    day["games"] = day.get("games", 0) + 1

    all_time = stats.setdefault("all_time", dict(_EMPTY_PERIOD))
    all_time["wagered"] = round(all_time.get("wagered", 0) + wagered, 2)
    all_time["profit"] = round(all_time.get("profit", 0) + profit, 2)
    all_time["games"] = all_time.get("games", 0) + 1

    most = stats.setdefault("most_played_game", {})
    most[game] = most.get(game, 0) + 1

    unique = stats.setdefault("unique_users", [])
    if user_id not in unique:
        unique.append(user_id)

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
        f"**Unique users:** {len(stats.get('unique_users', []))}",
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
