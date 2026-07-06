import asyncio
import re
from forms import member_has_listen_role
from postgame import end_game

DA_HOOD_BOT_ID = 1200925985999171706
ROLL_EMBED_PATTERN = re.compile(r"(\d+)\s*&\s*(\d+)")


def is_bot_turn(state):
    return state["current_player"] in ("me", "@gatodicer")


def current_player_key(state):
    return "me" if is_bot_turn(state) else "you"


def other_player_key(player):
    return "you" if player == "me" else "me"


async def get_command_before_message(channel, embed_message, predicate):
    async for msg in channel.history(limit=30, before=embed_message):
        if predicate(msg):
            return msg
    return None


async def trigger_bot_roll(channel, form, bot_user):
    state = form["game_state"]
    await asyncio.sleep(1)
    await channel.send("-roll")
    state["waiting_for_embed"] = True
    state["roll_initiator_id"] = bot_user.id


async def handle_user_roll(message, form, bot_user):
    state = form["game_state"]
    state.setdefault("pending_user_embeds", 0)
    state["pending_user_embeds"] += 1
    state["waiting_for_embed"] = True
    state["roll_initiator_id"] = message.author.id


def _pair_winner(me_total, you_total, gamemode, roll_mode):
    if gamemode in ("7s", "7s_ties") and (me_total == 7 or you_total == 7):
        return "me"
    if gamemode in ("ties", "7s_ties") and me_total == you_total:
        return "me"
    if me_total == you_total:
        return None
    if roll_mode == "crazy":
        return "me" if me_total < you_total else "you"
    return "me" if me_total > you_total else "you"


async def _score_pair(channel, form, bot_user, me_total, you_total, *, continue_batch=False):
    state = form["game_state"]
    winner = _pair_winner(me_total, you_total, state["gamemode"], state["mode"])

    if winner == "me":
        state["self_score"] += 1
    elif winner == "you":
        state["adder_score"] += 1

    await channel.send(f"`{state['self_score']}-{state['adder_score']}`")

    first_to = state["first_to"]
    if state["self_score"] >= first_to or state["adder_score"] >= first_to:
        self_won = state["self_score"] >= first_to
        winner_id = bot_user.id if self_won else form["ticket_user_id"]
        await channel.send(f"<@{winner_id}> won!")
        await end_game(channel, form, self_won, bot_user)
        return True

    if continue_batch:
        return False

    state["user_totals_queue"] = []
    state["pending_user_embeds"] = 0
    state["bot_rolls_remaining"] = 0
    state["pending_bot_total"] = None
    state["current_player"] = state["first_player"]
    await do_next_roll(channel, form, bot_user)
    return False


async def do_next_roll(channel, form, bot_user):
    state = form["game_state"]
    if state.get("game_type") != "dice" or state.get("waiting_for_embed"):
        return
    if is_bot_turn(state):
        await trigger_bot_roll(channel, form, bot_user)


def parse_roll_from_embed(message):
    if not message.author.bot or not message.embeds:
        return None
    description = message.embeds[0].description or ""
    match = ROLL_EMBED_PATTERN.search(description)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


async def handle_roll_embed(message, form, bot_user):
    state = form["game_state"]
    if not state.get("waiting_for_embed"):
        return
    if not message.author.bot or not message.embeds:
        return

    cmd = await get_command_before_message(
        message.channel, message, lambda m: m.content.strip().lower() == "-roll"
    )
    if not cmd or cmd.author.id != state.get("roll_initiator_id"):
        return

    rolls = parse_roll_from_embed(message)
    if not rolls:
        return

    total = rolls[0] + rolls[1]
    ticket_user_id = form["ticket_user_id"]
    state.setdefault("user_totals_queue", [])
    state.setdefault("pending_user_embeds", 0)
    state.setdefault("bot_rolls_remaining", 0)

    if cmd.author.id == ticket_user_id:
        state["pending_user_embeds"] -= 1

        if state.get("pending_bot_total") is not None:
            game_over = await _score_pair(
                message.channel, form, bot_user, state["pending_bot_total"], total
            )
            state["pending_bot_total"] = None
            state["waiting_for_embed"] = False
            return

        state["user_totals_queue"].append(total)

        if state["pending_user_embeds"] > 0:
            state["waiting_for_embed"] = True
            state["roll_initiator_id"] = ticket_user_id
            return

        state["waiting_for_embed"] = False
        state["bot_rolls_remaining"] = len(state["user_totals_queue"])
        await trigger_bot_roll(message.channel, form, bot_user)
        return

    # bot embed — pair 1:1 with the next queued user total
    if state["user_totals_queue"]:
        you_total = state["user_totals_queue"].pop(0)
        state["bot_rolls_remaining"] -= 1
        state["waiting_for_embed"] = False
        remaining = state["bot_rolls_remaining"]
        game_over = await _score_pair(
            message.channel, form, bot_user, total, you_total, continue_batch=remaining > 0
        )
        if game_over:
            return
        if remaining > 0:
            await trigger_bot_roll(message.channel, form, bot_user)
        return

    # bot went first this round — hold total until user embed arrives
    state["pending_bot_total"] = total
    state["current_player"] = "you"
    state["waiting_for_embed"] = False


