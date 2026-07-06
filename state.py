active_forms = {}
ticket_channels = set()
maintenance_mode = False
maintenance_notified_channels = set()
testing_mode = False


def is_maintenance_mode():
    return maintenance_mode


def is_testing_mode():
    return testing_mode


def toggle_maintenance():
    global maintenance_mode
    maintenance_mode = not maintenance_mode
    if not maintenance_mode:
        maintenance_notified_channels.clear()
    return maintenance_mode


def toggle_testing():
    global testing_mode
    testing_mode = not testing_mode
    return testing_mode


async def notify_maintenance(channel):
    if channel.id in maintenance_notified_channels:
        return
    maintenance_notified_channels.add(channel.id)
    try:
        await channel.send("🚧 **MAINTENANCE MODE IS ENABLED** 🚧")
    except Exception:
        maintenance_notified_channels.discard(channel.id)


def register_ticket_channel(channel_id):
    if channel_id in ticket_channels:
        return False
    ticket_channels.add(channel_id)
    return True


def unregister_ticket_channel(channel_id):
    ticket_channels.discard(channel_id)


def is_ticket_channel(channel):
    if channel.id in ticket_channels or channel.id in active_forms:
        return True
    return "ticket" in channel.name.lower()


def get_form(channel_id):
    return active_forms.get(channel_id)


def cancel_rerun_timeout(form):
    task = form.pop("rerun_timeout_task", None)
    if task and not task.done():
        task.cancel()


def finish_form(channel, form):
    cancel_rerun_timeout(form)
    active_forms.pop(channel.id, None)
    unregister_ticket_channel(channel.id)
