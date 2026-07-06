import requests
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import config

mongo_client = AsyncIOMotorClient(config.MONGO_URI)
db = mongo_client[config.DB_NAME]
stats_collection = db.stats


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


async def get_wallets():
    try:
        resp = requests.get(f"https://apirone.com/api/v2/accounts/{config.APIRONE_ACCOUNT}/wallets")
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}
