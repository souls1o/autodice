import asyncio
import random
import re

import discord

import config
from forms import is_cf_command, is_roll_command, member_has_listen_role
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
    return state["current_player"] in ("me", "@gengardicer")


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


def _is_cf_mm(form, author):
    mm_id = form.get("funds_recipient_id")
    if mm_id:
        return author.id == mm_id
    return member_has_listen_role(author)


def note_mm_cf_command(message, form):
    """Record MM -cf so the following Heads/Tails embed can be matched to it."""
    state = form.get("game_state") or {}
    if state.get("game_type") != "coinflip":
        return False
    if not is_cf_command(message.content):
        return False
    if not _is_cf_mm(form, message.author):
        return False
    if state.get("scoring"):
        return False
    if state.get("pending_cf_cmd_id"):
        return True
    if message.id in state.get("consumed_cf_cmd_ids", set()):
        return True
    state["pending_cf_cmd_id"] = message.id
    state["waiting_for_embed"] = True
    return True


def _cf_embed_text(message):
    parts = []
    for embed in message.embeds or []:
        parts.extend([embed.title or "", embed.description or ""])
        for field in embed.fields:
            parts.extend([field.name or "", field.value or ""])
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
        if embed.author and embed.author.name:
            parts.append(embed.author.name)
    return " ".join(parts).lower()


def parse_cf_flip(message):
    """Return 'heads' or 'tails' from a CF embed, or None."""
    if not message.embeds:
        return None
    for embed in message.embeds:
        for field in embed.fields:
            name = (field.name or "").lower()
            if any(k in name for k in ("result", "winner", "outcome", "side", "landed")):
                value = (field.value or "").lower()
                if re.search(r"\bheads\b", value) and not re.search(r"\btails\b", value):
                    return "heads"
                if re.search(r"\btails\b", value) and not re.search(r"\bheads\b", value):
                    return "tails"
                match = re.search(r"\b(heads|tails)\b", value)
                if match:
                    return match.group(1)
    text = _cf_embed_text(message)
    winner = re.search(
        r"(?:winner|won|result|landed(?:\s+on)?|flipped|side)\s*[:\-]?\s*\*?\*?(heads|tails)",
        text,
    )
    if winner:
        return winner.group(1)
    has_heads = bool(re.search(r"\bheads\b", text))
    has_tails = bool(re.search(r"\btails\b", text))
    if has_heads and not has_tails:
        return "heads"
    if has_tails and not has_heads:
        return "tails"
    bold = re.findall(r"\*\*(heads|tails)\*\*", text)
    if len(bold) == 1:
        return bold[0]
    found = re.findall(r"\b(heads|tails)\b", text)
    if found:
        return found[-1]
    return None


async def _resolve_cf_command(message):
    ref = message.reference
    if ref:
        cmd = ref.resolved if isinstance(getattr(ref, "resolved", None), discord.Message) else None
        if cmd is None and getattr(ref, "message_id", None):
            try:
                cmd = await message.channel.fetch_message(ref.message_id)
            except Exception:
                cmd = None
        if cmd is not None and is_cf_command(cmd.content):
            return cmd
    return await get_command_before_message(
        message.channel,
        message,
        lambda m: is_cf_command(m.content) and not getattr(m.author, "bot", False),
    )


async def trigger_bot_roll(roll_channel, form, bot_user):
    state = form["game_state"]
    # Lock immediately so overlapping calls / embeds can't double-roll
    if state.get("bot_roll_in_flight"):
        return

    state["bot_roll_in_flight"] = True
    state["waiting_for_embed"] = True
    state["roll_initiator_id"] = bot_user.id
    state["pending_bot_roll_cmd_id"] = None
    try:
        await asyncio.sleep(1)
        # Re-check game still active and still our turn to send
        if "game_state" not in form or form["game_state"] is not state:
            return
        if state.get("scoring"):
            return
        hype = random.choice(config.ROLL_HYPE_MESSAGES)
        sent = await send_channel(roll_channel, f"-roll {hype}")
        state["waiting_for_embed"] = True
        state["roll_initiator_id"] = bot_user.id
        if sent is not None:
            state["pending_bot_roll_cmd_id"] = sent.id
    finally:
        state["bot_roll_in_flight"] = False


