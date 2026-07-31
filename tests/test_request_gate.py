from __future__ import annotations

import asyncio
import unittest

from canvasforge.contracts import CanvasForgeError, ErrorCode
from canvasforge.rate_limit import RequestGate


class RequestGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_conversation_is_immediately_busy_with_active_task_id(
        self,
    ) -> None:
        gate = RequestGate()
        first = await gate.acquire(
            "first",
            conversation_key="chat-a:conversation-1",
            task_id="task-first",
            max_concurrent=3,
        )
        try:
            self.assertTrue(await gate.is_busy())
            self.assertTrue(await gate.is_busy("chat-a:conversation-1"))
            self.assertEqual(1, await gate.active_count())
            self.assertEqual(
                "task-first",
                await gate.active_task_id("chat-a:conversation-1"),
            )

            with self.assertRaises(CanvasForgeError) as caught:
                await gate.acquire(
                    "administrator",
                    conversation_key="chat-a:conversation-1",
                    task_id="task-second",
                    max_concurrent=3,
                    is_admin=True,
                    cooldown_seconds=0,
                )
            self.assertEqual(ErrorCode.BUSY, caught.exception.code)
            self.assertEqual("task-first", caught.exception.task_id)
            self.assertEqual(1, await gate.active_count())
        finally:
            await first.release()

        self.assertFalse(await gate.is_busy())
        self.assertIsNone(await gate.active_task_id("chat-a:conversation-1"))

    async def test_different_conversations_share_global_capacity(self) -> None:
        gate = RequestGate()
        leases = [
            await gate.acquire(
                f"user-{index}",
                conversation_key=f"conversation-{index}",
                task_id=f"task-{index}",
                max_concurrent=3,
                cooldown_seconds=0,
            )
            for index in range(3)
        ]
        try:
            self.assertEqual(3, await gate.active_count())
            with self.assertRaises(CanvasForgeError) as caught:
                await gate.acquire(
                    "fourth-user",
                    conversation_key="conversation-4",
                    task_id="task-4",
                    max_concurrent=3,
                    is_admin=True,
                    cooldown_seconds=0,
                )
            self.assertEqual(ErrorCode.BUSY, caught.exception.code)
            self.assertIsNone(caught.exception.task_id)
            self.assertIsNone(await gate.active_task_id("conversation-4"))
        finally:
            await asyncio.gather(*(lease.release() for lease in leases))

    async def test_release_frees_slots_without_starting_cooldown(self) -> None:
        gate = RequestGate()
        lease = await gate.acquire(
            "same-user",
            conversation_key="conversation-a",
            task_id="task-a",
            cooldown_seconds=300,
        )
        await lease.release()

        retry = await gate.acquire(
            "same-user",
            conversation_key="conversation-b",
            task_id="task-b",
            cooldown_seconds=300,
        )
        await retry.release()

    async def test_success_cooldown_follows_user_across_conversations(self) -> None:
        now = [100.0]
        gate = RequestGate(clock=lambda: now[0])
        lease = await gate.acquire(
            "same-user",
            conversation_key="conversation-a",
            task_id="task-a",
            cooldown_seconds=300,
        )
        await lease.commit()

        with self.assertRaises(CanvasForgeError) as caught:
            await gate.acquire(
                "same-user",
                conversation_key="conversation-b",
                task_id="task-b",
                cooldown_seconds=300,
            )
        self.assertEqual(ErrorCode.COOLDOWN, caught.exception.code)

        # Administrators bypass only cooldown and still occupy a normal slot.
        admin = await gate.acquire(
            "same-user",
            conversation_key="conversation-b",
            task_id="task-admin",
            is_admin=True,
            cooldown_seconds=300,
        )
        await admin.release()

        # Other users are unaffected by this user's cooldown.
        other = await gate.acquire(
            "other-user",
            conversation_key="conversation-c",
            task_id="task-other",
            cooldown_seconds=300,
        )
        await other.release()

        now[0] += 301
        after_cooldown = await gate.acquire(
            "same-user",
            conversation_key="conversation-d",
            task_id="task-after",
            cooldown_seconds=300,
        )
        await after_cooldown.release()

    async def test_same_conversation_contention_is_atomic(self) -> None:
        gate = RequestGate()

        async def contend(index: int):
            try:
                lease = await gate.acquire(
                    f"user-{index}",
                    conversation_key="one-conversation",
                    task_id=f"task-{index}",
                    max_concurrent=32,
                    cooldown_seconds=0,
                )
            except CanvasForgeError as exc:
                return exc
            return lease

        results = await asyncio.gather(*(contend(index) for index in range(12)))
        leases = [result for result in results if not isinstance(result, Exception)]
        errors = [result for result in results if isinstance(result, CanvasForgeError)]
        self.assertEqual(1, len(leases))
        self.assertEqual(11, len(errors))
        self.assertTrue(all(error.code is ErrorCode.BUSY for error in errors))
        self.assertTrue(
            all(error.task_id == leases[0].task_id for error in errors),
        )
        await leases[0].release()

    async def test_cancelled_owner_releases_without_cooldown(self) -> None:
        gate = RequestGate()
        entered = asyncio.Event()

        async def owner() -> None:
            lease = await gate.acquire(
                "cancelled-user",
                conversation_key="cancelled-conversation",
                task_id="cancelled-task",
                cooldown_seconds=300,
            )
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

        self.assertFalse(await gate.is_busy("cancelled-conversation"))
        retry = await gate.acquire(
            "cancelled-user",
            conversation_key="new-conversation",
            task_id="retry-task",
            cooldown_seconds=300,
        )
        await retry.release()

    async def test_rejects_invalid_global_limit(self) -> None:
        gate = RequestGate()
        for invalid in (0, 33):
            with self.subTest(max_concurrent=invalid):
                with self.assertRaises(ValueError):
                    await gate.acquire(
                        "user",
                        conversation_key="conversation",
                        task_id="task",
                        max_concurrent=invalid,
                    )


if __name__ == "__main__":
    unittest.main()
