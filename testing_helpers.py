import config
from state import active_forms, is_testing_mode


def get_active_game_form():
    for form in active_forms.values():
        if form.get("game_state"):
            return form
    return None


def is_testing_roll_channel(channel_id):
    return is_testing_mode() and channel_id == config.TESTING_CHANNEL_ID


async def get_roll_channel(bot, ticket_channel):
    if not is_testing_mode():
        return ticket_channel
    channel = bot.get_channel(config.TESTING_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(config.TESTING_CHANNEL_ID)
    return channel


async def send_game_message(bot, ticket_channel, content):
    if is_testing_mode():
        admin = bot.get_user(config.ADMIN_USER_ID)
        if admin is None:
            admin = await bot.fetch_user(config.ADMIN_USER_ID)
        await admin.send(content)
        return
    await ticket_channel.send(content)


async def get_ticket_channel(bot, form):
    channel = bot.get_channel(form["ticket_channel_id"])
    if channel is None:
        channel = await bot.fetch_channel(form["ticket_channel_id"])
    return channel
