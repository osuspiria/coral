from __future__ import annotations

import asyncio
import random

from coral_player.changes import Backoff, HeartbeatChangeSource


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


def make(now, full_interval=900.0):
    return HeartbeatChangeSource(full_interval, Backoff(0.01, 0.02), monotonic=lambda: now[0])


def test_first_trigger_is_immediate_and_full():
    async def scenario():
        source = make([1000.0])
        first = await source.next_trigger()
        assert (first.full, first.reason) == (True, "startup")

    asyncio.run(scenario())


def test_new_hash_from_heartbeat_wakes_once():
    async def scenario():
        now = [1000.0]
        source = make(now)
        await source.next_trigger()
        source.sync_finished(True)

        source.notify_hash("new", seen="old")
        trigger = await asyncio.wait_for(source.next_trigger(), 1)
        assert (trigger.full, trigger.reason) == (False, "heartbeat")
        source.sync_finished(False)

        # The same hash on the next heartbeats does not wake again: backoff retries.
        source.notify_hash("new", seen="old")
        assert not source._wake.is_set()
        retry = await asyncio.wait_for(source.next_trigger(), 1)
        assert retry.reason == "backoff retry"

    asyncio.run(scenario())


def test_hash_already_seen_does_not_wake():
    source = make([0.0])
    source.notify_hash("same", seen="same")
    source.notify_hash(None, seen="x")
    assert not source._wake.is_set()


def test_sync_now_forces_a_full_check():
    async def scenario():
        source = make([1000.0])
        await source.next_trigger()
        source.sync_finished(True)
        asyncio.get_running_loop().call_later(0.01, source.wake, True)
        trigger = await asyncio.wait_for(source.next_trigger(), 1)
        assert (trigger.full, trigger.reason) == (True, "heartbeat")

    asyncio.run(scenario())


def test_periodic_full_check_without_heartbeat_news():
    async def scenario():
        now = [1000.0]
        source = make(now, full_interval=900.0)
        await source.next_trigger()
        source.sync_finished(True)
        now[0] += 900  # the next full check is due
        trigger = await asyncio.wait_for(source.next_trigger(), 1)
        assert (trigger.full, trigger.reason) == (True, "periodic full check")

    asyncio.run(scenario())


def test_failed_full_check_is_retried_as_full():
    async def scenario():
        source = make([0.0])
        await source.next_trigger()
        source.sync_finished(False)
        retry = await asyncio.wait_for(source.next_trigger(), 1)
        assert (retry.full, retry.reason) == (True, "backoff retry")
        source.sync_finished(True)
        assert source.backoff.failures == 0

    asyncio.run(scenario())