def _queue_user_roll(state, message_id):
    if message_id in state.get("consumed_roll_cmd_ids", set()):
        return
    pending = state.setdefault("pending_roll_message_ids", [])
    queued = state.setdefault("queued_user_roll_ids", [])
    if message_id not in pending and message_id not in queued:
        queued.append(message_id)


def _accept_user_roll(state, message_id, ticket_user_id):
    if message_id in state.get("consumed_roll_cmd_ids", set()):
        return
    pending = state.setdefault("pending_roll_message_ids", [])
    state.setdefault("pending_user_embeds", 0)
    if message_id not in pending:
        pending.append(message_id)
        state["pending_user_embeds"] += 1
    state["waiting_for_embed"] = True
    state["roll_initiator_id"] = ticket_user_id


def _user_can_accept_rolls(state, bot_user_id):
    if state.get("scoring"):
        return False
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
    state.setdefault("consumed_roll_cmd_ids", set()).add(cmd_id)
    pending = state.get("pending_roll_message_ids", [])
    if cmd_id in pending:
        pending.remove(cmd_id)
        if state.get("pending_user_embeds", 0) > 0:
            state["pending_user_embeds"] -= 1
    queued = state.get("queued_user_roll_ids", [])
    if cmd_id in queued:
        queued.remove(cmd_id)


def _stash_prefetched_user_total(state, cmd_id, total):
    """Keep out-of-turn user embed totals across score/reset so bot can answer them."""
    if cmd_id in state.get("consumed_roll_cmd_ids", set()):
        return
    _queue_user_roll(state, cmd_id)
    prefs = state.setdefault("prefetched_user_totals", [])
    if any(p["cmd_id"] == cmd_id for p in prefs):
        return
    prefs.append({"cmd_id": cmd_id, "total": total})


def _take_prefetched_user_total(state, cmd_id=None):
    prefs = state.get("prefetched_user_totals", [])
    if not prefs:
        return None
    if cmd_id is None:
        entry = prefs.pop(0)
    else:
        entry = None
        for i, item in enumerate(prefs):
            if item["cmd_id"] == cmd_id:
                entry = prefs.pop(i)
                break
        if entry is None:
            return None
    queued = state.get("queued_user_roll_ids", [])
    if entry["cmd_id"] in queued:
        queued.remove(entry["cmd_id"])
    return entry["total"]


async def handle_user_roll(message, form, bot_user):
    state = form["game_state"]
    ticket_user_id = form["ticket_user_id"]
    if message.author.id != ticket_user_id:
        return

    # While scoring (incl. waiting on the score message queue), never accept —
    # only queue so the roll survives round reset.
    if state.get("scoring") or not _user_can_accept_rolls(state, bot_user.id):
        _queue_user_roll(state, message.id)
        return

    if state.get("awaiting_user_after_bot") or state.get("pending_bot_total") is not None:
        _accept_user_roll(state, message.id, ticket_user_id)
        return

    _accept_user_roll(state, message.id, ticket_user_id)


def _reset_round_state(state, ticket_user_id=None, bot_user_id=None):
    # Keep queued_user_roll_ids + prefetched_user_totals — out-of-turn rolls
    # that arrived before the score must still be answered next round.
    # Re-queue any pending cmds that were activated but not yet consumed.
    consumed = state.get("consumed_roll_cmd_ids", set())
    pending = state.get("pending_roll_message_ids", [])
    queued = state.setdefault("queued_user_roll_ids", [])
    for roll_id in pending:
        if roll_id not in consumed and roll_id not in queued:
            queued.append(roll_id)

    state["user_totals_queue"] = []
    state["pending_user_embeds"] = 0
    state["pending_roll_message_ids"] = []
    state["bot_rolls_remaining"] = 0
    state["pending_bot_total"] = None
    state["awaiting_user_after_bot"] = False
    state.pop("bot_first_embed_id", None)
    state["pending_bot_roll_cmd_id"] = None
    state["waiting_for_embed"] = False
    state["roll_initiator_id"] = None
    state["bot_roll_in_flight"] = False
    state["current_player"] = state["first_player"]


