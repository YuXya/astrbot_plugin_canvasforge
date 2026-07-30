from __future__ import annotations

import asyncio
import contextlib
import unittest
from types import MappingProxyType
from unittest.mock import patch

from tests.plugin_loader import load_main_module


main = load_main_module()


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[str] = []

    def register_web_api(self, path, *_args) -> None:
        self.routes.append(path)


class FakeConfig(dict):
    async def save_config_async(self, values=None):
        if values:
            self.update(values)
        return True

    def save_config(self) -> None:
        pass


class FakeEvent:
    def __init__(
        self,
        raw_message: str = "",
        *,
        sender_id: str = "10001",
        send_fails: bool = False,
        image_send_fails: bool = False,
        plain_send_fails: bool = False,
    ) -> None:
        self.raw_message = raw_message
        self.sender_id = sender_id
        self.send_fails = send_fails
        self.image_send_fails = image_send_fails
        self.plain_send_fails = plain_send_fails
        self.sent: list[object] = []
        self.stopped = False

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def is_admin(self) -> bool:
        return False

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_session_id(self) -> str:
        return f"session-{self.sender_id}"

    def get_message_str(self) -> str:
        return self.raw_message

    def stop_event(self) -> None:
        self.stopped = True

    async def send(self, message) -> None:
        if self.send_fails:
            raise RuntimeError("simulated send failure")
        component = message.chain[0]
        if self.image_send_fails and isinstance(component, main.Comp.Image):
            raise RuntimeError("simulated image send failure")
        if self.plain_send_fails and isinstance(component, main.Comp.Plain):
            raise RuntimeError("simulated plain send failure")
        self.sent.append(message)

    @property
    def sent_text(self) -> list[str]:
        values: list[str] = []
        for message in self.sent:
            for component in getattr(message, "chain", ()):
                text = getattr(component, "text", None)
                if isinstance(text, str):
                    values.append(text)
        return values


class SlowProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0
        self.failure: Exception | None = None

    async def generate(self, _prompt, _options):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.failure is not None:
            raise self.failure
        return main.GeneratedImage(
            data=b"generated",
            mime_type="image/png",
            format="png",
            width=1,
            height=1,
        )

    async def edit(self, prompt, _references, options):
        return await self.generate(prompt, options)


class FakeProviderFactory:
    def __init__(self, provider: SlowProvider) -> None:
        self.provider = provider
        self.seen_api_key: str | None = None

    def create(self, config):
        self.seen_api_key = config.api_key
        return self.provider


class FakeReferenceResolver:
    async def has_direct_images(self, _event) -> bool:
        return False

    async def resolve(self, *_args, **_kwargs):
        return []


class FakeAvatarResolver:
    def plan(self, _event, _targets):
        return []

    async def download(self, *_args, **_kwargs):
        return []


class FakeSession:
    def __init__(self, provider: SlowProvider) -> None:
        self.provider = provider
        self.closed = False
        self.closed_after_cancel = False

    async def close(self) -> None:
        self.closed_after_cancel = self.provider.cancelled.is_set()
        self.closed = True


