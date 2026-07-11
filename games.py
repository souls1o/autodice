import asyncio
import random
import re

import config
from forms import is_roll_command, member_has_listen_role
from message_queue import send_channel
from postgame import apply_hold_after_confirm, end_game, payout_winnings_if_any
from state import save_session_from_form
from notifications import notify_admin_game_started

DA_HOOD_BOT_ID = 1200925985999171706
ROLL_EMBED_PATTERN = re.compile(r"(\d+)\s*(?:&|\+)\s*(\d+)")


async def get_ticket_channel(bot, form, fallback=None):
    if bot is None:
        return fallback
    channel = bot.get_channel(form["ticket_channel_id"])
    if channel is None:
        channel = await bot.fetch_channel(form["ticket_channel_id"])
    return channel


def is_bot_turn(state):
    return state["current_player"] in ("me", "@gatodicer")


def current_player_key(state):
    return "me" if is_bot_turn(state) else "you"


def other_player_key(player):
    return "you" if player == "me" else "me"


async def get_roll_command_before_embed(
    channel, embed_message, *, initiator_id=None, exclude_author_id=None, after_message_id=None
):
    async for msg in channel.history(limit=50, before=embed_message):
        if not is_roll_command(msg.content):
            continue
        if after_message_id and msg.id <= after_message_id:
            continue
        if exclude_author_id and msg.author.id == exclude_author_id:
            continue
        if initiator_id and msg.author.id != initiator_id:
            continue
        return msg
    return None


async def get_command_before_message(channel, embed_message, predicate):
    async for msg in channel.history(limit=30, before=embed_message):
        if predicate(msg):
            return msg
    return None


async def trigger_bot_roll(roll_channel, form, bot_user):
    state = form["game_state"]
    # Lock immediately so overlapping calls / embeds can't double-roll
    if state.get("bot_roll_in_flight"):
        return
    if state.get("waiting_for_embed") and state.get("roll_initiator_id") == bot_user.id:
        return

    state["bot_roll_in_flight"] = True
    state["waiting_for_embed"] = True
    state["roll_initiator_id"] = bot_user.id
    try:
        await asyncio.sleep(1)
        # Re-check game still active and still our turn to send
        if "game_state" not in form or form["game_state"] is not state:
            return
        hype = random.choice(config.ROLL_HYPE_MESSAGES)
        await send_channel(roll_channel, f"-roll {hype}")
        state["waiting_for_embed"] = True
        state["roll_initiator_id"] = bot_user.id
    finally:
        state["bot_roll_in_flight"] = False


def _queue_user_roll(state, message_id):
    pending = state.setdefault("pending_roll_message_ids", [])
    queued = state.setdefault("queued_user_roll_ids", [])
    if message_id not in pending and message_id not in queued:
        queued.append(message_id)


def _accept_user_roll(state, message_id, ticket_user_id):
    pending = state.setdefault("pending_roll_message_ids", [])
    state.setdefault("pending_user_embeds", 0)
    if message_id not in pending:
        pending.append(message_id)
        state["pending_user_embeds"] += 1
    state["waiting_for_embed"] = True
    state["roll_initiator_id"] = ticket_user_id


def _user_can_accept_rolls(state, bot_user_id):
    if state.get("bot_roll_in_flight"):
        return False
    if state.get("awaiting_user_after_bot") or state.get("pending_bot_total") is not None:
        # One user roll at a time when pairing against a pending bot total
        if state.get("pending_user_embeds", 0) > 0:
            return False
        return True
    if not is_bot_turn(state):
        waiting = state.get("waiting_for_embed")
        initiator = state.get("roll_initiator_id")
        if waiting and initiator == bot_user_id:
            return False
        # Don't stack multiple pending embeds for the same pair — queue extras
        if state.get("pending_user_embeds", 0) > 0:
            return False
        return True
    waiting = state.get("waiting_for_embed")
    initiator = state.get("roll_initiator_id")
    return bool(waiting and initiator != bot_user_id)


def _try_activate_queued_user_rolls(state, ticket_user_id, bot_user_id):
    queue = state.get("queued_user_roll_ids", [])
    if not queue or not _user_can_accept_rolls(state, bot_user_id):
        return
    while queue and _user_can_accept_rolls(state, bot_user_id):
        roll_id = queue.pop(0)
        _accept_user_roll(state, roll_id, ticket_user_id)


def _consume_user_roll_cmd(state, cmd_id):
    pending = state.get("pending_roll_message_ids", [])
    if cmd_id in pending:
        pending.remove(cmd_id)
        if state.get("pending_user_embeds", 0) > 0:
            state["pending_user_embeds"] -= 1
        return
    queued = state.get("queued_user_roll_ids", [])
    if cmd_id in queued:
        queued.remove(cmd_id)


