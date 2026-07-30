from __future__ import annotations

import asyncio
import contextlib
import json
import unittest

from tests.astrbot_stubs import (
    FakeLLMResponse,
    fake_session_lock_manager,
)
from tests.plugin_loader import load_main_module


main = load_main_module()


class FakeConversation:
    def __init__(
        self,
        cid: str,
        history: list[dict[str, object]] | None = None,
    ) -> None:
        self.cid = cid
        self.history = json.dumps(
            list(history or []),
            ensure_ascii=False,
        )


class FakeConversationManager:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.current: dict[str, str | None] = {}
        self.conversations: dict[tuple[str, str], FakeConversation] = {}
        self.update_calls: list[dict[str, object]] = []

    def add(
        self,
        unified_msg_origin: str,
        conversation: FakeConversation,
        *,
        current: bool = True,
    ) -> None:
        self.conversations[
            (unified_msg_origin, conversation.cid)
        ] = conversation
        if current:
            self.current[unified_msg_origin] = conversation.cid

    async def get_curr_conversation_id(
        self,
        unified_msg_origin: str,
    ) -> str | None:
        return self.current.get(unified_msg_origin)

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ) -> FakeConversation | None:
        key = (unified_msg_origin, conversation_id)
        conversation = self.conversations.get(key)
        if conversation is None and create_if_not_exists:
            conversation = FakeConversation(conversation_id)
            self.conversations[key] = conversation
        return conversation

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict[str, object]] | None = None,
        **values,
    ) -> None:
        cid = conversation_id or self.current.get(unified_msg_origin)
        if cid is None:
            return
        conversation = self.conversations.get((unified_msg_origin, cid))
        if conversation is None:
            return
        if history is not None:
            conversation.history = json.dumps(
                history,
                ensure_ascii=False,
            )
        self.update_calls.append(
            {
                "umo": unified_msg_origin,
                "cid": cid,
                "history": history,
                **values,
            },
        )
        self.timeline.append("history")

    def switch(self, unified_msg_origin: str, conversation_id: str) -> None:
        self.current[unified_msg_origin] = conversation_id

    def delete(self, unified_msg_origin: str, conversation_id: str) -> None:
        self.conversations.pop(
            (unified_msg_origin, conversation_id),
            None,
        )
        if self.current.get(unified_msg_origin) == conversation_id:
            self.current[unified_msg_origin] = None


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[str] = []
        self.timeline: list[str] = []
        self.conversation_manager = FakeConversationManager(self.timeline)
        self.chat_provider_id = "chat-provider"
        self.provider_error: Exception | None = None
        self.llm_completion = "AI：图片已经画好啦。"
        self.llm_error: BaseException | None = None
        self.llm_calls: list[dict[str, object]] = []
        self.llm_started = asyncio.Event()
        self.llm_release = asyncio.Event()
        self.llm_release.set()
        self.outbound: list[tuple[str, object]] = []
        self.send_returns = True
        self.send_error: Exception | None = None
        self.send_results: dict[str, bool] = {}
        self.send_errors: dict[str, Exception] = {}

    def register_web_api(self, path, *_args) -> None:
        self.routes.append(path)

    async def get_current_chat_provider_id(
        self,
        umo: str,
    ) -> str:
        if self.provider_error is not None:
            raise self.provider_error
        return self.chat_provider_id

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(dict(kwargs))
        self.timeline.append("llm")
        self.llm_started.set()
        await self.llm_release.wait()
        if self.llm_error is not None:
            raise self.llm_error
        return FakeLLMResponse(self.llm_completion)

    async def send_message(self, umo: str, message) -> bool:
        component = message.chain[0]
        if isinstance(component, main.Comp.Image):
            kind = "image"
        elif isinstance(component, main.Comp.Plain):
            kind = "plain"
        else:
            kind = "other"
        self.timeline.append(kind)
        error = self.send_errors.get(kind, self.send_error)
        if error is not None:
            raise error
        sent = self.send_results.get(kind, self.send_returns)
        if sent:
            self.outbound.append((umo, message))
        return sent


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
        provider_request: object | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.raw_message = raw_message
        self.sender_id = sender_id
        self.unified_msg_origin = (
            f"aiocqhttp:GroupMessage:session-{sender_id}"
        )
        self.send_fails = send_fails
        self.image_send_fails = image_send_fails
        self.plain_send_fails = plain_send_fails
        self.provider_request = provider_request or FakeProviderRequest()
        self.extras: dict[str, object] = {
            "provider_request": self.provider_request,
        }
        self.timeline = timeline
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

    def get_extra(self, key: str, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value

    async def send(self, message) -> None:
        if self.send_fails:
            raise RuntimeError("simulated send failure")
        component = message.chain[0]
        if self.image_send_fails and isinstance(component, main.Comp.Image):
            raise RuntimeError("simulated image send failure")
        if self.plain_send_fails and isinstance(component, main.Comp.Plain):
            raise RuntimeError("simulated plain send failure")
        if self.timeline is not None:
            if isinstance(component, main.Comp.Image):
                self.timeline.append("image")
            elif isinstance(component, main.Comp.Plain):
                self.timeline.append("plain")
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


class FakeProviderRequest:
    def __init__(
        self,
        *,
        contexts: list[dict[str, object]] | None = None,
        system_prompt: str = "You are a friendly QQ assistant.",
        model: str = "chat-model",
    ) -> None:
        self.extra_user_content_parts: list[object] = []
        self.contexts = list(contexts or [])
        self.system_prompt = system_prompt
        self.model = model


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
    async def snapshot(self, _event):
        return main.ReferenceSnapshot(())

    async def resolve_snapshot(self, *_args, **_kwargs):
        return []

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
        while (
            getattr(plugin, "_gate_tasks", ())
            or getattr(plugin, "_generation_tasks", ())
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


def assistant_text(history_entry: dict[str, object]) -> str:
    content = history_entry.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                values.append(part["text"])
        return "".join(values)
    return ""


class AgentTurnHarness:
    def __init__(
        self,
        plugin,
        event: FakeEvent,
        prompt: str,
    ) -> None:
        self.plugin = plugin
        self.event = event
        self.prompt = prompt
        self.tool_result: asyncio.Future[str] = (
            asyncio.get_running_loop().create_future()
        )
        self.allow_final_response = asyncio.Event()
        self.final_response_seen = asyncio.Event()
        self.allow_unlock = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async with fake_session_lock_manager.acquire_lock(
            self.event.unified_msg_origin,
        ):
            result = await self.plugin.canvasforge_text_to_image(
                self.event,
                self.prompt,
            )
            self.tool_result.set_result(result)
            await self.allow_final_response.wait()
            await self.plugin.start_generation_after_llm_response(
                self.event,
                FakeLLMResponse("好的，图片任务已准备。"),
            )
            self.final_response_seen.set()
            manager = self.plugin.context.conversation_manager
            cid = await manager.get_curr_conversation_id(
                self.event.unified_msg_origin,
            )
            if cid is not None:
                conversation = await manager.get_conversation(
                    self.event.unified_msg_origin,
                    cid,
                )
                if conversation is not None:
                    history = json.loads(conversation.history)
                    history.append(
                        {
                            "role": "tool",
                            "content": result,
                        },
                    )
                    history.append(
                        {
                            "role": "assistant",
                            "content": "好的，图片任务已准备。",
                        },
                    )
                    conversation.history = json.dumps(
                        history,
                        ensure_ascii=False,
                    )
            await self.allow_unlock.wait()

    async def result(self) -> str:
        return await asyncio.wait_for(
            asyncio.shield(self.tool_result),
            timeout=0.5,
        )

    async def signal_final_response(self) -> None:
        self.allow_final_response.set()
        await asyncio.wait_for(
            self.final_response_seen.wait(),
            timeout=0.5,
        )

    async def unlock(self) -> None:
        self.allow_unlock.set()
        await asyncio.wait_for(self.task, timeout=0.5)


class BackgroundGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = SlowProvider()
        self.factory = FakeProviderFactory(self.provider)
        self.context = FakeContext()
        fake_session_lock_manager.reset()
        self.default_umo = "aiocqhttp:GroupMessage:session-10001"
        self.initial_history: list[dict[str, object]] = [
            {
                "role": "user",
                "content": "请画一张测试图片",
            },
            {
                "role": "_checkpoint",
                "content": {"id": "checkpoint-1"},
            },
            {
                "role": "assistant",
                "content": "我来准备。",
            },
        ]
        self.context.conversation_manager.add(
            self.default_umo,
            FakeConversation("cid-original", self.initial_history),
        )
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
        self.original_metadata_builder = main.build_source_metadata

        async def build_metadata(_event):
            return {}

        main.build_source_metadata = build_metadata

    async def asyncTearDown(self) -> None:
        main.build_source_metadata = self.original_metadata_builder
        self.provider.release.set()
        self.context.llm_release.set()
        tasks = tuple(
            {
                *getattr(self.plugin, "_gate_tasks", ()),
                *getattr(self.plugin, "_generation_tasks", ()),
            },
        )
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

    async def test_generation_waits_for_final_reply_and_session_unlock(
        self,
    ) -> None:
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(
            self.plugin,
            event,
            "draw a lighthouse",
        )

        result = await turn.result()
        self.assertIn("accepted=true", result)
        self.assertEqual(0, self.provider.calls)
        self.assertTrue(await self.plugin._request_gate.is_busy())
        self.assertTrue(self.plugin._prepared_jobs)
        self.assertFalse(self.plugin._gate_tasks)

        await turn.signal_final_response()
        await asyncio.sleep(0)
        self.assertEqual(0, self.provider.calls)
        self.assertTrue(self.plugin._gate_tasks)

        await turn.unlock()
        await asyncio.wait_for(self.provider.started.wait(), timeout=0.5)
        self.assertEqual(1, self.provider.calls)

        # Slow image generation must not retain AstrBot's per-session lock.
        async with asyncio.timeout(0.2):
            async with fake_session_lock_manager.acquire_lock(
                event.unified_msg_origin,
            ):
                pass

        self.provider.release.set()
        await wait_until_idle(self.plugin)

    async def test_empty_llm_response_gates_and_missing_reply_aborts(
        self,
    ) -> None:
        event = FakeEvent(timeline=self.context.timeline)
        manager = self.context.conversation_manager

        async with fake_session_lock_manager.acquire_lock(
            event.unified_msg_origin,
        ):
            result = await self.plugin.canvasforge_text_to_image(
                event,
                "draw after an empty final response",
            )
            self.assertIn("accepted=true", result)
            await self.plugin.start_generation_after_llm_response(
                event,
                FakeLLMResponse(""),
            )

            conversation = await manager.get_conversation(
                event.unified_msg_origin,
                "cid-original",
            )
            self.assertIsNotNone(conversation)
            history = json.loads(conversation.history)
            history.append({"role": "tool", "content": result})
            conversation.history = json.dumps(history, ensure_ascii=False)

            self.assertFalse(self.plugin._prepared_jobs)
            self.assertTrue(self.plugin._gate_tasks)

        await wait_until_idle(self.plugin)

        self.assertEqual(0, self.provider.calls)
        self.assertEqual(0, len(self.context.llm_calls))
        self.assertFalse(await self.plugin._request_gate.is_busy())
        self.assertFalse(self.plugin._generation_tasks)
        self.assertTrue(
            any(
                isinstance(message.chain[0], main.Comp.Plain)
                and "未能启动" in message.chain[0].text
                for _, message in self.context.outbound
            ),
        )

    async def test_command_starts_after_its_pipeline_unlocks(self) -> None:
        event = FakeEvent(
            "/canvasforge draw a moon",
            timeline=self.context.timeline,
        )
        allow_unlock = asyncio.Event()

        async def command_turn() -> None:
            async with fake_session_lock_manager.acquire_lock(
                event.unified_msg_origin,
            ):
                await self.plugin.canvasforge_command(event)
                await allow_unlock.wait()

        task = asyncio.create_task(command_turn())
        while not event.sent_text:
            await asyncio.sleep(0)

        self.assertTrue(event.stopped)
        self.assertTrue(any("已受理" in text for text in event.sent_text))

        allow_unlock.set()
        await asyncio.wait_for(task, timeout=0.5)
        await asyncio.wait_for(self.provider.started.wait(), timeout=0.5)
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(1, len(self.context.llm_calls))
        self.assertEqual(
            ["plain", "history", "image", "llm", "plain", "history"],
            self.context.timeline,
        )

    async def test_command_pending_history_has_assistant_then_checkpoint(
        self,
    ) -> None:
        event = FakeEvent(
            "/canvasforge draw a checkpoint",
            timeline=self.context.timeline,
        )

        async with fake_session_lock_manager.acquire_lock(
            event.unified_msg_origin,
        ):
            await self.plugin.canvasforge_command(event)

        await asyncio.wait_for(self.provider.started.wait(), timeout=0.5)
        conversation = self.context.conversation_manager.conversations[
            (self.default_umo, "cid-original")
        ]
        history = json.loads(conversation.history)
        job = next(iter(self.plugin._generation_jobs.values()))

        self.assertEqual("assistant", history[-2]["role"])
        self.assertEqual(
            main._COMMAND_PENDING_MESSAGE,
            assistant_text(history[-2]),
        )
        self.assertEqual("_checkpoint", history[-1]["role"])
        self.assertEqual(job.task_id, history[-1]["content"]["id"])

        self.provider.release.set()
        await wait_until_idle(self.plugin)

    async def test_command_reset_same_conversation_skips_history_writeback(
        self,
    ) -> None:
        event = FakeEvent(
            "/canvasforge draw then reset",
            timeline=self.context.timeline,
        )

        async with fake_session_lock_manager.acquire_lock(
            event.unified_msg_origin,
        ):
            await self.plugin.canvasforge_command(event)

        await asyncio.wait_for(self.provider.started.wait(), timeout=0.5)
        manager = self.context.conversation_manager
        conversation = manager.conversations[
            (self.default_umo, "cid-original")
        ]
        pending_update_count = len(manager.update_calls)
        reset_history: list[dict[str, object]] = [
            {"role": "user", "content": "同一会话已经重置"},
        ]
        async with fake_session_lock_manager.acquire_lock(self.default_umo):
            conversation.history = json.dumps(
                reset_history,
                ensure_ascii=False,
            )

        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(1, len(self.context.llm_calls))
        self.assertEqual(
            ["image", "plain"],
            [
                (
                    "image"
                    if isinstance(message.chain[0], main.Comp.Image)
                    else "plain"
                )
                for _, message in self.context.outbound
            ],
        )
        self.assertEqual(reset_history, json.loads(conversation.history))
        self.assertEqual(pending_update_count, len(manager.update_calls))

    async def test_late_failure_is_reported_and_releases_the_slot(self) -> None:
        self.provider.failure = main.CanvasForgeError(
            main.ErrorCode.UPSTREAM_UNAVAILABLE,
            "safe late failure",
        )
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(self.plugin, event, "first")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertFalse(await self.plugin._request_gate.is_busy())
        self.assertEqual(0, len(self.context.llm_calls))
        history = json.loads(
            self.context.conversation_manager.conversations[
                (self.default_umo, "cid-original")
            ].history,
        )
        self.assertIn("safe late failure", assistant_text(history[-1]))
        self.assertNotIn("image", self.context.timeline)

        # A failed request did not create cooldown and the task slot is usable.
        retry_lease = await self.plugin._request_gate.acquire(
            "10001",
            cooldown_seconds=300,
        )
        await retry_lease.release()

    async def test_successful_delivery_commits_cooldown(self) -> None:
        self.config["advanced"]["cooldown_seconds"] = 300
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(self.plugin, event, "first")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        with self.assertRaises(main.CanvasForgeError) as caught:
            await self.plugin._request_gate.acquire(
                "10001",
                cooldown_seconds=300,
            )
        self.assertEqual(main.ErrorCode.COOLDOWN, caught.exception.code)

    async def test_delivery_failure_does_not_commit_cooldown(self) -> None:
        self.config["advanced"]["cooldown_seconds"] = 300
        self.context.send_results["image"] = False
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(self.plugin, event, "first")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(0, len(self.context.llm_calls))
        retry_lease = await self.plugin._request_gate.acquire(
            "10001",
            cooldown_seconds=300,
        )
        await retry_lease.release()

    async def test_command_acceptance_send_failure_releases_lease(self) -> None:
        failed_event = FakeEvent(
            "/canvasforge first",
            send_fails=True,
        )
        await self.plugin.canvasforge_command(failed_event)
        self.assertEqual(0, self.provider.calls)
        self.assertFalse(await self.plugin._request_gate.is_busy())
        self.assertFalse(self.plugin._prepared_jobs)
        self.assertFalse(self.plugin._gate_tasks)

    async def test_preflight_failure_is_cached_for_the_same_agent_event(
        self,
    ) -> None:
        event = FakeEvent()

        result = await self.plugin.canvasforge_text_to_image(event, "")
        repeated = await self.plugin.canvasforge_text_to_image(
            event,
            "must not be admitted after the first failure",
        )

        self.assertIn("missing_prompt", result)
        self.assertEqual(result, repeated)
        self.assertFalse(event.stopped)
        self.assertEqual(0, self.provider.calls)
        self.assertFalse(self.plugin._prepared_jobs)
        self.assertFalse(self.plugin._gate_tasks)
        self.assertFalse(self.plugin._generation_tasks)

    async def test_terminate_discards_prepared_jobs_and_future_hook(
        self,
    ) -> None:
        event = FakeEvent()
        turn = AgentTurnHarness(self.plugin, event, "cancel immediately")
        self.assertIn("accepted=true", await turn.result())
        self.assertTrue(self.plugin._prepared_jobs)

        await asyncio.wait_for(self.plugin.terminate(), timeout=1)

        self.assertFalse(self.plugin._prepared_jobs)
        self.assertFalse(self.plugin._gate_tasks)
        self.assertFalse(self.plugin._generation_tasks)
        self.assertFalse(await self.plugin._request_gate.is_busy())
        await turn.signal_final_response()
        await turn.unlock()
        await asyncio.sleep(0)
        self.assertEqual(0, self.provider.calls)

    async def test_prepared_job_reserves_the_single_global_slot(self) -> None:
        first_event = FakeEvent()
        turn = AgentTurnHarness(self.plugin, first_event, "first")
        self.assertIn("accepted=true", await turn.result())

        duplicate_event = FakeEvent(sender_id="20002")
        duplicate = await self.plugin.canvasforge_text_to_image(
            duplicate_event,
            "duplicate",
        )
        repeated_duplicate = await self.plugin.canvasforge_text_to_image(
            duplicate_event,
            "must not retry the busy request",
        )
        self.assertIn("busy", duplicate)
        self.assertEqual(duplicate, repeated_duplicate)
        self.assertFalse(duplicate_event.stopped)
        self.assertEqual(0, self.provider.calls)

        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

    async def test_success_sends_image_then_one_ai_reply_and_history(
        self,
    ) -> None:
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(
            self.plugin,
            event,
            "draw an ordered completion test",
        )
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(
            ["image", "llm", "plain", "history"],
            self.context.timeline,
        )
        self.assertEqual(1, len(self.context.llm_calls))
        llm_call = self.context.llm_calls[0]
        self.assertIsNone(llm_call["tools"])
        self.assertEqual(1, llm_call["request_max_retries"])
        self.assertEqual("chat-provider", llm_call["chat_provider_id"])
        self.assertEqual(
            "You are a friendly QQ assistant.",
            llm_call["system_prompt"],
        )
        self.assertEqual("chat-model", llm_call["model"])
        context_roles = [
            (
                item.get("role")
                if isinstance(item, dict)
                else getattr(item, "role", None)
            )
            for item in llm_call["contexts"]
        ]
        self.assertNotIn("_checkpoint", context_roles)

        self.assertEqual(2, len(self.context.outbound))
        self.assertIsInstance(
            self.context.outbound[0][1].chain[0],
            main.Comp.Image,
        )
        completion_component = self.context.outbound[1][1].chain[0]
        self.assertIsInstance(completion_component, main.Comp.Plain)
        self.assertEqual(
            self.context.llm_completion,
            completion_component.text,
        )

        conversation = self.context.conversation_manager.conversations[
            (self.default_umo, "cid-original")
        ]
        history = json.loads(conversation.history)
        self.assertTrue(
            any(item.get("role") == "_checkpoint" for item in history),
        )
        self.assertEqual("assistant", history[-1]["role"])
        self.assertEqual(
            self.context.llm_completion,
            assistant_text(history[-1]),
        )

    async def test_completion_llm_does_not_hold_global_generation_slot(
        self,
    ) -> None:
        self.context.llm_release.clear()
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(
            self.plugin,
            event,
            "draw while completion is blocked",
        )
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await asyncio.wait_for(self.provider.started.wait(), timeout=0.5)
        self.provider.release.set()
        await asyncio.wait_for(self.context.llm_started.wait(), timeout=0.5)

        self.assertIn("image", self.context.timeline)
        second_lease = await asyncio.wait_for(
            self.plugin._request_gate.acquire(
                "20002",
                cooldown_seconds=300,
            ),
            timeout=0.2,
        )
        try:
            self.assertTrue(await self.plugin._request_gate.is_busy())
        finally:
            await second_lease.release()

        self.context.llm_release.set()
        await wait_until_idle(self.plugin)

    async def test_completion_ai_timeout_sends_and_records_fallback(
        self,
    ) -> None:
        self.context.llm_error = asyncio.TimeoutError()
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(self.plugin, event, "draw a timeout test")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(1, len(self.context.llm_calls))
        fallback = self.context.outbound[-1][1].chain[0].text
        self.assertEqual("图片已生成并发送。", fallback)
        history = json.loads(
            self.context.conversation_manager.conversations[
                (self.default_umo, "cid-original")
            ].history,
        )
        self.assertEqual(fallback, assistant_text(history[-1]))

    async def test_conversation_switch_updates_only_original_conversation(
        self,
    ) -> None:
        new_history: list[dict[str, object]] = [
            {"role": "user", "content": "这是新会话"},
        ]
        self.context.conversation_manager.add(
            self.default_umo,
            FakeConversation("cid-new", new_history),
            current=False,
        )
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(self.plugin, event, "draw in old cid")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        self.context.conversation_manager.switch(
            self.default_umo,
            "cid-new",
        )
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(1, len(self.context.llm_calls))
        self.assertTrue(
            all(
                call["cid"] == "cid-original"
                for call in self.context.conversation_manager.update_calls
            ),
        )
        original = self.context.conversation_manager.conversations[
            (self.default_umo, "cid-original")
        ]
        switched = self.context.conversation_manager.conversations[
            (self.default_umo, "cid-new")
        ]
        self.assertEqual(
            self.context.llm_completion,
            assistant_text(json.loads(original.history)[-1]),
        )
        self.assertEqual(
            new_history,
            json.loads(switched.history),
        )

    async def test_deleted_conversation_uses_admission_context_without_history(
        self,
    ) -> None:
        admission_contexts: list[dict[str, object]] = [
            {
                "role": "user",
                "content": "captured admission context",
            },
        ]
        event = FakeEvent(
            provider_request=FakeProviderRequest(
                contexts=admission_contexts,
                system_prompt="captured system prompt",
                model="captured-model",
            ),
            timeline=self.context.timeline,
        )
        turn = AgentTurnHarness(self.plugin, event, "draw then delete cid")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.context.conversation_manager.delete(
            self.default_umo,
            "cid-original",
        )
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(1, len(self.context.llm_calls))
        self.assertEqual(
            admission_contexts,
            self.context.llm_calls[0]["contexts"],
        )
        self.assertEqual(
            "captured system prompt",
            self.context.llm_calls[0]["system_prompt"],
        )
        self.assertEqual(
            "captured-model",
            self.context.llm_calls[0]["model"],
        )
        self.assertEqual(2, len(self.context.outbound))
        self.assertFalse(
            self.context.conversation_manager.update_calls,
        )

    async def test_completion_ai_exception_uses_fixed_fallback(
        self,
    ) -> None:
        self.context.llm_error = RuntimeError("simulated completion failure")
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(self.plugin, event, "draw an exception test")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(1, len(self.context.llm_calls))
        self.assertEqual(
            "图片已生成并发送。",
            self.context.outbound[-1][1].chain[0].text,
        )

    async def test_completion_send_false_records_canonical_history_status(
        self,
    ) -> None:
        self.context.send_results["plain"] = False
        event = FakeEvent(timeline=self.context.timeline)
        turn = AgentTurnHarness(self.plugin, event, "draw a send false test")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        self.provider.release.set()
        await wait_until_idle(self.plugin)

        self.assertEqual(1, len(self.context.outbound))
        history = json.loads(
            self.context.conversation_manager.conversations[
                (self.default_umo, "cid-original")
            ].history,
        )
        self.assertEqual(
            "图片已成功发送，但完成通知未能送达。",
            assistant_text(history[-1]),
        )

    async def test_terminate_cancels_jobs_before_closing_session(self) -> None:
        event = FakeEvent()
        turn = AgentTurnHarness(self.plugin, event, "long request")
        self.assertIn("accepted=true", await turn.result())
        await turn.signal_final_response()
        await turn.unlock()
        await self.provider.started.wait()
        session = FakeSession(self.provider)
        self.plugin._session = session

        await asyncio.wait_for(self.plugin.terminate(), timeout=1)

        self.assertTrue(self.provider.cancelled.is_set())
        self.assertTrue(session.closed)
        self.assertTrue(session.closed_after_cancel)
        self.assertFalse(self.plugin._prepared_jobs)
        self.assertFalse(self.plugin._gate_tasks)
        self.assertFalse(self.plugin._generation_tasks)
        self.assertFalse(await self.plugin._request_gate.is_busy())
        self.assertFalse(self.plugin._web_api._active)
        self.assertEqual(0, len(self.context.llm_calls))


if __name__ == "__main__":
    unittest.main()