async def _answer_early_user_rolls(roll_channel, form, bot_user, bot):
    """
    If the player already rolled out of turn (cmd and/or embed), bot must roll
    to match those totals and score — separate from the next bot-first opener.
    Returns True if it started matching / is waiting on embeds.
    """
    state = form["game_state"]
    prefs = state.setdefault("prefetched_user_totals", [])
    queued = state.setdefault("queued_user_roll_ids", [])
    consumed = state.get("consumed_roll_cmd_ids", set())

    # Drop zombies that were already scored
    if queued:
        state["queued_user_roll_ids"] = [r for r in queued if r not in consumed]
        queued = state["queued_user_roll_ids"]

    if not prefs and not queued:
        return False

    # Fold every prefetched embed into the match queue
    while prefs:
        entry = prefs.pop(0)
        if entry["cmd_id"] in consumed:
            continue
        state.setdefault("user_totals_queue", []).append(entry["total"])
        _consume_user_roll_cmd(state, entry["cmd_id"])
        consumed = state.get("consumed_roll_cmd_ids", set())

    # Only pull queued cmds that already have embeds. Leave the rest queued
    # so a later embed can stash + resume (don't activate-then-lose on reset).
    still_queued = []
    while queued:
        roll_id = queued.pop(0)
        if roll_id in consumed:
            continue
        stashed = _take_prefetched_user_total(state, roll_id)
        if stashed is not None:
            state.setdefault("user_totals_queue", []).append(stashed)
            _consume_user_roll_cmd(state, roll_id)
            consumed = state.get("consumed_roll_cmd_ids", set())
        else:
            still_queued.append(roll_id)
    queued.extend(still_queued)

    if state.get("user_totals_queue"):
        state["current_player"] = "you"
        state["waiting_for_embed"] = False
        state["bot_rolls_remaining"] = len(state["user_totals_queue"])
        await trigger_bot_roll(roll_channel, form, bot_user)
        return True

    # Cmds queued without embeds yet must NOT block the bot-first opener.
    # When their embeds arrive they stash and get matched (or pair with opener).
    return False


async def _start_next_round(roll_channel, form, bot_user, bot):
    """After a scored pair: match any ready early user rolls, then resume turn order."""
    if "game_state" not in form:
        return
    state = form["game_state"]
    state["current_player"] = state["first_player"]

    # Answer ready out-of-turn player rolls first (match + score), then open.
    if await _answer_early_user_rolls(roll_channel, form, bot_user, bot):
        return

    if is_bot_turn(state):
        await do_next_roll(roll_channel, form, bot_user, bot)
    else:
        _try_activate_queued_user_rolls(state, form["ticket_user_id"], bot_user.id)


def _apply_bot_roll_bonus(me_total, gamemode, roll_mode):
    """House roll bonus — plus1: +1 normal, -1 crazy (e.g. 3&4 → 8 / 6)."""
    if gamemode == "plus1":
        return me_total - 1 if roll_mode == "crazy" else me_total + 1
    return me_total


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
    state["scoring"] = True
    try:
        me_total = _apply_bot_roll_bonus(me_total, state["gamemode"], state["mode"])
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
            await end_game(ticket_channel, form, self_won, bot_user, bot)
            return True

        if continue_batch:
            await send_channel(ticket_channel, f"`{state['self_score']}-{state['adder_score']}`")
            return False

        # Post score while still scoring-locked so out-of-turn rolls only queue
        await send_channel(ticket_channel, f"`{state['self_score']}-{state['adder_score']}`")
        _reset_round_state(state)
        state["scoring"] = False
        await _start_next_round(roll_channel, form, bot_user, bot)
        return False
    finally:
        state["scoring"] = False


async def do_next_roll(roll_channel, form, bot_user, bot):
    state = form["game_state"]
    if state.get("game_type") != "dice":
        return
    if state.get("waiting_for_embed") or state.get("bot_roll_in_flight") or state.get("scoring"):
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


def _next_expected_user_roll_id(state):
    """Oldest unconsumed user -roll we are waiting on (pending first, then queued)."""
    consumed = state.get("consumed_roll_cmd_ids", set())
    for rid in state.get("pending_roll_message_ids", []):
        if rid not in consumed:
            return rid
    for rid in state.get("queued_user_roll_ids", []):
        if rid not in consumed:
            return rid
    return None


async def _find_nearest_roll_cmd(channel, embed_message):
    """Nearest -roll command before this embed (any author)."""
    async for msg in channel.history(limit=50, before=embed_message):
        if is_roll_command(msg.content):
            return msg
    return None


