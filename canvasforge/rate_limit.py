"""In-memory, non-queued request gate for paid image operations."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable

from .contracts import CanvasForgeError, ErrorCode


class RequestLease:
    """Exclusive request lease returned by :class:`RequestGate`.

    Call ``commit`` only after the generated image has been delivered to QQ.
    Any exception path should call ``release`` (the async context manager does
    this automatically), which frees the global slot without adding cooldown.
    """

    def __init__(
        self,
        gate: "RequestGate",
        token: object,
        user_id: str,
    ) -> None:
        self._gate = gate
        self._token = token
        self.user_id = user_id
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    async def commit(self) -> None:
        await self._finish(committed=True)

    async def release(self) -> None:
        await self._finish(committed=False)

    async def _finish(self, *, committed: bool) -> None:
        """Finalize the lease before propagating caller cancellation."""

        if self._finished:
            return

        task = asyncio.create_task(
            self._gate._finish(
                self._token,
                self.user_id,
                committed=committed,
            ),
        )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
            except Exception:
                break

        try:
            task.result()
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
        self._finished = True
        if cancellation is not None:
            raise cancellation

    async def __aenter__(self) -> "RequestLease":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if not self._finished:
            await self.release()


class RequestGate:
    """One global in-flight request plus per-user success cooldowns."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._state_lock = asyncio.Lock()
        self._active_token: object | None = None
        self._last_success: dict[str, float] = {}

    async def acquire(
        self,
        user_id: str,
        *,
        is_admin: bool = False,
        cooldown_seconds: float = 300.0,
    ) -> RequestLease:
        """Acquire immediately or raise ``BUSY``/``COOLDOWN``.

        Administrators bypass only the per-user cooldown. They still contend
        for the same single global request slot.
        """

        cooldown = max(0.0, float(cooldown_seconds))
        normalized_user_id = str(user_id)
        async with self._state_lock:
            if self._active_token is not None:
                raise CanvasForgeError(ErrorCode.BUSY)

            now = self._clock()
            if not is_admin and cooldown > 0:
                last_success = self._last_success.get(normalized_user_id)
                if last_success is not None:
                    remaining = cooldown - (now - last_success)
                    if remaining > 0:
                        retry_after = max(1, math.ceil(remaining))
                        raise CanvasForgeError(
                            ErrorCode.COOLDOWN,
                            f"生成冷却尚未结束，请在 {retry_after} 秒后再试。",
                            retry_after=retry_after,
                        )

            token = object()
            self._active_token = token
            return RequestLease(self, token, normalized_user_id)

    async def _finish(
        self,
        token: object,
        user_id: str,
        *,
        committed: bool,
    ) -> None:
        async with self._state_lock:
            if self._active_token is not token:
                return
            if committed:
                self._last_success[user_id] = self._clock()
            self._active_token = None

    async def clear_cooldowns(self) -> None:
        async with self._state_lock:
            self._last_success.clear()

    async def is_busy(self) -> bool:
        async with self._state_lock:
            return self._active_token is not None
