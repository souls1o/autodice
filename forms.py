import random
import config
from bets import (
    bet_validator,
    calculate_my_bet,
    extract_crypto_address,
    get_bet_info,
    get_max_bet,
    get_price,
    get_wager_usd,
    normalize_coin,
    usd_to_smallest_unit,
)
from services import send_apirone
from state import active_forms, get_form, is_maintenance_mode, notify_maintenance, register_ticket_channel, ticket_channels

LISTEN_ROLES = [1258727325265297408, 1258732498482106398]
VALIDATORS = {"bet_validator": bet_validator}


def message_references_bot(message, bot_user):
    content = message.content or ""
    if "gatodicer" in content.lower():
        return True
    if str(bot_user.id) in content:
        return True
    if f"<@{bot_user.id}>" in content or f"<@!{bot_user.id}>" in content:
        return True
    return any(user.id == bot_user.id for user in message.mentions)


def _overwrite_target_ids(channel):
    overwrites = getattr(channel, "overwrites", None)
    if not overwrites:
        return set()
    return {getattr(target, "id", None) for target in overwrites}


def is_channel_blacklisted(channel_id):
    return channel_id in config.CHANNEL_BLACKLIST


def was_bot_added_to_channel(channel, bot_user, before=None):
    if is_channel_blacklisted(channel.id):
        return False
    member = channel.guild.get_member(bot_user.id)
    if member is None:
        return False
    try:
        can_view = channel.permissions_for(member).view_channel
    except Exception:
        return False
    if not can_view:
        return False

    bot_id = bot_user.id
    if bot_id in _overwrite_target_ids(channel):
        return True
    if before is None:
        return False

    try:
        if not before.permissions_for(member).view_channel:
            return True
    except Exception:
        return True

    before_ids = _overwrite_target_ids(before)
    after_ids = _overwrite_target_ids(channel)
    if bot_id in after_ids and bot_id not in before_ids:
        return True

    role_ids = {role.id for role in member.roles}
    return bool(role_ids & (after_ids - before_ids))


def should_process_channel(channel, message=None, bot_user=None):
    if is_channel_blacklisted(channel.id):
        return False
    if channel.id in ticket_channels or channel.id in active_forms:
        return True
    if "ticket" in channel.name.lower():
        return True
    if message is not None and bot_user is not None and message_references_bot(message, bot_user):
        return True
    return False


async def handle_bot_added_to_channel(bot, channel):
    if is_maintenance_mode():
        await notify_maintenance(channel)
        return
    if register_ticket_channel(channel.id):
        await notify_admin_ticket_added(bot, channel)


async def notify_admin_ticket_added(bot, channel):
    try:
        admin = bot.get_user(config.ADMIN_USER_ID)
        if admin is None:
            admin = await bot.fetch_user(config.ADMIN_USER_ID)
        guild_name = channel.guild.name if channel.guild else "Unknown"
        await admin.send(
            f"*📃 New Ticket Created*"
            f"**Channel:** #{channel.name} (`{channel.id}`)\n"
        )
    except Exception:
        pass


def member_has_listen_role(member):
    return any(role.id in LISTEN_ROLES for role in member.roles)


def ticket_mention(channel, form):
    user = channel.guild.get_member(form["ticket_user_id"])
    return user.mention if user else f"<@{form['ticket_user_id']}>"


def format_text(text, mention, responses, bot_user, dynamic=None):
    dynamic = dynamic or {}
    result = text.replace("@mention", mention).replace("@gatodicer", bot_user.mention)
    for key, value in {**responses, **dynamic}.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def build_confirm_text(channel, form, bot_user):
    mention = ticket_mention(channel, form)
    responses = form.get("responses", {})
    game = responses.get("game", "dice")
    first_to = responses.get("first_to", "ft3")
    gamemode_key = responses.get("gamemode", "7s")
    first = responses.get("first", "@gatodicer 1").replace("@mention", mention).replace("@gatodicer", bot_user.mention)
    mode = responses.get("mode", "normal")
    side = responses.get("side", "h")

    gamemode_text = {
        "7s": f", {bot_user.mention} wins 7s",
        "7s_ties": f", {bot_user.mention} wins 7s and ties",
        "ties": f", {bot_user.mention} wins ties",
        "fair": "",
    }.get(gamemode_key, "wins 7s")

    if game == "dice":
        return f"dice {first_to} {mode} {first}{gamemode_text}"
    return f"cf {first_to} {first} {side}"


async def start_ticket_form(channel, bot_user, bot=None):
    if is_channel_blacklisted(channel.id):
        return
    if get_form(channel.id):
        return

    was_tracked = channel.id in ticket_channels
    ticket_user_id = None
    bot_referenced = False
    async for msg in channel.history(limit=30):
        if message_references_bot(msg, bot_user):
            bot_referenced = True
            ticket_user_id = msg.author.id
            break

    if not bot_referenced and not was_tracked:
        return

    if is_maintenance_mode():
        await notify_maintenance(channel)
        return

    if not ticket_user_id:
        async for msg in channel.history(limit=30):
            if not msg.author.bot:
                ticket_user_id = msg.author.id
                break

    if register_ticket_channel(channel.id) and bot:
        await notify_admin_ticket_added(bot, channel)

    active_forms[channel.id] = {
        "ticket_user_id": ticket_user_id,
        "step": 0,
        "responses": {},
        "waiting_for_address": False,
        "waiting_for_confirm": False,
    }
    await ask_next_step(channel, bot_user)