async def handle_coinflip_embed(message, form, bot_user):
    state = form["game_state"]
    if not state.get("waiting_for_embed"):
        return

    cmd = await get_command_before_message(
        message.channel, message, lambda m: "-cf" in (m.content or "").lower()
    )
    if not cmd or not member_has_listen_role(cmd.author):
        return
    if state.get("consumed_cf_message_id") == cmd.id:
        return
    state["consumed_cf_message_id"] = cmd.id

    text = (message.content or "").lower()
    if message.embeds:
        embed = message.embeds[0]
        text += " " + (embed.title or "").lower()
        text += " " + (embed.description or "").lower()
        for field in embed.fields:
            text += " " + (field.name or "").lower()
            text += " " + (field.value or "").lower()

    if re.search(r"\bheads\b", text):
        flip = "heads"
    elif re.search(r"\btails\b", text):
        flip = "tails"
    else:
        return

    player = current_player_key(state)
    state["round_flips"][player] = flip
    state["waiting_for_embed"] = False
    state.pop("consumed_cf_message_id", None)

    other = other_player_key(player)
    if state["round_flips"][other] is None:
        state["current_player"] = other
        state["waiting_for_embed"] = True
        return

    user_flip = state["round_flips"]["you"]
    house_flip = state["round_flips"]["me"]
    if user_flip == state["user_side"]:
        state["adder_score"] += 1
    if house_flip == state["house_side"]:
        state["self_score"] += 1

    await channel.send(f"`{state['self_score']}-{state['adder_score']}`")

    first_to = state["first_to"]
    if state["self_score"] >= first_to or state["adder_score"] >= first_to:
        self_won = state["self_score"] >= first_to
        winner_id = bot_user.id if self_won else form["ticket_user_id"]
        await channel.send(f"<@{winner_id}> won!")
        await end_game(channel, form, self_won, bot_user)
        return

    state["round_flips"] = {"me": None, "you": None}
    state["current_player"] = state["first_player"]
    state["waiting_for_embed"] = True


async def handle_da_hood_message(message, form, bot_user):
    state = form["game_state"]
    if state.get("game_type") == "coinflip":
        await handle_coinflip_embed(message, form, bot_user)
    else:
        await handle_roll_embed(message, form, bot_user)


async def start_game(channel, form, bot_user):
    responses = form["responses"]
    game = responses.get("game", "dice")
    first_to = int(responses.get("first_to", "ft3").replace("ft", ""))

    if game == "coinflip":
        side = (responses.get("side", "heads") or "heads").lower()
        if side in ("h", "heads"):
            user_side, house_side = "heads", "tails"
        elif side in ("t", "tails"):
            user_side, house_side = "tails", "heads"
        else:
            user_side, house_side = side, "tails" if side == "heads" else "heads"

        form["game_state"] = {
            "game_type": "coinflip",
            "first_to": first_to,
            "user_side": user_side,
            "house_side": house_side,
            "self_score": 0,
            "adder_score": 0,
            "first_player": "you",
            "current_player": "you",
            "round_flips": {"me": None, "you": None},
            "waiting_for_embed": True,
        }
        return

    first_player = (
        responses.get("first", "@gatodicer 1")
        .replace(" 1", "")
        .replace("@mention", "you")
        .replace("@gatodicer", "me")
    )
    form["game_state"] = {
        "game_type": "dice",
        "first_to": first_to,
        "mode": responses.get("mode", "normal"),
        "gamemode": responses.get("gamemode", "fair"),
        "self_score": 0,
        "adder_score": 0,
        "first_player": first_player,
        "current_player": first_player,
        "waiting_for_embed": False,
        "roll_initiator_id": None,
        "user_totals_queue": [],
        "pending_bot_total": None,
        "pending_user_embeds": 0,
        "bot_rolls_remaining": 0,
    }
    await do_next_roll(channel, form, bot_user)
