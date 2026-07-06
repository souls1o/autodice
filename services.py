import requests
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import config
from bets import UNITS, get_price

mongo_client = AsyncIOMotorClient(config.MONGO_URI)
db = mongo_client[config.DB_NAME]
stats_collection = db.stats

HOUSE_COINS = ("btc", "eth", "ltc")


async def get_stats():
    stats = await stats_collection.find_one({"_id": "global"})
    if not stats:
        stats = {
            "_id": "global", "daily": {}, "weekly": {}, "monthly": {},
            "all_time": {}, "most_played_game": "", "unique_users": [],
        }
        await stats_collection.insert_one(stats)
    return stats


async def update_stats(data):
    await stats_collection.update_one({"_id": "global"}, {"$set": data}, upsert=True)


async def track_stats(form, amount, game, result="win"):
    user_id = str(form["ticket_user_id"])
    now = datetime.utcnow()
    stats = await get_stats()
    today = (now - timedelta(hours=8)).strftime("%Y-%m-%d")
    if today not in stats["daily"]:
        stats["daily"][today] = {"wagered": 0, "profit": 0, "games": 0}
    stats["daily"][today]["wagered"] += amount
    stats["daily"][today]["games"] += 1
    if result == "win":
        stats["daily"][today]["profit"] += amount * 0.1
    stats["weekly"]["wagered"] = stats.get("weekly", {}).get("wagered", 0) + amount
    stats["monthly"]["wagered"] = stats.get("monthly", {}).get("wagered", 0) + amount
    stats["all_time"]["wagered"] = stats.get("all_time", {}).get("wagered", 0) + amount
    stats["most_played_game"][game] = stats["most_played_game"].get(game, 0) + 1
    if user_id not in stats["unique_users"]:
        stats["unique_users"].append(user_id)
    await update_stats(stats)


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
