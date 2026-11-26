import asyncio
import time

import pytest

from backend.config import Settings
from backend.database import Store
from backend.runtime import Runtime
from backend.service import HubService


@pytest.fixture
def runtime(tmp_path):
    store = Store(f'sqlite:///{tmp_path}/hub.db')
    store.initialize()
    return Runtime(HubService(store, Settings(enable_ingestion=False)))


@pytest.mark.asyncio
async def test_repeat_exits_when_a_library_swallows_cancellation(runtime):
    # Observed with httpx/anyio: a request returned normally although the task was being cancelled.
    async def swallowing():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return

    runtime.tasks = [asyncio.create_task(runtime.repeat(swallowing, 1))]
    await asyncio.sleep(0.05)
    started = time.monotonic()
    await asyncio.wait_for(runtime.stop(), 5)  # a regression fails here instead of hanging the suite
    assert runtime.tasks[0].done() and time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_stop_is_bounded_when_a_task_ignores_cancellation(runtime, monkeypatch):
    monkeypatch.setattr('backend.runtime.SHUTDOWN_TIMEOUT_SECONDS', 0.3)

    release = asyncio.Event()

    async def stubborn():
        while not release.is_set():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                continue

    task = asyncio.create_task(stubborn())
    runtime.tasks = [task]
    await asyncio.sleep(0.05)
    started = time.monotonic()
    await asyncio.wait_for(runtime.stop(), 5)
    assert time.monotonic() - started < 2 and not task.done()
    release.set()
    task.cancel()
    await asyncio.wait([task], timeout=1)