async def _find_user_roll_cmd(channel, embed_message, form):
    """Map a dice embed to the correct user -roll using FIFO, not most-recent."""
    state = form["game_state"]
    expected_id = _next_expected_user_roll_id(state)
    ticket_user_id = form["ticket_user_id"]
    bot_first_id = state.get("bot_first_embed_id")

    if expected_id is not None:
        async for msg in channel.history(limit=50, before=embed_message):
            if msg.id == expected_id:
                return msg if is_roll_command(msg.content) else None
        return None

    candidates = []
    async for msg in channel.history(limit=50, before=embed_message):
        if bot_first_id and msg.id <= bot_first_id:
            break
        if not is_roll_command(msg.content):
            continue
        if msg.author.id != ticket_user_id:
            continue
        if msg.id in state.get("consumed_roll_cmd_ids", set()):
            continue
        candidates.append(msg)
    return candidates[-1] if candidates else None


async def handle_roll_embed(message, form, bot_user, bot):
    state = form["game_state"]
    state.setdefault("consumed_embed_ids", set())
    state.setdefault("consumed_roll_cmd_ids", set())
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
    total = rolls[0] + rolls[1]
    state.setdefault("user_totals_queue", [])
    state.setdefault("pending_user_embeds", 0)
    state.setdefault("bot_rolls_remaining", 0)
    state.setdefault("prefetched_user_totals", [])
    state.setdefault("queued_user_roll_ids", [])

    nearest = await _find_nearest_roll_cmd(message.channel, message)
    if not nearest:
        state["consumed_embed_ids"].discard(message.id)
        return

    # HARD RULE: whose -roll caused this embed decides the path.
    # A player embed must NEVER be scored as the bot's point.
    if nearest.author.id == ticket_user_id:
        await _handle_user_roll_embed(message, form, bot_user, bot, nearest, total)
        return
    if nearest.author.id == bot_user.id:
        await _handle_bot_roll_embed(message, form, bot_user, bot, nearest, total)
        return

    state["consumed_embed_ids"].discard(message.id)


async def _handle_user_roll_embed(message, form, bot_user, bot, cmd, total):
    state = form["game_state"]
    ticket_user_id = form["ticket_user_id"]

    if state.get("scoring"):
        _stash_prefetched_user_total(state, cmd.id, total)
        return

    # Pair against a pending bot-first total
    pending_bot_total = state.get("pending_bot_total")
    if pending_bot_total is not None:
        state["pending_bot_total"] = None
        state["awaiting_user_after_bot"] = False
        state.pop("bot_first_embed_id", None)
        state["pending_user_embeds"] = 0
        state["user_totals_queue"] = []
        state["waiting_for_embed"] = False
        _consume_user_roll_cmd(state, cmd.id)
        stashed = _take_prefetched_user_total(state, cmd.id)
        await _score_pair(
            message.channel, form, bot_user, bot, pending_bot_total,
            stashed if stashed is not None else total,
        )
        return

    pending_ids = state.get("pending_roll_message_ids", [])
    queued_ids = state.get("queued_user_roll_ids", [])

    # Waiting on OUR bot embed — extra player embeds are early rolls, never bot points
    waiting_on_bot = (
        state.get("waiting_for_embed")
        and state.get("roll_initiator_id") == bot_user.id
    ) or state.get("bot_roll_in_flight") or bool(state.get("pending_bot_roll_cmd_id"))

    out_of_turn = (
        is_bot_turn(state)
        and state.get("pending_bot_total") is None
        and not state.get("awaiting_user_after_bot")
        and cmd.id not in pending_ids
    )

    if waiting_on_bot or out_of_turn or (cmd.id in queued_ids and cmd.id not in pending_ids):
        _stash_prefetched_user_total(state, cmd.id, total)
        idle = (
            not state.get("bot_roll_in_flight")
            and not state.get("waiting_for_embed")
            and not state.get("scoring")
            and not state.get("user_totals_queue")
            and not state.get("pending_bot_roll_cmd_id")
        )
        if idle:
            await _start_next_round(message.channel, form, bot_user, bot)
        return

    # Legitimate user-first roll for this round
    _consume_user_roll_cmd(state, cmd.id)
    state["user_totals_queue"].append(total)

    if state.get("pending_user_embeds", 0) > 0:
        state["waiting_for_embed"] = True
        state["roll_initiator_id"] = ticket_user_id
        return

    state["waiting_for_embed"] = False
    state["bot_rolls_remaining"] = len(state["user_totals_queue"])
    await trigger_bot_roll(message.channel, form, bot_user)


