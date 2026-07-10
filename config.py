import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
APIRONE_ACCOUNT = os.getenv("APIRONE_ACCOUNT", "")
APIRONE_TRANSFER_KEY = os.getenv("APIRONE_TRANSFER_KEY", "")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DB_NAME", "gatodicer")

ADMIN_USER_ID = 1200925985999171706

AUTO_POST_CHANNEL_ID = 1524104505384501331
AUTO_POST_CHANNEL_NAME = "lf-players"
AUTO_POST_INTERVAL = 300

# Channel scanned for the latest Game #<number> to increment after each game
GAME_LOG_CHANNEL_ID = 1258789286388568134
VOUCH_CHANNEL_ID = 1258789148702146700

# Staff roles allowed to be recorded as the wager funds recipient
FUNDS_RECIPIENT_ROLE_IDS = [
    1258727325265297408
]

ROLL_HYPE_MESSAGES = [
    "gg",
    "LOCK IN",
    "I sentence you to death",
    "GET OVER HERE",
    "womp womp",
    "HUX IS MY DADDY",
    "6&6",
    "get cooked kid",
    "bout to cry?",
    "GAME OVER",
    "lol",
]

# Channels where ticket scanning / form start is ignored (e.g. general chat, auto-post)
CHANNEL_BLACKLIST = [
    AUTO_POST_CHANNEL_ID,
    1258789148702146700,
]

AUTO_POST_MESSAGE = """**[<:Dices:1259259866254676049>] __I Win ALL 7's__ | FT3 → `2x Bet` / FT5 → `3x Bet`
[<:Dices:1259259866254676049>] __I Win ALL 7's & Ties__ | FT3 → `3x Bet` / FT5 → `3.5x Bet`
[<:Dices:1259259866254676049>] __I Win Ties__ | FT3 → `10% MORE BET` / FT5 → `25% MORE Bet`
[<:Dices:1259259866254676049>] __Fair__ | FT3/FT5 → `15%-10% LESS Bet`**
> 🤖 **Make a Ticket - I'M AUTOMATED (MIN: __$1__ | MAX: __$200__)** <:BTC:1450767429465800878><:eth:1289062489363058708><:ltc:1259292428175806504>
"""
# [<:Coin:1259259605255720980>] FT3/FT5 → `15% LESS Bet` | __Fair__**

FORM_QUESTIONS = [
    {
        "type": "choice",
        "text": """🎲 **Which game would you like to play?**
1. `Dice`

-# @mention
""",
        "mapping": {
            "dice": ["1", "dice", "dices", ":game_die:", ":dices:", "d", "roll"], 
            # "coinflip": ["2", "coinflip", "cf", ":coin:", "coin", "flip", "c"]
        },
        "short_key": "game"
    },
    {
        "type": "choice",
        "text": """<:Dices:1259259866254676049> **Which gamemode would you like to play?**
1. `I Win ALL 7's — FT3 → 2x | FT5 → 3x Bet`
2. `I Win ALL 7's & Ties — FT3 → 3x | FT5 → 3.5x Bet`
3. `I Win Ties — FT3 → 10% | FT5 → 25% MORE Bet`
4. `Fair — 15%-10% LESS Bet`

-# @mention
""",
        "mapping": {
            "7s": ["1"],
            "7s_ties": ["2"],
            "ties": ["3"],
            "fair": ["4"]
        },
        "only_for": ["dice"],
        "short_key": "gamemode"
    },
    {
        "type": "choice",
        "text": """🔢 **First to how many?**
1. `FT3`
2. `FT5`
3. `Random`

-# @mention
""",
        "mapping": {
            "ft3": ["1"],
            "ft5": ["2"],
            "random": ["3"]
        },
        "short_key": "first_to"
    },
    {
        "type": "open",
        "text": '💸 **How much would you like to bet in USD + coin?**\n\n**Example:** "5 eth", "10 litecoin" (MIN: __$5__ | MAX: __${max_bet}__)\n\n-# @mention',
        "short_key": "bet",
        "validator": "bet_validator"
    },
    {
        "type": "listen_address",
        "text": "send {coin} addy, my {my_bet}v{his_bet}"
    },
    {
        "type": "choice",
        "text": """👤 **Who goes first?**

1. @gatodicer
2. @mention
3. `Random`

-# @mention""",
        "mapping": {
            "@gatodicer 1": ["1", "you", "@gatodicer"],
            "@mention 1": ["2", "me", "@mention"],
            "random": ["3", "random", "r"]
        },
        "only_for": ["dice"],
        "short_key": "first"
    },
    {
        "type": "choice",
        "text": """🎮 **Which mode would you like to play?**

1. `Normal Mode`
2. `Crazy Mode`
3. `Random`

-# @mention""",
        "mapping": {
            "normal": ["1", "normal", "normal mode", "n"],
            "crazy": ["2", "crazy", "crazy mode", "c"],
            "random": ["3", "random", "r"]
        },
        "only_for": ["dice"],
        "short_key": "mode"
    },
    {
        "type": "choice",
        "text": """<:Coin:1259259605255720980> **Which side would you like to be?**

1. `Heads`
2. `Tails`
3. `Random`

-# @mention""",
        "mapping": {
            "tails": ["1", "heads" "h"],
            "heads": ["2", "tails", "t"],
            "random": ["3", "random", "r"]
        },
        "only_for": ["coinflip"],
        "short_key": "side"
    },
    {
        "type": "listen_confirm",
        "text": ""
    }
]