async def wait_until_idle(plugin, *, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while plugin._generation_tasks:
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


class BackgroundGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = SlowProvider()
        self.factory = FakeProviderFactory(self.provider)
        self.context = FakeContext()
        self.config = FakeConfig(
            {
                "base_url": "https://images.example.invalid/v1",
                "api_key": "top-secret-api-key",
                "advanced": {
                    "admin_only_generation": False,
                    "cooldown_seconds": 0,
                },
            },
        )
        self.plugin = main.CanvasForgePlugin(self.context, self.config)
        self.plugin._cache_ready = False

        async def ensure_runtime():
            return (
                self.factory,
                FakeReferenceResolver(),
                FakeAvatarResolver(),
            )

        self.plugin._ensure_runtime = ensure_runtime
        self.deliveries = 0

        async def deliver(_event, _image, lease, *_args, **_kwargs):
            self.deliveries += 1
            await lease.commit()

        self.plugin._send_generated_image_and_commit = deliver
        self.original_metadata_builder = main.build_source_metadata

        async def build_metadata(_event):
            return {}

        main.build_source_metadata = build_metadata

    async def asyncTearDown(self) -> None:
        main.build_source_metadata = self.original_metadata_builder
        self.provider.release.set()
        tasks = tuple(getattr(self.plugin, "_generation_tasks", ()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for lease in tuple(
            getattr(self.plugin, "_generation_leases", {}).values(),
        ):
            if not lease.finished:
                with contextlib.suppress(BaseException):
                    await lease.release()

    async def test_llm_tool_hands_off_quickly_and_duplicate_is_busy(self) -> None:
        event = FakeEvent()
        accepted = await asyncio.wait_for(
            self.plugin.canvasforge_text_to_image(event, "draw a lighthouse"),
            timeout=0.3,
        )
        self.assertIn("accepted=true", accepted)
        self.assertIn("completed=false", accepted)
        self.assertFalse(event.sent)
        await asyncio.wait_for(self.provider.started.wait(), timeout=0.3)

        # The provider remains blocked while this heartbeat and the duplicate
        # tool invocation both complete.
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        duplicate = await asyncio.wait_for(
            self.plugin.canvasforge_text_to_image(event, "draw another one"),
            timeout=0.3,
        )
        self.assertIn("busy", duplicate)
        self.assertIn(
            "CanvasForge 一次只能处理一个生图任务；请等待当前任务完成后再试。",
            duplicate,
        )
        self.assertEqual(1, self.provider.calls)

        self.provider.release.set()
        await wait_until_idle(self.plugin)
        self.assertEqual(1, self.deliveries)

    async def test_command_hands_off_and_reports_busy_without_queueing(self) -> None:
        first = FakeEvent("/canvasforge draw a moon")
        await asyncio.wait_for(
            self.plugin.canvasforge_command(first),
            timeout=0.3,
        )
        self.assertTrue(first.stopped)
        self.assertTrue(any("已受理" in text for text in first.sent_text))
        await asyncio.wait_for(self.provider.started.wait(), timeout=0.3)

        second = FakeEvent("/canvasforge draw a sun", sender_id="10002")
        await asyncio.wait_for(
            self.plugin.canvasforge_command(second),
            timeout=0.3,
        )
        self.assertTrue(
            any("一次只能处理一个生图任务" in text for text in second.sent_text),
        )
        self.assertEqual(1, self.provider.calls)

        self.provider.release.set()
        await wait_until_idle(self.plugin)

    async def test_late_failure_is_reported_and_releases_the_slot(self) -> None:
        self.provider.failure = main.CanvasForgeError(
            main.ErrorCode.UPSTREAM_UNAVAILABLE,
            "safe late failure",
        )
        event = FakeEvent()
        accepted = await self.plugin.canvasforge_text_to_image(
            event,
            "first",
            completion_message="不应发送的完成语",
        )
        self.assertIn("accepted=true", accepted)
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertFalse(await self.plugin._request_gate.is_busy())
        self.assertTrue(
            any("safe late failure" in text for text in event.sent_text),
        )
        self.assertFalse(
            any("不应发送的完成语" in text for text in event.sent_text),
        )

        # A failed request did not create cooldown and the task slot is usable.
        self.provider = SlowProvider()
        self.factory.provider = self.provider
        retry = await self.plugin.canvasforge_text_to_image(event, "retry")
        self.assertIn("accepted=true", retry)

    async def test_successful_delivery_commits_cooldown(self) -> None:
        self.config["advanced"]["cooldown_seconds"] = 300
        event = FakeEvent()
        accepted = await self.plugin.canvasforge_text_to_image(event, "first")
        self.assertIn("accepted=true", accepted)
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        retry = await self.plugin.canvasforge_text_to_image(event, "retry")
        self.assertIn("cooldown", retry)

    async def test_delivery_failure_does_not_commit_cooldown(self) -> None:
        self.config["advanced"]["cooldown_seconds"] = 300

        async def fail_delivery(_event, _image, _lease, *_args, **_kwargs):
            raise main.CanvasForgeError(main.ErrorCode.SEND_FAILED)

        self.plugin._send_generated_image_and_commit = fail_delivery
        event = FakeEvent()
        accepted = await self.plugin.canvasforge_text_to_image(
            event,
            "first",
            completion_message="不应发送的完成语",
        )
        self.assertIn("accepted=true", accepted)
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)
        self.assertTrue(any("send_failed" in text for text in event.sent_text))
        self.assertFalse(
            any("不应发送的完成语" in text for text in event.sent_text),
        )

        self.provider = SlowProvider()
        self.factory.provider = self.provider
        retry = await self.plugin.canvasforge_text_to_image(event, "retry")
        self.assertIn("accepted=true", retry)

    async def test_command_acceptance_send_failure_releases_lease(self) -> None:
        failed_event = FakeEvent(
            "/canvasforge first",
            send_fails=True,
        )
        await self.plugin.canvasforge_command(failed_event)
        self.assertEqual(0, self.provider.calls)
        self.assertFalse(await self.plugin._request_gate.is_busy())

        retry = await self.plugin.canvasforge_text_to_image(
            FakeEvent(),
            "retry",
        )
        self.assertIn("accepted=true", retry)

    async def test_task_creation_failure_releases_lease(self) -> None:
        real_create_task = asyncio.create_task

        def create_task(awaitable, *args, **kwargs):
            code = getattr(awaitable, "cr_code", None)
            if code is not None and code.co_name == "_run_background_generation":
                raise RuntimeError("simulated task creation failure")
            return real_create_task(awaitable, *args, **kwargs)

        with patch.object(main.asyncio, "create_task", side_effect=create_task):
            result = await self.plugin.canvasforge_text_to_image(
                FakeEvent(),
                "cannot start",
            )

        self.assertIn("internal", result)
        self.assertFalse(await self.plugin._request_gate.is_busy())

    async def test_cancel_before_background_body_runs_releases_lease(self) -> None:
        accepted = await self.plugin.canvasforge_text_to_image(
            FakeEvent(),
            "cancel immediately",
        )
        self.assertIn("accepted=true", accepted)
        task = next(iter(self.plugin._generation_tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        async def wait_for_cleanup() -> None:
            while self.plugin._lease_cleanup_tasks:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_cleanup(), timeout=1)
        self.assertFalse(await self.plugin._request_gate.is_busy())

    async def test_job_snapshot_is_immutable_and_hides_secrets_from_repr(self) -> None:
        event = FakeEvent()
        job = await self.plugin._prepare_generation_job(
            event,
            "private user prompt",
            requested_mode="generate",
        )
        try:
            rendered = repr(job)
            self.assertNotIn("private user prompt", rendered)
            self.assertNotIn("top-secret-api-key", rendered)
            self.assertNotIn("images.example.invalid", rendered)
            provider_config = main.HttpKeyProviderConfig(
                base_url="https://images.example.invalid/v1",
                api_key="top-secret-api-key",
            )
            self.assertNotIn("top-secret-api-key", repr(provider_config))
            self.assertIsInstance(job.settings, MappingProxyType)
            with self.assertRaises(TypeError):
                job.settings["model"] = "mutated"
        finally:
            await job.lease.release()

    async def test_missing_completion_message_uses_safe_fallback(self) -> None:
        self.plugin._send_generated_image_and_commit = (
            main.CanvasForgePlugin._send_generated_image_and_commit.__get__(
                self.plugin,
            )
        )
        event = FakeEvent()

        accepted = await self.plugin.canvasforge_text_to_image(
            event,
            "draw a fallback test",
        )
        self.assertIn("accepted=true", accepted)
        self.assertIn("completed=false", accepted)
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(2, len(event.sent))
        self.assertIsInstance(event.sent[0].chain[0], main.Comp.Image)
        self.assertIsInstance(event.sent[1].chain[0], main.Comp.Plain)
        self.assertEqual("图片画好啦～", event.sent[1].chain[0].text)

    async def test_success_sends_image_then_normalized_completion_message(
        self,
    ) -> None:
        self.config["advanced"]["cooldown_seconds"] = 300
        self.plugin._send_generated_image_and_commit = (
            main.CanvasForgePlugin._send_generated_image_and_commit.__get__(
                self.plugin,
            )
        )
        event = FakeEvent()

        accepted = await self.plugin.canvasforge_text_to_image(
            event,
            "draw an ordered delivery test",
            completion_message="  完成啦\n请查收  " + ("呀" * 100),
        )
        self.assertIn("completed=false", accepted)
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(2, len(event.sent))
        self.assertIsInstance(event.sent[0].chain[0], main.Comp.Image)
        self.assertIsInstance(event.sent[1].chain[0], main.Comp.Plain)
        completion_text = event.sent[1].chain[0].text
        self.assertNotIn("\n", completion_text)
        self.assertEqual(80, len(completion_text))
        self.assertIn("完成啦", completion_text)
        self.assertIn("请查收", completion_text)

        retry = await self.plugin.canvasforge_text_to_image(
            event,
            "retry",
            completion_message="不会发送",
        )
        self.assertIn("cooldown", retry)

    async def test_image_send_failure_has_no_completion_or_cooldown(
        self,
    ) -> None:
        self.config["advanced"]["cooldown_seconds"] = 300
        self.plugin._send_generated_image_and_commit = (
            main.CanvasForgePlugin._send_generated_image_and_commit.__get__(
                self.plugin,
            )
        )
        event = FakeEvent(image_send_fails=True)

        accepted = await self.plugin.canvasforge_text_to_image(
            event,
            "draw a failed delivery test",
            completion_message="不应发送的完成语",
        )
        self.assertIn("completed=false", accepted)
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertTrue(any("send_failed" in text for text in event.sent_text))
        self.assertFalse(
            any("不应发送的完成语" in text for text in event.sent_text),
        )

        self.provider = SlowProvider()
        self.factory.provider = self.provider
        retry = await self.plugin.canvasforge_text_to_image(
            FakeEvent(),
            "retry after failed image delivery",
            completion_message="稍后完成",
        )
        self.assertIn("accepted=true", retry)

    async def test_completion_send_failure_keeps_success_and_cooldown(
        self,
    ) -> None:
        self.config["advanced"]["cooldown_seconds"] = 300
        self.plugin._send_generated_image_and_commit = (
            main.CanvasForgePlugin._send_generated_image_and_commit.__get__(
                self.plugin,
            )
        )
        event = FakeEvent(plain_send_fails=True)

        accepted = await self.plugin.canvasforge_text_to_image(
            event,
            "draw a completion failure test",
            completion_message="这个完成语会发送失败",
        )
        self.assertIn("completed=false", accepted)
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(1, len(event.sent))
        self.assertIsInstance(event.sent[0].chain[0], main.Comp.Image)
        self.assertFalse(
            any("生图失败" in text for text in event.sent_text),
        )
        retry = await self.plugin.canvasforge_text_to_image(
            event,
            "retry after completion failure",
            completion_message="不会发送",
        )
        self.assertIn("cooldown", retry)

    async def test_terminate_cancels_jobs_before_closing_session(self) -> None:
        event = FakeEvent()
        accepted = await self.plugin.canvasforge_text_to_image(
            event,
            "long request",
            completion_message="取消时不应发送的完成语",
        )
        self.assertIn("accepted=true", accepted)
        await self.provider.started.wait()
        session = FakeSession(self.provider)
        self.plugin._session = session

        await asyncio.wait_for(self.plugin.terminate(), timeout=1)

        self.assertTrue(self.provider.cancelled.is_set())
        self.assertTrue(session.closed)
        self.assertTrue(session.closed_after_cancel)
        self.assertFalse(self.plugin._generation_tasks)
        self.assertFalse(await self.plugin._request_gate.is_busy())
        self.assertFalse(self.plugin._web_api._active)
        self.assertFalse(
            any("取消时不应发送的完成语" in text for text in event.sent_text),
        )


if __name__ == "__main__":
    unittest.main()
