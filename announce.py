"""Admin mass-DM announcements (including message requests)."""

import io

import discord

from message_queue import send_channel


async def resolve_reference_message(message):
    """Return the message being replied to, or None."""
    ref = message.reference
    if ref is None or ref.message_id is None:
        return None
    source = getattr(ref, "resolved", None)
    if isinstance(source, discord.Message):
        return source
    try:
        return await message.channel.fetch_message(ref.message_id)
    except Exception:
        return None


def _embed_copies(embeds):
    copies = []
    for emb in embeds or []:
        try:
            copies.append(discord.Embed.from_dict(emb.to_dict()))
        except Exception:
            pass
    return copies[:10]


async def _attachment_blobs(attachments):
    blobs = []
    for att in attachments or []:
        try:
            blobs.append((att.filename, await att.read()))
        except Exception as exc:
            print(f"[announce] failed to read attachment {att.filename}: {exc}")
    return blobs


def _files_from_blobs(blobs):
    return [discord.File(io.BytesIO(data), filename=name) for name, data in blobs]


async def _prepare_dm_channel(channel):
    """Accept pending message requests so the channel can receive sends."""
    try:
        if hasattr(channel, "is_message_request") and channel.is_message_request():
            if hasattr(channel, "is_accepted") and not channel.is_accepted():
                await channel.accept()
    except Exception as exc:
        print(f"[announce] accept failed for {channel.id}: {exc}")


async def broadcast_announcement(bot, source_message):
    """
    Send source_message content/embeds/attachments to every DMChannel,
    including message-request DMs. Returns (ok_count, fail_count, total).
    """
    content = source_message.content or None
    embeds = _embed_copies(source_message.embeds)
    blobs = await _attachment_blobs(source_message.attachments)

    if not content and not embeds and not blobs:
        return 0, 0, 0

    try:
        channels = await bot.fetch_private_channels()
    except Exception as exc:
        print(f"[announce] fetch_private_channels failed: {exc}")
        channels = list(bot.private_channels)

    targets = []
    for channel in channels:
        if not isinstance(channel, discord.DMChannel):
            continue
        recipient = getattr(channel, "recipient", None)
        if recipient is not None and bot.user is not None and recipient.id == bot.user.id:
            continue
        targets.append(channel)

    ok = 0
    fail = 0
    for channel in targets:
        try:
            await _prepare_dm_channel(channel)
            kwargs = {}
            if embeds:
                kwargs["embeds"] = _embed_copies(source_message.embeds)
            if blobs:
                kwargs["files"] = _files_from_blobs(blobs)
            # Discord requires non-empty content or embeds/files
            send_content = content if content else None
            await send_channel(channel, send_content, **kwargs)
            ok += 1
        except Exception as exc:
            fail += 1
            print(f"[announce] send failed to {channel.id}: {exc}")

    return ok, fail, len(targets)