async def ask_next_step(channel, bot_user):
    form = get_form(channel.id)
    if not form:
        return

    while form["step"] < len(config.FORM_QUESTIONS):
        q = config.FORM_QUESTIONS[form["step"]]
        game = form["responses"].get("game")
        if "only_for" not in q or not game or game in q["only_for"]:
            break
        form["step"] += 1

    q = config.FORM_QUESTIONS[form["step"]]
    mention = ticket_mention(channel, form)
    responses = form.get("responses", {})
    game = responses.get("game")
    dynamic = {"max_bet": get_max_bet(form), "game_emoji": "Dices" if game == "dice" else "Coin"}
    question_text = format_text(q.get("text", ""), mention, responses, bot_user, dynamic)

    if q["type"] in ("choice", "open"):
        await channel.send(question_text)
        return

    if q["type"] == "listen_address":
        bet_parts = responses.get("bet", "").split()
        dynamic.update({
            "coin": normalize_coin(bet_parts[-1]),
            "my_bet": calculate_my_bet(form),
            "his_bet": bet_parts[0],
        })
        question_text = format_text(q.get("text", ""), mention, responses, bot_user, dynamic)
        form["waiting_for_address"] = True
    elif q["type"] == "listen_confirm":
        question_text = build_confirm_text(channel, form, bot_user)
        form["confirm_text"] = question_text
        form["waiting_for_confirm"] = True

    await channel.send(question_text)


async def handle_form_step(message, form, bot_user):
    if form["step"] >= len(config.FORM_QUESTIONS):
        return
    if form["ticket_user_id"] != message.author.id:
        return

    q = config.FORM_QUESTIONS[form["step"]]
    response = message.content.strip()
    upper_resp = response.upper()

    if q["type"] == "choice":
        output_value = None
        random_inputs = q["mapping"].get("random", [])
        if upper_resp in ("RANDOM", "R") or any(upper_resp == inp.upper() for inp in random_inputs):
            options = [val for val in q["mapping"] if val.lower() != "random"]
            output_value = random.choice(options) if options else None
        else:
            for val, inputs in q["mapping"].items():
                if val.lower() == "random":
                    continue
                if any(upper_resp == inp.upper() for inp in inputs):
                    output_value = val
                    break
        if output_value is None:
            return
        if q.get("short_key"):
            form["responses"][q["short_key"]] = output_value
        form["step"] += 1
        await ask_next_step(message.channel, bot_user)
        return

    if q["type"] == "open":
        validator = VALIDATORS.get(q.get("validator"))
        if validator and not validator(response, form):
            await message.reply("❌ Invalid format or out of range.")
            return
        if q.get("short_key"):
            form["responses"][q["short_key"]] = response
        form["step"] += 1
        await ask_next_step(message.channel, bot_user)


async def handle_global_listeners(message, bot_user, start_game_fn):
    form = get_form(message.channel.id)
    if not form:
        return

    if form.get("waiting_for_rerun"):
        from postgame import handle_rerun_response
        await handle_rerun_response(message, form, bot_user, start_game_fn)
        if message.channel.id not in active_forms:
            return

    form = get_form(message.channel.id)
    if not form:
        return

    if form.get("waiting_for_address") and member_has_listen_role(message.author):
        _, _, coin = get_bet_info(form)
        address = extract_crypto_address(message.content, coin)
        if address:
            wager_usd = get_wager_usd(form)
            amount = usd_to_smallest_unit(wager_usd, coin, get_price(coin))
            result = await send_apirone(coin, address, amount)
            if "error" in result:
                err = result["error"]
                await message.channel.send(
                    f"❌ Transfer failed: {err if isinstance(err, str) else err}"
                )
                return
            form["waiting_for_address"] = False
            form["payout_address"] = address
            await message.channel.send(f"📤 Sent `${wager_usd}` {coin.upper()} to `{address}`")
            form["step"] += 1
            await ask_next_step(message.channel, bot_user)

    if form.get("waiting_for_confirm"):
        expected = form.get("confirm_text")
        if expected and message.content.strip() == expected.strip() and member_has_listen_role(message.author):
            await message.reply("conf")
            form["waiting_for_adder_confirm"] = True

        if (
            form.get("waiting_for_adder_confirm")
            and message.author.id == form["ticket_user_id"]
            and message.content.lower() == "conf"
        ):
            form["waiting_for_confirm"] = False
            form["waiting_for_adder_confirm"] = False
            await start_game_fn(message.channel, form, bot_user)