async def handle_user_roll(message, form, bot_user):
    state = form["game_state"]
    ticket_user_id = form["ticket_user_id"]
    if message.author.id != ticket_user_id:
        return

    if state.get("awaiting_user_after_bot") or state.get("pending_bot_total") is not None:
        _accept_user_roll(state, message.id, ticket_user_id)
        return

    if _user_can_accept_rolls(state, bot_user.id):
        _accept_user_roll(state, message.id, ticket_user_id)
    else:
        _queue_user_roll(state, message.id)


def _reset_round_state(state, ticket_user_id=None, bot_user_id=None):
    state["user_totals_queue"] = []
    state["pending_user_embeds"] = 0
    state["pending_roll_message_ids"] = []
    state["bot_rolls_remaining"] = 0
    state["pending_bot_total"] = None
    state["awaiting_user_after_bot"] = False
    state.pop("bot_first_embed_id", None)
    state["waiting_for_embed"] = False
    state["roll_initiator_id"] = None
    state["bot_roll_in_flight"] = False
    state["current_player"] = state["first_player"]
    if ticket_user_id is not None and bot_user_id is not None:
        _try_activate_queued_user_rolls(state, ticket_user_id, bot_user_id)


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


async def _score_pair(roll_channel, form, bot_user, bot, me_total, you_total, *, continue_batch=False):
    state = form["game_state"]
    winner = _pair_winner(me_total, you_total, state["gamemode"], state["mode"])
    ticket_channel = await get_ticket_channel(bot, form, fallback=roll_channel)

    if winner == "me":
        state["self_score"] += 1
    elif winner == "you":
        state["adder_score"] += 1

    first_to = state["first_to"]
    if state["self_score"] >= first_to or state["adder_score"] >= first_to:
        await send_channel(ticket_channel, f"`{state['self_score']}-{state['adder_score']}`")
        self_won = state["self_score"] >= first_to
        winner_id = bot_user.id if self_won else form["ticket_user_id"]
        await send_channel(ticket_channel, f"<@{winner_id}> won!")
        await end_game(ticket_channel, form, self_won, bot_user, bot)
        return True

    if continue_batch:
        await send_channel(ticket_channel, f"`{state['self_score']}-{state['adder_score']}`")
        return False

    _reset_round_state(state, form["ticket_user_id"], bot_user.id)

    await send_channel(ticket_channel, f"`{state['self_score']}-{state['adder_score']}`")
    await do_next_roll(roll_channel, form, bot_user, bot)
    return False


async def do_next_roll(roll_channel, form, bot_user, bot):
    state = form["game_state"]
    if state.get("game_type") != "dice":
        return
    if state.get("waiting_for_embed") or state.get("bot_roll_in_flight"):
        return
    if is_bot_turn(state):
        await trigger_bot_roll(roll_channel, form, bot_user)


def parse_roll_from_embed(message):
    if not message.embeds:
        return None
    embed = message.embeds[0]
    parts = [embed.description or "", embed.title or ""]
    for field in embed.fields:
        parts.append(field.name or "")
        parts.append(field.value or "")
    for text in parts:
        match = ROLL_EMBED_PATTERN.search(text)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


async def _find_roll_command(channel, embed_message, form, bot_user, *, pending_bot_total=None):
    ticket_user_id = form["ticket_user_id"]
    if pending_bot_total is not None:
        return await get_roll_command_before_embed(
            channel,
            embed_message,
            exclude_author_id=bot_user.id,
            after_message_id=form["game_state"].get("bot_first_embed_id"),
        )

    state = form["game_state"]
    cmd = await get_roll_command_before_embed(
        channel, embed_message, initiator_id=state.get("roll_initiator_id")
    )
    if cmd:
        return cmd
    return await get_roll_command_before_embed(
        channel, embed_message, initiator_id=ticket_user_id
    )


