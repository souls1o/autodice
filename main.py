import discord
from discord.ext import commands, tasks
import asyncio
import config
from forms import (
    build_dm_gamemodes_text,
    build_dm_help_text,
    handle_bot_added_to_channel,
    handle_form_step,
    handle_global_listeners,
    handle_ticket_command,
    is_roll_command,
    should_process_channel,
    start_ticket_form,
    was_bot_added_to_channel,
)
from games import DA_HOOD_BOT_ID, handle_da_hood_message, handle_user_roll, start_game
from services import get_house_balance_text, get_stats, get_wallets
from state import active_forms, clear_ticket_session, get_form, is_maintenance_mode, is_ticket_channel, toggle_maintenance, toggle_testing

bot = commands.Bot(command_prefix="!", self_bot=True)


def ensure_auto_post():
    if auto_post.is_running():
        return
    auto_post.start()
    print("[auto_post] task started")


@bot.event
async def on_ready():
    print(f"✅ Selfbot logged in as {bot.user} (ID: {bot.user.id})")
    ensure_auto_post()
    if not watchdog.is_running():
        watchdog.start()


@bot.event
async def on_disconnect():
    print("⚠️ Disconnected from Discord gateway — reconnecting...")


@bot.event
async def on_resumed():
    print("✅ Session resumed")
    ensure_auto_post()


@tasks.loop(seconds=config.AUTO_POST_INTERVAL)
async def auto_post():
    try:
        if is_maintenance_mode():
            return
        channel = bot.get_channel(config.AUTO_POST_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(config.AUTO_POST_CHANNEL_ID)
        await channel.send(config.AUTO_POST_MESSAGE)
    except discord.Forbidden:
        print("[auto_post] missing permission — will retry next interval")
    except discord.HTTPException as exc:
        print(f"[auto_post] HTTP error ({exc.status}): {exc}")
    except Exception as exc:
        print(f"[auto_post] error: {exc}")


@auto_post.before_loop
async def before_auto_post():
    await bot.wait_until_ready()


@auto_post.error
async def auto_post_error(exc):
    print(f"[auto_post] task error: {exc}")


@tasks.loop(minutes=2)
async def watchdog():
    if not bot.is_ready():
        return
    if not auto_post.is_running():
        print("[watchdog] auto_post stopped — restarting")
        ensure_auto_post()


@watchdog.before_loop
async def before_watchdog():
    await bot.wait_until_ready()


@bot.event
async def on_guild_channel_create(channel):
    if isinstance(channel, discord.TextChannel) and was_bot_added_to_channel(channel, bot.user):
        await handle_bot_added_to_channel(bot, channel)


@bot.event
async def on_guild_channel_update(before, after):
    if isinstance(after, discord.TextChannel) and was_bot_added_to_channel(after, bot.user, before):
        await handle_bot_added_to_channel(bot, after)


@bot.event
async def on_guild_channel_delete(channel):
    clear_ticket_session(channel.id)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    try:
        await _handle_message(message)
    except Exception as exc:
        print(f"[on_message] error in #{getattr(message.channel, 'name', '?')}: {exc}")


async def _handle_message(message: discord.Message):
    if isinstance(message.channel, discord.DMChannel):
        content = message.content.strip().lower()

        if content == "!help":
            await message.reply(build_dm_help_text(message.author.id))
            return
        if content == "!gamemodes":
            await message.reply(build_dm_gamemodes_text())
            return
        if content == "!housebal":
            await message.reply(await get_house_balance_text())
            return

        if message.content == "!panel" and message.author.id == config.ADMIN_USER_ID:
            stats = await get_stats()
            await message.reply(
                f"**📊 Admin Panel**\n\n"
                f"**Daily:** Wagered ${stats.get('daily', {}).get('wagered', 0)}\n"
                f"**Weekly:** Wagered ${stats.get('weekly', {}).get('wagered', 0)}\n"
                f"**Monthly:** Wagered ${stats.get('monthly', {}).get('wagered', 0)}\n"
                f"**All Time:** Wagered ${stats.get('all_time', {}).get('wagered', 0)}\n\n"
                f"Most played: Dice\n"
                f"Unique users: {len(stats.get('unique_users', []))}\n"
                f"House Balance: Loading..."
            )
            return
        if message.content == "!wallet" and message.author.id == config.ADMIN_USER_ID:
            wallets = await get_wallets()
            lines = ["**Wallets:**"]
            for w in wallets.get("wallets", []):
                lines.append(f"**{w['currency'].upper()}:** `{w['address']}` | Balance: {w.get('balance', 0)}")
            await message.reply("\n".join(lines))
            return
        if message.content.strip().lower() == "!toggle maintenance" and message.author.id == config.ADMIN_USER_ID:
            enabled = toggle_maintenance()
            status = "enabled" if enabled else "disabled"
            await message.reply(f"Maintenance mode is {status}.")
            return
        if message.content.strip().lower() == "!toggle testing" and message.author.id == config.ADMIN_USER_ID:
            enabled = toggle_testing()
            status = "enabled" if enabled else "disabled"
            await message.reply(f"Testing mode is {status}.")
            return

    if not isinstance(message.channel, discord.TextChannel):
        return
    if not should_process_channel(message.channel, message, bot.user):
        return

    if is_ticket_channel(message.channel):
        if await handle_ticket_command(message, bot.user, bot):
            return

    channel_id = message.channel.id
    form = get_form(channel_id)

    if is_roll_command(message.content) and form and form.get("game_state", {}).get("game_type") == "dice":
        if message.author.id == form["ticket_user_id"]:
            await handle_user_roll(message, form, bot.user)
        return

    if form and "game_state" in form:
        state = form["game_state"]
        if state.get("game_type") == "dice" and message.author.bot and (
            state.get("waiting_for_embed")
            or state.get("pending_bot_total") is not None
            or state.get("awaiting_user_after_bot")
        ):
            await handle_da_hood_message(message, form, bot.user)
            return
        if message.author.id == DA_HOOD_BOT_ID:
            await handle_da_hood_message(message, form, bot.user)
            return

    form = get_form(channel_id)
    if form and not form.get("game_state") and not form.get("waiting_for_rerun") and not form.get("waiting_for_confirm"):
        await handle_form_step(message, form, bot.user)

    if channel_id not in active_forms:
        await asyncio.sleep(1)
        await start_ticket_form(message.channel, bot.user, bot)
        return

    await handle_global_listeners(message, bot.user, start_game)


if __name__ == "__main__":
    token = config.DISCORD_TOKEN
    if not token:
        token = input("Paste your Discord User Token: ")
    bot.run(token)
