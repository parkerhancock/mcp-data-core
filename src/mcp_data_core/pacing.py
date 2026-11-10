"""Per-host request pacing shared by every client in the process."""

from __future__ import annotations

import asyncio
import time
import weakref

import httpx2


class HostPacer:
    """Space requests to the same host at least ``interval`` seconds apart.

    Callers queue on a per-host lock and hold it while they sleep, and the
    next slot is measured from when the previous caller actually woke. If the
    event loop stalls, waiters can't wake together and fire in a burst; they
    still leave one at a time, ``interval`` apart. A cancelled waiter releases
    the lock without using a slot. Slot times are shared across event loops;
    the locks are per loop because an ``asyncio.Lock`` can't be shared
    between loops.
    """

    def __init__(self) -> None:
        self._next_slot: dict[str, float] = {}
        self._locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
        ] = weakref.WeakKeyDictionary()

    def _lock(self, host: str) -> asyncio.Lock:
        per_loop = self._locks.setdefault(asyncio.get_running_loop(), {})
        lock = per_loop.get(host)
        if lock is None:
            lock = per_loop[host] = asyncio.Lock()
        return lock

    async def wait(self, host: str, interval: float) -> None:
        """Sleep until this caller's slot for ``host`` arrives."""
        if interval <= 0:
            return
        async with self._lock(host):
            # Loop because asyncio may fire a timer up to its clock resolution early.
            while (delay := self._next_slot.get(host, 0.0) - time.monotonic()) > 0:
                await asyncio.sleep(delay)
            self._next_slot[host] = time.monotonic() + interval


default_pacer = HostPacer()


class PacedTransport(httpx2.AsyncBaseTransport):
    """Wait for the host's pacing slot before each request reaches the network.

    Sits below the HTTP cache, so cache hits return without waiting.
    """

    def __init__(
        self,
        next_transport: httpx2.AsyncBaseTransport,
        interval: float,
        pacer: HostPacer = default_pacer,
    ) -> None:
        self._next = next_transport
        self._interval = interval
        self._pacer = pacer

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await self._pacer.wait(request.url.host, self._interval)
        return await self._next.handle_async_request(request)

    async def aclose(self) -> None:
        await self._next.aclose()


__all__ = ["HostPacer", "PacedTransport", "default_pacer"]
