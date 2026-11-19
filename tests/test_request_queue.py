import asyncio

import pytest
from muse_glimmer.runtime import QueuedRequests


def scope(path="/v1/chat/completions"):
    return {"type": "http", "method": "POST", "path": path, "headers": []}


async def receive():
    return {"type": "http.request", "body": b"{}", "more_body": False}


async def send(message):
    pass


def test_queue_holds_capacity_through_streaming_and_cleanup():
    async def check():
        started = [asyncio.Event() for _ in range(6)]
        finish_body = [asyncio.Event() for _ in range(6)]
        finish_cleanup = [asyncio.Event() for _ in range(6)]
        bypassed = asyncio.Event()
        active = 0
        peak = 0

        async def app(request, receive, send):
            nonlocal active, peak
            if request["path"] != "/v1/chat/completions":
                bypassed.set()
                return
            index = request["test_index"]
            active += 1
            peak = max(peak, active)
            try:
                await send({"type": "http.response.start", "status": 200, "headers": []})
                started[index].set()
                await finish_body[index].wait()
                await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})
                await finish_cleanup[index].wait()
            finally:
                active -= 1

        queued = QueuedRequests(app, 2)
        tasks = [
            asyncio.create_task(queued({**scope(), "test_index": i}, receive, send))
            for i in range(6)
        ]
        try:
            await started[0].wait()
            await started[1].wait()
            assert not any(event.is_set() for event in started[2:])
            await queued(scope("/health"), receive, send)
            assert bypassed.is_set()
            finish_body[0].set()
            await asyncio.sleep(0)
            assert not started[2].is_set()
            finish_cleanup[0].set()
            await started[2].wait()
            for event in [*finish_body, *finish_cleanup]:
                event.set()
            await asyncio.gather(*tasks)
            assert peak == 2
            assert all(event.is_set() for event in started)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(asyncio.wait_for(check(), 5))


@pytest.mark.parametrize("failure", ["cancel", "error"])
def test_cancelled_waiter_and_failed_active_request_do_not_leak_capacity(failure):
    async def check():
        started = asyncio.Event()
        release = asyncio.Event()
        completed = []

        async def app(request, receive, send):
            if request["test_index"] == 0:
                started.set()
                await release.wait()
                raise RuntimeError("stream failed")
            completed.append(request["test_index"])

        queued = QueuedRequests(app, 1)
        first = asyncio.create_task(queued({**scope(), "test_index": 0}, receive, send))
        await started.wait()
        cancelled = asyncio.create_task(queued({**scope(), "test_index": 1}, receive, send))
        last = asyncio.create_task(queued({**scope(), "test_index": 2}, receive, send))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        if failure == "cancel":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            release.set()
            with pytest.raises(RuntimeError, match="stream failed"):
                await first
        await last
        assert completed == [2]

    asyncio.run(asyncio.wait_for(check(), 5))