async def handle_roll_embed(message, form, bot_user, bot):
    state = form["game_state"]
    state.setdefault("consumed_embed_ids", set())
    if message.id in state["consumed_embed_ids"]:
        return
    if not message.author.bot or not message.embeds:
        return

    rolls = parse_roll_from_embed(message)
    if not rolls:
        return

    # Claim immediately so concurrent handlers can't double-process after await
    state["consumed_embed_ids"].add(message.id)

    ticket_user_id = form["ticket_user_id"]
    pending_bot_total = state.get("pending_bot_total")
    cmd = await _find_roll_command(
        message.channel, message, form, bot_user, pending_bot_total=pending_bot_total
    )
    if not cmd:
        state["consumed_embed_ids"].discard(message.id)
        return

    total = rolls[0] + rolls[1]
    state.setdefault("user_totals_queue", [])
    state.setdefault("pending_user_embeds", 0)
    state.setdefault("bot_rolls_remaining", 0)

    if pending_bot_total is not None and cmd.author.id != bot_user.id:
        bot_total = pending_bot_total
        state["pending_bot_total"] = None
        state["awaiting_user_after_bot"] = False
        state.pop("bot_first_embed_id", None)
        state["pending_user_embeds"] = 0
        state["user_totals_queue"] = []
        state["waiting_for_embed"] = False
        await _score_pair(message.channel, form, bot_user, bot, bot_total, total)
        return

    if cmd.author.id == ticket_user_id:
        _consume_user_roll_cmd(state, cmd.id)
        state["user_totals_queue"].append(total)

        if state.get("pending_user_embeds", 0) > 0:
            state["waiting_for_embed"] = True
            state["roll_initiator_id"] = ticket_user_id
            return

        state["waiting_for_embed"] = False
        state["bot_rolls_remaining"] = len(state["user_totals_queue"])
        await trigger_bot_roll(message.channel, form, bot_user)
        return

    if not state.get("waiting_for_embed") and not state["user_totals_queue"] and not state.get("bot_roll_in_flight"):
        return

    # bot embed — pair 1:1 with the next queued user total
    if state["user_totals_queue"]:
        you_total = state["user_totals_queue"].pop(0)
        state["bot_rolls_remaining"] = max(0, state.get("bot_rolls_remaining", 1) - 1)
        state["waiting_for_embed"] = False
        remaining = state["bot_rolls_remaining"]
        game_over = await _score_pair(
            message.channel, form, bot_user, bot, total, you_total, continue_batch=remaining > 0
        )
        if game_over:
            return
        if remaining > 0:
            await trigger_bot_roll(message.channel, form, bot_user)
        return

    # bot went first this round — hold total until user embed arrives
    state["pending_bot_total"] = total
    state["bot_first_embed_id"] = message.id
    state["awaiting_user_after_bot"] = True
    state["pending_user_embeds"] = 0
    state["user_totals_queue"] = []
    state["current_player"] = "you"
    state["waiting_for_embed"] = False
    _try_activate_queued_user_rolls(state, ticket_user_id, bot_user.id)


async def handle_coinflip_embed(message, form, bot_user, bot):
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

    ticket_channel = await get_ticket_channel(bot, form, fallback=roll_channel)
    await send_channel(ticket_channel, f"`{state['self_score']}-{state['adder_score']}`")

    first_to = state["first_to"]
    if state["self_score"] >= first_to or state["adder_score"] >= first_to:
        self_won = state["self_score"] >= first_to
        winner_id = bot_user.id if self_won else form["ticket_user_id"]
        await send_channel(ticket_channel, f"<@{winner_id}> won!")
        await end_game(ticket_channel, form, self_won, bot_user, bot)
        return

    state["round_flips"] = {"me": None, "you": None}
    state["current_player"] = state["first_player"]
    state["waiting_for_embed"] = True


async def handle_da_hood_message(message, form, bot_user, bot):
    state = form["game_state"]
    if state.get("game_type") == "coinflip":
        await handle_coinflip_embed(message, form, bot_user, bot)
    else:
        await handle_roll_embed(message, form, bot_user, bot)


async def start_game(channel, form, bot_user, bot=None):
    needs_hold = form.pop("pending_rerun_fund", False) or form.get("pending_hold_deduct") is not None
    if needs_hold or form.get("pending_wager_usd") is not None:
        if not await apply_hold_after_confirm(channel, form):
            await payout_winnings_if_any(channel, form)
            return

    form["game_started"] = True
    form["ticket_channel_id"] = channel.id
    save_session_from_form(channel.id, form)
    if bot:
        await notify_admin_game_started(bot, channel, form)
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

    first_raw = responses.get("first", "@gatodicer 1").replace(" 1", "").strip()
    ticket_user_id = form.get("ticket_user_id")
    if first_raw in ("@mention", "you") or (
        ticket_user_id and str(ticket_user_id) in first_raw
    ):
        first_player = "you"
    elif first_raw in ("@gatodicer", "me") or str(bot_user.id) in first_raw:
        first_player = "me"
    else:
        first_player = first_raw
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
        "awaiting_user_after_bot": False,
        "bot_first_embed_id": None,
        "consumed_embed_ids": set(),
        "pending_user_embeds": 0,
        "pending_roll_message_ids": [],
        "queued_user_roll_ids": [],
        "bot_rolls_remaining": 0,
        "bot_roll_in_flight": False,
    }
    roll_channel = await get_ticket_channel(bot, form) if bot else channel
    await do_next_roll(roll_channel, form, bot_user, bot)
