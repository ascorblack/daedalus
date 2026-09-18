"""Waiting for something to have happened, instead of waiting for a length of time.

Every test that hands work to a task, a thread or a background loop has to let it get there before
it looks at the result. A ``sleep`` says "by now, surely" — which is a statement about the host, not
about the code: on a machine under load the scheduler can spend the whole of a fifty-millisecond
sleep without running the task it was meant to cover, and the test fails for a reason that has
nothing to do with what it is testing.

So the wait is on the thing itself. :func:`until` polls a predicate the code under test moves, with
a bound long enough that only a hang can reach it — a bound is not a duration the test depends on,
it is the difference between a failure and a suite that never finishes. Load makes these tests
slower and never makes them fail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

SETTLE = 60.0
"""The bound on every wait here. Reaching it means the thing never happened, which is a failure to
report rather than a slow host to accommodate."""


async def until(predicate: Callable[[], Any], what: str = "the condition", *, timeout: float = SETTLE) -> None:
    """Hold until ``predicate()`` is true. The message names what never happened."""
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.001)
    except TimeoutError:
        raise AssertionError(f"waited {timeout:.0f}s and {what} never happened") from None


async def until_await(check: Callable[[], Any], what: str = "the condition", *, timeout: float = SETTLE) -> None:
    """:func:`until` for a condition that has to be awaited — a query, a store read."""
    try:
        async with asyncio.timeout(timeout):
            while not await check():
                await asyncio.sleep(0.001)
    except TimeoutError:
        raise AssertionError(f"waited {timeout:.0f}s and {what} never happened") from None


async def grows_to(seq: Sequence[Any], size: int, what: str = "the list", *, timeout: float = SETTLE) -> None:
    """Hold until ``seq`` holds at least ``size`` items — the commonest shape of the wait above."""
    await until(lambda: len(seq) >= size, f"{what} reached {size} (it has {len(seq)})", timeout=timeout)


__all__ = ["SETTLE", "grows_to", "until", "until_await"]