async def _handle_bot_roll_embed(message, form, bot_user, bot, cmd, total):
    state = form["game_state"]
    ticket_user_id = form["ticket_user_id"]

    # Only accept the embed for the -roll we actually just sent
    pending_cmd_id = state.get("pending_bot_roll_cmd_id")
    if pending_cmd_id is not None and cmd.id != pending_cmd_id:
        # Stale bot -roll from an earlier round — ignore
        return
    if pending_cmd_id is None and not (
        state.get("waiting_for_embed") and state.get("roll_initiator_id") == bot_user.id
    ) and not state.get("user_totals_queue"):
        # Not expecting a bot embed right now
        return

    state["pending_bot_roll_cmd_id"] = None

    # Match against queued user totals (user went first / early-roll batch)
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

    # Bot opener: pair with a prefetched early user total if present
    prefetched = _take_prefetched_user_total(state)
    if prefetched is not None:
        state["waiting_for_embed"] = False
        state["pending_bot_total"] = None
        state["awaiting_user_after_bot"] = False
        state.pop("bot_first_embed_id", None)
        await _score_pair(message.channel, form, bot_user, bot, total, prefetched)
        return

    # Bot went first — hold until user embed
    state["pending_bot_total"] = total
    state["bot_first_embed_id"] = message.id
    state["awaiting_user_after_bot"] = True
    state["pending_user_embeds"] = 0
    state["user_totals_queue"] = []
    state["current_player"] = "you"
    state["waiting_for_embed"] = False
    _try_activate_queued_user_rolls(state, ticket_user_id, bot_user.id)

    pending_ids = list(state.get("pending_roll_message_ids", []))
    for cmd_id in pending_ids:
        stashed = _take_prefetched_user_total(state, cmd_id)
        if stashed is None:
            continue
        _consume_user_roll_cmd(state, cmd_id)
        bot_total = state.get("pending_bot_total")
        if bot_total is None:
            state.setdefault("user_totals_queue", []).append(stashed)
            continue
        state["pending_bot_total"] = None
        state["awaiting_user_after_bot"] = False
        state.pop("bot_first_embed_id", None)
        state["waiting_for_embed"] = False
        await _score_pair(message.channel, form, bot_user, bot, bot_total, stashed)
        return


async def handle_coinflip_embed(message, form, bot_user, bot):
    """
    One MM -cf per point. Nearest -cf before the embed must be from the MM
    who received the crypto (funds_recipient_id). Embed must contain Heads or Tails.
    """
    state = form["game_state"]
    if state.get("scoring"):
        return
    if not state.get("waiting_for_embed"):
        return
    consumed = state.setdefault("consumed_embed_ids", set())
    consumed_cmds = state.setdefault("consumed_cf_cmd_ids", set())
    if message.id in consumed:
        return

    flip = parse_cf_flip(message)
    if not flip:
        return

    cmd = await _resolve_cf_command(message)
    if not cmd:
        return
    if cmd.id in consumed_cmds:
        return
    if not _is_cf_mm(form, cmd.author):
        return

    pending = state.get("pending_cf_cmd_id")
    if not pending or cmd.id != pending:
        return

    consumed.add(message.id)
    consumed_cmds.add(cmd.id)
    state.pop("pending_cf_cmd_id", None)
    state["scoring"] = True

    try:
        user_side = (state.get("user_side") or "heads").lower()
        if flip == user_side:
            state["adder_score"] += 1
        else:
            state["self_score"] += 1

        ticket_channel = await get_ticket_channel(bot, form, fallback=message.channel)
        await send_channel(ticket_channel, f"`{state['self_score']}-{state['adder_score']}`")

        first_to = int(state.get("first_to") or 2)
        if state["self_score"] >= first_to or state["adder_score"] >= first_to:
            self_won = state["self_score"] >= first_to
            await end_game(ticket_channel, form, self_won, bot_user, bot)
            return
    finally:
        if form.get("game_state") is state:
            state["scoring"] = False
            state["waiting_for_embed"] = True


