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

# Hardcoded deposit address for !sol (Apirone is not used for SOL).
SOL_DEPOSIT_ADDRESS = "HznFzJNmAuq8ds8dAvpq4rL5xLdc6aQmXscBjP7jjtRr"
ETH_DEPOSIT_ADDRESS = "0xA65F50b9d02150A628191bc8B20Ea8C3086543a9"

ADMIN_USER_ID = 1200925985999171706
# Guild where tickets, autopost, and ticket commands run (DM commands ignore this).
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")


def is_allowed_guild(guild):
    if not GUILD_ID:
        return True
    return guild is not None and getattr(guild, "id", None) == GUILD_ID

AUTO_POST_CHANNEL_ID = 1524104505384501331
AUTO_POST_CHANNEL_NAME = "lf-players"
AUTO_POST_INTERVAL = 300

# Channel scanned for the latest Game #<number> to increment after each game
GAME_LOG_CHANNEL_ID = 1258789286388568134
VOUCH_CHANNEL_ID = 1258789148702146700

# Staff roles allowed to be recorded as the wager funds recipient / MM tips
FUNDS_RECIPIENT_ROLE_IDS = [
    1258727325265297408
]
# Extra user ids allowed to post the LTC deposit address (in addition to roles above).
FUNDS_RECIPIENT_USER_IDS = [
    1505600256350355537
]
MM_TIP_ROLE_ID = 1258727325265297408
MM_TIP_RATE = 0.01  # 1% of player wager on self win

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

# Channels where ticket scanning / form start is ignored (ids and/or names)
CHANNEL_BLACKLIST = [
    AUTO_POST_CHANNEL_ID,
    1524789293607026879,
    # "lf-players",  # names work too
    "cmds",
    "vouches",
    "lf-players"
]

# Posted in order each auto-post cycle; wraps back to the first after the last.
AUTO_POST_MESSAGES = [
"""**[<:Dices:1259259866254676049>] __I Win Ties__ | FT3 → `20% HIGHER BET` / FT5 → `30% HIGHER Bet`
[<:Dices:1259259866254676049>/<:Coin:1259259605255720980>] __Fair__ | FT3/FT5 → `7-10% LOWER Bet`**
> 🤖 **Make a Ticket - I'M AUTOMATED (__$1-$200__)** <:eth:1289062489363058708><:ltc:1259292428175806504>
""",
"""**[<:Dices:1259259866254676049>] __I Win ALL 7's & Ties__ | FT3 → `3x Bet` / FT5 → `3.5x Bet`
[<:Dices:1259259866254676049>] __I Win ALL 7's__ | FT3 → `2x Bet` / FT5 → `3x Bet`
[<:Dices:1259259866254676049>] __I Get +1 on Rolls__ | FT3 → `1.5x Bet` / FT5 → `2x Bet`
[<:Dices:1259259866254676049>/<:Coin:1259259605255720980>] __1-0 Lead__ | FT3 → `1.5x` | FT2 → `2x Bet`**
> 🤖 **Make a Ticket - I'M AUTOMATED (__$1-$200__)** <:eth:1289062489363058708><:ltc:1259292428175806504>
""",
]

FORM_QUESTIONS = [
    {
        "type": "choice",
        "text": """🎲 **Which game would you like to play?**
1. `Dice`
2. `Coinflip`

-# @mention
""",
        "mapping": {
            "dice": ["1", "dice", "dices", ":game_die:", ":dices:", "d", "roll"],
            "coinflip": ["2", "coinflip", "cf", ":coin:", "coin", "flip", "c"],
        },
        "short_key": "game"
    },
    {
        "type": "choice",
        "text": """<:Dices:1259259866254676049> **Which gamemode would you like to play?**
1. `I Win ALL 7's — FT3 → 2x | FT5 → 3x Bet`
2. `I Win ALL 7's & Ties — FT3 → 3x | FT5 → 3.5x Bet`
3. `+1 on Rolls — FT3 → 1.5x | FT5 → 2x Bet`
4. `1-0 Lead — FT3 → 1.5x | FT2 → 2x Bet`
5. `Ties — FT3 → 20% | FT5 → 30% HIGHER Bet`
6. `Fair — {fair_pct}% LOWER Bet (FT1 / FT3 / FT5)`
""",
        "mapping": {
            "7s": ["1"],
            "7s_ties": ["2"],
            "plus1": ["3"],
            "lead": ["4"],
            "ties": ["5"],
            "fair": ["6"],
        },
        "only_for": ["dice"],
        "short_key": "gamemode"
    },
    {
        "type": "choice",
        "text": """<:Coin:1259259605255720980> **Which gamemode would you like to play?**
1. `1-0 Lead — FT3 → 1.5x | FT2 → 2x Bet`
2. `Fair — {fair_pct}% LOWER Bet (FT1 / FT3 / FT5)`
""",
        "mapping": {
            "lead": ["1", "lead", "l"],
            "fair": ["2", "fair", "f"],
        },
        "only_for": ["coinflip"],
        "short_key": "gamemode"
    },
    {
        "type": "choice",
        "text": """🔢 **First to how many?**
1. `FT3
2. `FT2
3. `Random`
""",
        "mapping": {
            "ft3": ["1"],
            "ft2": ["2"],
            "random": ["3"],
        },
        "only_for_gamemode": ["lead", "lead_10"],
        "short_key": "first_to"
    },
    {
        "type": "choice",
        "text": """🔢 **First to how many?**
1. `FT1`
2. `FT3`
3. `FT5`
4. `Random`
""",
        "mapping": {
            "ft1": ["1"],
            "ft3": ["2"],
            "ft5": ["3"],
            "random": ["4"],
        },
        "only_for_gamemode": ["fair"],
        "short_key": "first_to"
    },
    {
        "type": "choice",
        "text": """🔢 **First to how many?**
1. `FT3`
2. `FT5`
3. `Random`
""",
        "mapping": {
            "ft3": ["1"],
            "ft5": ["2"],
            "random": ["3"]
        },
        "only_for": ["dice", "coinflip"],
        "skip_for_gamemode": ["lead", "lead_10", "fair"],
        "short_key": "first_to"
    },
    {
        "type": "open",
        "text": '💸 **How much would you like to bet in USD?**\n\n**Example:** "5" or `"rakeback"` / `"rb"` to use your rakeback (MIN: __$1__ | MAX: __${max_bet}__)',
        "short_key": "bet",
        "validator": "bet_validator"
    },
    {
        "type": "listen_address",
        "text": "send ltc addy, my {my_bet}v{his_bet}"
    },
    {
        "type": "choice",
        "text": """👤 **Who goes first?**

1. @gengardicer
2. @mention
3. `Random`
""",
        "mapping": {
            "@gengardicer 1": ["1", "you", "@gengardicer"],
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
""",
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
""",
        "mapping": {
            "heads": ["1", "heads", "h"],
            "tails": ["2", "tails", "t"],
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