"""In-memory, non-queued request gate for paid image operations."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from .contracts import CanvasForgeError, ErrorCode


class RequestLease:
    """One conversation-scoped lease returned by :class:`RequestGate`.

    Call ``commit`` only after the generated image has been delivered to QQ.
    Any exception path should call ``release`` (the async context manager does
    this automatically), which frees the conversation and global slots without
    adding cooldown.
    """

    def __init__(
        self,
        gate: "RequestGate",
        token: object,
        user_id: str,
        conversation_key: str,
        task_id: str,
    ) -> None:
        self._gate = gate
        self._token = token
        self.user_id = user_id
        self.conversation_key = conversation_key
        self.task_id = task_id
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
                self.conversation_key,
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


@dataclass(frozen=True, slots=True)
class _ActiveRequest:
    token: object
    task_id: str


class RequestGate:
    """Immediate, non-queued admission by conversation and global capacity."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._state_lock = asyncio.Lock()
        self._active: dict[str, _ActiveRequest] = {}
        self._last_success: dict[str, float] = {}

    async def acquire(
        self,
        user_id: str,
        *,
        conversation_key: str,
        task_id: str,
        max_concurrent: int = 3,
        is_admin: bool = False,
        cooldown_seconds: float = 300.0,
    ) -> RequestLease:
        """Acquire immediately or raise ``BUSY``/``COOLDOWN``.

        A conversation can own at most one request and all conversations share
        ``max_concurrent`` slots. Administrators bypass only the per-user
        cooldown; they still contend for both admission limits.
        """

        cooldown = max(0.0, float(cooldown_seconds))
        limit = int(max_concurrent)
        if not 1 <= limit <= 32:
            raise ValueError("max_concurrent must be between 1 and 32")

        normalized_user_id = str(user_id)
        normalized_conversation_key = str(conversation_key)
        normalized_task_id = str(task_id)
        async with self._state_lock:
            active = self._active.get(normalized_conversation_key)
            if active is not None:
                raise CanvasForgeError(
                    ErrorCode.BUSY,
                    "当前会话已有图片正在生成。",
                    task_id=active.task_id,
                )

            if len(self._active) >= limit:
                raise CanvasForgeError(
                    ErrorCode.BUSY,
                    "CanvasForge 当前并发生图任务已达上限。",
                )

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
            self._active[normalized_conversation_key] = _ActiveRequest(
                token=token,
                task_id=normalized_task_id,
            )
            return RequestLease(
                self,
                token,
                normalized_user_id,
                normalized_conversation_key,
                normalized_task_id,
            )

    async def _finish(
        self,
        token: object,
        user_id: str,
        conversation_key: str,
        *,
        committed: bool,
    ) -> None:
        async with self._state_lock:
            active = self._active.get(conversation_key)
            if active is None or active.token is not token:
                return
            if committed:
                self._last_success[user_id] = self._clock()
            del self._active[conversation_key]

    async def clear_cooldowns(self) -> None:
        async with self._state_lock:
            self._last_success.clear()

    async def active_task_id(self, conversation_key: str) -> str | None:
        async with self._state_lock:
            active = self._active.get(str(conversation_key))
            return None if active is None else active.task_id

    async def active_count(self) -> int:
        async with self._state_lock:
            return len(self._active)

    async def is_busy(self, conversation_key: str | None = None) -> bool:
        """Return whether any slot, or one conversation slot, is occupied."""

        async with self._state_lock:
            if conversation_key is None:
                return bool(self._active)
            return str(conversation_key) in self._active