async def handle_da_hood_message(message, form, bot_user, bot):
    state = form["game_state"]
    if state.get("game_type") == "coinflip":
        await handle_coinflip_embed(message, form, bot_user, bot)
    else:
        await handle_roll_embed(message, form, bot_user, bot)


async def start_game(channel, form, bot_user, bot=None):
    from users import (
        credit_rakeback,
        debit_rakeback_stake_for_form,
        record_user_wager_on_game_start,
    )
    from message_queue import send_channel

    # Debit rakeback before hold; refund if hold apply fails.
    ok, err, rb_debited = await debit_rakeback_stake_for_form(form)
    if not ok:
        await send_channel(channel, err or "❌ Could not apply rakeback.")
        await payout_winnings_if_any(channel, form)
        return

    needs_hold = form.pop("pending_rerun_fund", False) or form.get("pending_hold_deduct") is not None
    if needs_hold or form.get("pending_wager_usd") is not None:
        if not await apply_hold_after_confirm(channel, form):
            if rb_debited > 0 and form.get("ticket_user_id"):
                await credit_rakeback(form["ticket_user_id"], rb_debited)
            await payout_winnings_if_any(channel, form)
            return

    from bets import apply_player_hold_stake
    apply_player_hold_stake(form)
    save_session_from_form(channel.id, form)

    await record_user_wager_on_game_start(form)
    form["game_started"] = True
    form["ticket_channel_id"] = channel.id
    save_session_from_form(channel.id, form)
    if bot:
        await notify_admin_game_started(bot, channel, form)
    responses = form["responses"]
    game = responses.get("game", "dice")

    if game == "coinflip":
        side = (responses.get("side", "heads") or "heads").lower()
        if side in ("h", "heads"):
            user_side, house_side = "heads", "tails"
        elif side in ("t", "tails"):
            user_side, house_side = "tails", "heads"
        else:
            user_side, house_side = side, "tails" if side == "heads" else "heads"

        gamemode = responses.get("gamemode", "lead")
        if gamemode == "lead_10":
            gamemode = "lead"
            first_to_raw = responses.get("first_to") or "ft2"
        else:
            first_to_raw = responses.get("first_to", "ft3")
        first_to = int(str(first_to_raw).replace("ft", "") or "3")
        is_lead = gamemode == "lead"
        self_score, adder_score = (1, 0) if is_lead else (0, 0)

        form["game_state"] = {
            "game_type": "coinflip",
            "gamemode": gamemode,
            "first_to": first_to,
            "user_side": user_side,
            "house_side": house_side,
            "self_score": self_score,
            "adder_score": adder_score,
            "waiting_for_embed": True,
            "scoring": False,
            "consumed_embed_ids": set(),
            "consumed_cf_cmd_ids": set(),
        }
        if is_lead:
            await send_channel(channel, f"`{self_score}-{adder_score}`")
        return

    first_to = int(responses.get("first_to", "ft3").replace("ft", ""))
    first_raw = responses.get("first", "@gengardicer 1").replace(" 1", "").strip()
    ticket_user_id = form.get("ticket_user_id")
    if first_raw in ("@mention", "you") or (
        ticket_user_id and str(ticket_user_id) in first_raw
    ):
        first_player = "you"
    elif first_raw in ("@gengardicer", "me") or str(bot_user.id) in first_raw:
        first_player = "me"
    else:
        first_player = first_raw
    gamemode = responses.get("gamemode", "fair")
    if gamemode == "lead_10":
        gamemode = "lead"
    is_lead = gamemode == "lead"
    self_score, adder_score = (1, 0) if is_lead else (0, 0)
    form["game_state"] = {
        "game_type": "dice",
        "first_to": first_to,
        "mode": responses.get("mode", "normal"),
        "gamemode": gamemode,
        "self_score": self_score,
        "adder_score": adder_score,
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
        "prefetched_user_totals": [],
        "consumed_roll_cmd_ids": set(),
        "bot_rolls_remaining": 0,
        "bot_roll_in_flight": False,
        "pending_bot_roll_cmd_id": None,
        "scoring": False,
    }
    if is_lead:
        await send_channel(channel, f"`{self_score}-{adder_score}`")
    roll_channel = await get_ticket_channel(bot, form) if bot else channel
    await do_next_roll(roll_channel, form, bot_user, bot)
