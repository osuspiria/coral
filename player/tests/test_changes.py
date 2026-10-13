from __future__ import annotations

import asyncio
import random

from coral_player.changes import Backoff, PollingChangeSource


def test_backoff_grows_with_jitter_and_caps():
    backoff = Backoff(5, 900, random.Random(1))
    ceilings = []
    for _ in range(12):
        backoff.fail()
        d = backoff.delay()
        ceiling = min(900, 5 * 2 ** (backoff.failures - 1))
        assert ceiling / 2 <= d <= ceiling
        ceilings.append(ceiling)
    assert ceilings[0] == 5 and ceilings[-1] == 900
    backoff.reset()
    assert backoff.failures == 0


def test_backoff_is_jittered():
    backoff = Backoff(5, 900, random.Random(2))
    for _ in range(5):
        backoff.fail()
    assert len({round(backoff.delay(), 6) for _ in range(20)}) > 1


def test_poll_interval_has_jitter():
    source = PollingChangeSource(60, 900, Backoff(5, 900), rng=random.Random(3))
    delays = {source.next_delay() for _ in range(50)}
    assert all(54 <= d <= 66 for d in delays) and len(delays) > 1


def make(now):
    return PollingChangeSource(
        0.01, 900, Backoff(0.01, 0.02), monotonic=lambda: now[0], rng=random.Random(0)
    )


def test_first_trigger_is_immediate_and_full():
    async def scenario():
        now = [1000.0]
        source = make(now)
        first = await source.next_trigger()
        assert (first.full, first.reason) == (True, "startup")
        source.sync_finished(True)
        second = await source.next_trigger()
        assert (second.full, second.reason) == (False, "poll")
        source.sync_finished(True)
        now[0] += 901
        third = await source.next_trigger()
        assert third.full

    asyncio.run(scenario())


def test_failed_full_check_is_retried_as_full_with_backoff():
    async def scenario():
        now = [0.0]
        source = make(now)
        await source.next_trigger()
        source.sync_finished(False)
        assert source.backoff.failures == 1
        retry = await source.next_trigger()
        assert (retry.full, retry.reason) == (True, "backoff retry")
        source.sync_finished(True)
        assert source.backoff.failures == 0

    asyncio.run(scenario())


def test_wake_triggers_immediately():
    async def scenario():
        source = PollingChangeSource(3600, 900, Backoff(5, 900))
        await source.next_trigger()
        source.sync_finished(True)
        asyncio.get_running_loop().call_later(0.01, source.wake)
        trigger = await asyncio.wait_for(source.next_trigger(), timeout=2)
        assert trigger.reason == "wake"

    asyncio.run(scenario())
