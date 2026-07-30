from __future__ import annotations

import asyncio
import threading
import unittest

from tests.astrbot_stubs import install_astrbot_stubs

install_astrbot_stubs()

from canvasforge.avatar import AvatarResolver
from canvasforge.reference import _call_action as reference_call_action


class CallActionCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_call_action_runs_off_the_event_loop(self) -> None:
        loop_thread = threading.get_ident()

        for name, invoke in (
            ("reference", reference_call_action),
            ("avatar", AvatarResolver._call_action),
        ):
            with self.subTest(module=name):
                started = threading.Event()
                release = threading.Event()
                worker_thread: list[int] = []

                class Client:
                    def call_action(self, **_values):
                        worker_thread.append(threading.get_ident())
                        started.set()
                        release.wait(timeout=2)
                        return {"ok": True}

                task = asyncio.create_task(invoke(Client(), "get_msg", message_id=1))
                await asyncio.wait_for(
                    asyncio.to_thread(started.wait, 1),
                    timeout=1.5,
                )

                # If call_action were executing on the loop, this heartbeat
                # could not run until the worker was released.
                heartbeat = asyncio.create_task(asyncio.sleep(0))
                await asyncio.wait_for(heartbeat, timeout=0.2)
                release.set()
                self.assertEqual({"ok": True}, await task)
                self.assertNotEqual(loop_thread, worker_thread[0])

    async def test_async_call_action_is_awaited_directly(self) -> None:
        loop_thread = threading.get_ident()

        for name, invoke in (
            ("reference", reference_call_action),
            ("avatar", AvatarResolver._call_action),
        ):
            with self.subTest(module=name):
                called_from: list[int] = []

                class Client:
                    async def call_action(self, **values):
                        called_from.append(threading.get_ident())
                        await asyncio.sleep(0)
                        return values

                result = await invoke(Client(), "get_login_info")
                self.assertEqual("get_login_info", result["action"])
                self.assertEqual([loop_thread], called_from)

    async def test_sync_wrapper_returning_awaitable_is_awaited(self) -> None:
        class Client:
            def call_action(self, **values):
                async def finish():
                    await asyncio.sleep(0)
                    return values["action"]

                return finish()

        self.assertEqual(
            "get_group_info",
            await reference_call_action(Client(), "get_group_info"),
        )
        self.assertEqual(
            "get_group_info",
            await AvatarResolver._call_action(Client(), "get_group_info"),
        )


if __name__ == "__main__":
    unittest.main()
