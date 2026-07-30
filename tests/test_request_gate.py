from __future__ import annotations

import asyncio
import unittest

from canvasforge.contracts import CanvasForgeError, ErrorCode
from canvasforge.rate_limit import RequestGate


class RequestGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_slot_is_immediate_and_admin_does_not_bypass_it(self) -> None:
        gate = RequestGate()
        first = await gate.acquire("first", cooldown_seconds=300)
        try:
            with self.assertRaises(CanvasForgeError) as caught:
                await gate.acquire(
                    "administrator",
                    is_admin=True,
                    cooldown_seconds=0,
                )
            self.assertEqual(ErrorCode.BUSY, caught.exception.code)
            self.assertEqual(
                "CanvasForge 一次只能处理一个生图任务；请等待当前任务完成后再试。",
                str(caught.exception),
            )
        finally:
            await first.release()

    async def test_release_frees_slot_without_starting_cooldown(self) -> None:
        now = [100.0]
        gate = RequestGate(clock=lambda: now[0])
        lease = await gate.acquire("same-user", cooldown_seconds=300)
        await lease.release()

        retry = await gate.acquire("same-user", cooldown_seconds=300)
        await retry.release()

    async def test_only_successful_commit_starts_cooldown(self) -> None:
        now = [100.0]
        gate = RequestGate(clock=lambda: now[0])
        lease = await gate.acquire("same-user", cooldown_seconds=300)
        await lease.commit()

        with self.assertRaises(CanvasForgeError) as caught:
            await gate.acquire("same-user", cooldown_seconds=300)
        self.assertEqual(ErrorCode.COOLDOWN, caught.exception.code)

        # A different user is unaffected by the per-user cooldown.
        other = await gate.acquire("other-user", cooldown_seconds=300)
        await other.release()

        now[0] += 301
        after_cooldown = await gate.acquire("same-user", cooldown_seconds=300)
        await after_cooldown.release()

    async def test_cancelled_owner_can_release_and_a_later_request_can_run(self) -> None:
        gate = RequestGate()
        entered = asyncio.Event()

        async def owner() -> None:
            lease = await gate.acquire("cancelled-user", cooldown_seconds=300)
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await lease.release()

        task = asyncio.create_task(owner())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        retry = await gate.acquire("cancelled-user", cooldown_seconds=300)
        await retry.release()


if __name__ == "__main__":
    unittest.main()
