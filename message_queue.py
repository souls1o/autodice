import asyncio

# Discord allows ~5 msgs / 5s per channel; stay under 4/sec globally for the selfbot.
_SEND_INTERVAL = 0.2

_queue = asyncio.Queue()
_worker_task = None


async def _send_worker():
    while True:
        send_fn = await _queue.get()
        try:
            await send_fn()
        except Exception as exc:
            print(f"[send_queue] send failed: {exc}")
        finally:
            _queue.task_done()
        await asyncio.sleep(_SEND_INTERVAL)


def start_send_worker():
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_send_worker())


async def _enqueue(send_fn):
    start_send_worker()
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    async def _run():
        try:
            result = await send_fn()
            if not future.done():
                future.set_result(result)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)

    await _queue.put(_run)
    return await future


async def send_channel(channel, content, **kwargs):
    return await _enqueue(lambda: channel.send(content, **kwargs))


async def reply_message(message, content, **kwargs):
    return await _enqueue(lambda: message.reply(content, **kwargs))


async def send_user(user, content, **kwargs):
    return await _enqueue(lambda: user.send(content, **kwargs))
