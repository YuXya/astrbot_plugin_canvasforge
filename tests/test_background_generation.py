from __future__ import annotations

import asyncio
import contextlib
import json
import unittest
from types import SimpleNamespace
from typing import Any

from tests.astrbot_stubs import FakeLLMResponse, fake_session_lock_manager
from tests.plugin_loader import load_main_module


main = load_main_module()


class FakeConversation:
    def __init__(
        self,
        cid: str,
        history: list[dict[str, object]] | None = None,
    ) -> None:
        self.cid = cid
        self.history = json.dumps(list(history or []), ensure_ascii=False)


class FakeConversationManager:
    """AstrBot-like history store with controllable silent write loss."""

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.current: dict[str, str | None] = {}
        self.conversations: dict[tuple[str, str], FakeConversation] = {}
        self.update_calls: list[dict[str, object]] = []
        self.silent_update_noops = 0
        self.current_read_error: Exception | None = None
        self.conversation_read_error: Exception | None = None

    def add(
        self,
        unified_msg_origin: str,
        conversation: FakeConversation,
        *,
        current: bool = True,
    ) -> None:
        self.conversations[(unified_msg_origin, conversation.cid)] = conversation
        if current:
            self.current[unified_msg_origin] = conversation.cid

    async def get_curr_conversation_id(
        self,
        unified_msg_origin: str,
    ) -> str | None:
        if self.current_read_error is not None:
            raise self.current_read_error
        return self.current.get(unified_msg_origin)

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ) -> FakeConversation | None:
        if self.conversation_read_error is not None:
            raise self.conversation_read_error
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
        **values: object,
    ) -> None:
        cid = conversation_id or self.current.get(unified_msg_origin)
        call = {
            "umo": unified_msg_origin,
            "cid": cid,
            "history": history,
            **values,
        }
        self.update_calls.append(call)
        if self.silent_update_noops:
            self.silent_update_noops -= 1
            self.timeline.append("history-noop")
            return
        if cid is None:
            return
        conversation = self.conversations.get((unified_msg_origin, cid))
        if conversation is None:
            return
        if history is not None:
            conversation.history = json.dumps(history, ensure_ascii=False)
        self.timeline.append("history")

    def switch(self, unified_msg_origin: str, conversation_id: str) -> None:
        self.current[unified_msg_origin] = conversation_id

    def delete(self, unified_msg_origin: str, conversation_id: str) -> None:
        self.conversations.pop((unified_msg_origin, conversation_id), None)
        if self.current.get(unified_msg_origin) == conversation_id:
            self.current[unified_msg_origin] = None


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[str] = []
        self.timeline: list[str] = []
        self.conversation_manager = FakeConversationManager(self.timeline)
        self.chat_provider_id = "chat-provider"
        self.provider_error: Exception | None = None
        self.llm_completion = "图片已经画好啦。"
        self.llm_error: BaseException | None = None
        self.llm_calls: list[dict[str, object]] = []
        self.llm_release = asyncio.Event()
        self.llm_release.set()
        self.llm_release_events: list[asyncio.Event] = [self.llm_release]
        self.llm_cancelled = 0
        self.outbound: list[tuple[str, object]] = []
        self.send_returns = True
        self.send_error: Exception | None = None
        self.send_results: dict[str, bool] = {}
        self.send_errors: dict[str, Exception] = {}
        self.blocked_send_kinds: set[str] = set()
        self.send_started = asyncio.Event()
        self.send_release = asyncio.Event()
        self.send_cancelled = 0

    def block_llm(self) -> None:
        self.llm_release = asyncio.Event()
        self.llm_release_events.append(self.llm_release)

    def release_llm(self) -> None:
        self.llm_release.set()

    def register_web_api(self, path: str, *_args: object) -> None:
        self.routes.append(path)

    async def get_current_chat_provider_id(self, _umo: str) -> str:
        if self.provider_error is not None:
            raise self.provider_error
        return self.chat_provider_id

    async def llm_generate(self, **kwargs: object) -> FakeLLMResponse:
        self.llm_calls.append(dict(kwargs))
        self.timeline.append("llm")
        release = self.llm_release
        try:
            await release.wait()
        except asyncio.CancelledError:
            self.llm_cancelled += 1
            raise
        if self.llm_error is not None:
            raise self.llm_error
        return FakeLLMResponse(self.llm_completion)

    async def send_message(self, umo: str, message: object) -> bool:
        component = message.chain[0]
        if isinstance(component, main.Comp.Image):
            kind = "image"
        elif isinstance(component, main.Comp.Plain):
            kind = "plain"
        else:
            kind = "other"
        self.timeline.append(kind)
        if kind in self.blocked_send_kinds:
            self.send_started.set()
            try:
                await self.send_release.wait()
            except asyncio.CancelledError:
                self.send_cancelled += 1
                raise
        error = self.send_errors.get(kind, self.send_error)
        if error is not None:
            raise error
        sent = self.send_results.get(kind, self.send_returns)
        if sent:
            self.outbound.append((umo, message))
        return sent


class FakeConfig(dict):
    async def save_config_async(self, values: object = None) -> bool:
        if isinstance(values, dict):
            self.update(values)
        return True

    def save_config(self) -> None:
        pass


class FakeProviderRequest:
    def __init__(
        self,
        conversation_id: str,
        *,
        contexts: list[dict[str, object]] | None = None,
        system_prompt: str = "You are a friendly QQ assistant.",
        model: str = "chat-model",
    ) -> None:
        self.extra_user_content_parts: list[object] = []
        self.contexts = list(contexts or [])
        self.system_prompt = system_prompt
        self.model = model
        self.conversation = SimpleNamespace(cid=conversation_id)


class FakeEvent:
    def __init__(
        self,
        raw_message: str,
        *,
        umo: str,
        conversation_id: str,
        contexts: list[dict[str, object]],
        sender_id: str = "10001",
        send_fails: bool = False,
        reference_sources: tuple[str, ...] = (),
        timeline: list[str] | None = None,
    ) -> None:
        self.raw_message = raw_message
        self.sender_id = sender_id
        self.unified_msg_origin = umo
        self.send_fails = send_fails
        self.reference_sources = reference_sources
        self.provider_request = FakeProviderRequest(
            conversation_id,
            contexts=contexts,
        )
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
        return self.unified_msg_origin.rsplit(":", 1)[-1]

    def get_message_str(self) -> str:
        return self.raw_message

    def stop_event(self) -> None:
        self.stopped = True

    def get_extra(self, key: str, default: object = None) -> object:
        return self.extras.get(key, default)

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value

    async def send(self, message: object) -> None:
        if self.send_fails:
            raise RuntimeError("simulated command send failure")
        component = message.chain[0]
        if self.timeline is not None:
            if isinstance(component, main.Comp.Plain):
                self.timeline.append("command-plain")
            elif isinstance(component, main.Comp.Image):
                self.timeline.append("command-image")
        self.sent.append(message)

    @property
    def sent_text(self) -> list[str]:
        return [
            component.text
            for message in self.sent
            for component in getattr(message, "chain", ())
            if isinstance(getattr(component, "text", None), str)
        ]


class SlowProvider:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.calls = 0
        self.modes: list[str] = []
        self.failure: Exception | None = None
        self.release = asyncio.Event()
        self.release_events: list[asyncio.Event] = [self.release]
        self.cancelled = 0

    def block(self) -> None:
        self.release = asyncio.Event()
        self.release_events.append(self.release)

    def allow(self) -> None:
        self.release.set()

    async def _run(self, mode: str) -> object:
        self.calls += 1
        self.modes.append(mode)
        self.timeline.append("provider")
        release = self.release
        try:
            await release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.failure is not None:
            raise self.failure
        return main.GeneratedImage(
            data=b"generated-image",
            mime_type="image/png",
            format="png",
            width=1,
            height=1,
        )

    async def generate(self, _prompt: str, _options: object) -> object:
        return await self._run("generate")

    async def edit(
        self,
        _prompt: str,
        _references: object,
        _options: object,
    ) -> object:
        return await self._run("edit")


class FakeProviderFactory:
    def __init__(self, provider: SlowProvider) -> None:
        self.provider = provider
        self.seen_api_keys: list[str] = []

    def create(self, config: object) -> SlowProvider:
        self.seen_api_keys.append(config.api_key)
        return self.provider


class FakeReferenceResolver:
    async def snapshot_all(self, event: FakeEvent) -> object:
        sources = tuple(event.reference_sources)
        return main.ReferenceSnapshot(
            sources,
            current_image_count=len(sources),
        )

    async def resolve_snapshot(
        self,
        snapshot: object,
        *_args: object,
        **_kwargs: object,
    ) -> list[object]:
        return [
            SimpleNamespace(data=b"reference")
            for _source in snapshot.sources
        ]


class FakeAvatarResolver:
    def plan(self, _event: object, _targets: object) -> list[object]:
        return []

    async def download(self, *_args: object, **_kwargs: object) -> list[object]:
        return []


class FakeSession:
    def __init__(self, provider: SlowProvider, context: FakeContext) -> None:
        self.provider = provider
        self.context = context
        self.closed = False
        self.closed_after_cancellations = False

    async def close(self) -> None:
        self.closed_after_cancellations = (
            self.provider.cancelled > 0 and self.context.llm_cancelled > 0
        )
        self.closed = True


def parse_status(value: str) -> dict[str, Any]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise AssertionError("tool status must be a JSON object")
    return payload


def parse_history(conversation: FakeConversation) -> list[dict[str, Any]]:
    value = json.loads(conversation.history)
    if not isinstance(value, list):
        raise AssertionError("conversation history must be a JSON list")
    return value


def history_text(item: dict[str, Any]) -> str:
    content = item.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict)
    )


async def wait_for(predicate, *, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


async def wait_plugin_quiet(plugin: object, *, timeout: float = 1.5) -> None:
    async def poll() -> None:
        quiet_rounds = 0
        while quiet_rounds < 3:
            active = bool(
                getattr(plugin, "_generation_tasks", ())
                or getattr(plugin, "_notification_tasks", ())
                or getattr(plugin, "_lease_cleanup_tasks", ())
            )
            if not active and await plugin._request_gate.active_count() == 0:
                quiet_rounds += 1
            else:
                quiet_rounds = 0
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


class BackgroundGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fake_session_lock_manager.reset()
        self.context = FakeContext()
        self.provider = SlowProvider(self.context.timeline)
        self.factory = FakeProviderFactory(self.provider)
        self.reference_resolver = FakeReferenceResolver()
        self.avatar_resolver = FakeAvatarResolver()
        self.default_umo = "aiocqhttp:GroupMessage:shared-chat"
        self.config = FakeConfig(
            {
                "base_url": "https://images.example.invalid/v1",
                "api_key": "top-secret-api-key",
                "advanced": {
                    "admin_only_generation": False,
                    "cooldown_seconds": 0,
                    "max_concurrent_generations": 3,
                },
            },
        )
        self.plugin = main.CanvasForgePlugin(self.context, self.config)
        self.plugin._cache_ready = False

        async def ensure_runtime() -> tuple[object, object, object]:
            return (
                self.factory,
                self.reference_resolver,
                self.avatar_resolver,
            )

        self.plugin._ensure_runtime = ensure_runtime
        self.original_metadata_builder = main.build_source_metadata
        self.original_completion_timeout = main._COMPLETION_TIMEOUT_SECONDS
        self.original_active_send_timeout = getattr(
            main,
            "_ACTIVE_SEND_TIMEOUT_SECONDS",
            None,
        )

        async def build_metadata(_event: object) -> dict[str, str]:
            return {}

        main.build_source_metadata = build_metadata
        self.add_conversation("cid-1", umo=self.default_umo)

    async def asyncTearDown(self) -> None:
        main.build_source_metadata = self.original_metadata_builder
        main._COMPLETION_TIMEOUT_SECONDS = self.original_completion_timeout
        if self.original_active_send_timeout is None:
            with contextlib.suppress(AttributeError):
                delattr(main, "_ACTIVE_SEND_TIMEOUT_SECONDS")
        else:
            main._ACTIVE_SEND_TIMEOUT_SECONDS = (
                self.original_active_send_timeout
            )
        self.context.send_release.set()
        for release in self.provider.release_events:
            release.set()
        for release in self.context.llm_release_events:
            release.set()
        tasks = tuple(
            {
                *getattr(self.plugin, "_generation_tasks", ()),
                *getattr(self.plugin, "_notification_tasks", ()),
                *getattr(self.plugin, "_lease_cleanup_tasks", ()),
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

    def add_conversation(
        self,
        cid: str,
        *,
        umo: str,
        history: list[dict[str, object]] | None = None,
        current: bool = True,
    ) -> FakeConversation:
        conversation = FakeConversation(
            cid,
            history
            if history is not None
            else [
                {
                    "role": "user",
                    "content": f"request for {cid}",
                },
            ],
        )
        self.context.conversation_manager.add(
            umo,
            conversation,
            current=current,
        )
        return conversation

    def make_event(
        self,
        cid: str,
        *,
        umo: str | None = None,
        sender_id: str = "10001",
        raw_message: str = "draw a test image",
        send_fails: bool = False,
        reference_sources: tuple[str, ...] = (),
    ) -> FakeEvent:
        resolved_umo = umo or self.default_umo
        conversation = self.context.conversation_manager.conversations[
            (resolved_umo, cid)
        ]
        return FakeEvent(
            raw_message,
            umo=resolved_umo,
            conversation_id=cid,
            contexts=parse_history(conversation),
            sender_id=sender_id,
            send_fails=send_fails,
            reference_sources=reference_sources,
            timeline=self.context.timeline,
        )

    def stage_agent_history(
        self,
        event: FakeEvent,
        result: str,
        *,
        tool_call_id: str = "tool-call-1",
        assistant_reply: str = "正在处理这张图片。",
    ) -> None:
        cid = event.provider_request.conversation.cid
        conversation = self.context.conversation_manager.conversations[
            (event.unified_msg_origin, cid)
        ]
        history = parse_history(conversation)
        history.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            },
        )
        history.append(
            {
                "role": "assistant",
                "content": assistant_reply,
            },
        )
        conversation.history = json.dumps(history, ensure_ascii=False)

    async def invoke_and_stage(
        self,
        event: FakeEvent,
        prompt: str = "draw a lighthouse",
        *,
        tool_call_id: str = "tool-call-1",
    ) -> tuple[str, dict[str, Any]]:
        async with fake_session_lock_manager.acquire_lock(
            event.unified_msg_origin,
        ):
            result = await self.plugin.canvasforge_text_to_image(event, prompt)
            payload = parse_status(result)
            if payload.get("accepted") is True:
                self.stage_agent_history(
                    event,
                    result,
                    tool_call_id=tool_call_id,
                )
        return result, payload

    def conversation(self, cid: str, *, umo: str | None = None) -> FakeConversation:
        return self.context.conversation_manager.conversations[
            (umo or self.default_umo, cid)
        ]

    def tool_entries(self, cid: str, *, umo: str | None = None) -> list[dict[str, Any]]:
        return [
            item
            for item in parse_history(self.conversation(cid, umo=umo))
            if item.get("role") == "tool"
        ]

    def outbound_plain_texts(self) -> list[str]:
        return [
            message.chain[0].text
            for _umo, message in self.context.outbound
            if isinstance(message.chain[0], main.Comp.Plain)
        ]

    def task_statuses(
        self,
        cid: str,
        *,
        umo: str | None = None,
    ) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for item in parse_history(self.conversation(cid, umo=umo)):
            content = item.get("content")
            if item.get("role") == "_checkpoint" and isinstance(
                content,
                dict,
            ):
                checkpoint = dict(content)
                if "task_id" not in checkpoint and isinstance(
                    checkpoint.get("id"),
                    str,
                ):
                    checkpoint["task_id"] = checkpoint["id"]
                statuses.append(checkpoint)
                continue
            text = history_text(item)
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and isinstance(
                payload.get("task_id"),
                str,
            ):
                statuses.append(payload)
        return statuses

    async def test_tool_starts_immediately_and_rewrites_exact_tool_result(self) -> None:
        event = self.make_event("cid-1")
        heartbeat = asyncio.Event()

        async def pulse() -> None:
            await asyncio.sleep(0)
            heartbeat.set()

        async with fake_session_lock_manager.acquire_lock(
            event.unified_msg_origin,
        ):
            pulse_task = asyncio.create_task(pulse())
            result = await self.plugin.canvasforge_text_to_image(
                event,
                "draw a lighthouse",
            )
            payload = parse_status(result)
            self.assertTrue(payload["accepted"])
            self.assertEqual("generating", payload["state"])
            await wait_for(lambda: self.provider.calls == 1)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
            self.assertFalse(
                any(kind == "image" for kind in self.context.timeline),
            )

            self.stage_agent_history(
                event,
                result,
                tool_call_id="call-preserved",
            )
            self.provider.allow()
            await asyncio.sleep(0)
            self.assertFalse(
                any(kind == "image" for kind in self.context.timeline),
            )

        await pulse_task
        await wait_plugin_quiet(self.plugin)

        tool = self.tool_entries("cid-1")[-1]
        terminal = parse_status(tool["content"])
        self.assertEqual("call-preserved", tool["tool_call_id"])
        self.assertTrue(terminal["finished"])
        self.assertTrue(terminal["completed"])
        self.assertTrue(terminal["image_sent"])
        self.assertEqual(payload["task_id"], terminal["task_id"])

    async def test_completed_state_is_readable_while_completion_llm_blocks(self) -> None:
        self.context.block_llm()
        event = self.make_event("cid-1")
        _result, accepted = await self.invoke_and_stage(event)

        self.provider.allow()
        await wait_for(lambda: len(self.context.llm_calls) == 1)

        terminal = parse_status(self.tool_entries("cid-1")[-1]["content"])
        self.assertEqual(accepted["task_id"], terminal["task_id"])
        self.assertTrue(terminal["completed"])
        self.assertTrue(terminal["image_sent"])
        self.assertEqual(0, await self.plugin._request_gate.active_count())
        self.assertEqual(["image"], [
            kind for kind in self.context.timeline if kind in {"image", "plain"}
        ])

        self.context.release_llm()
        await wait_plugin_quiet(self.plugin)

    async def test_conversation_busy_and_global_capacity(self) -> None:
        first_event = self.make_event("cid-1")
        first_result = await self.plugin.canvasforge_text_to_image(
            first_event,
            "first",
        )
        first = parse_status(first_result)
        await wait_for(lambda: self.provider.calls == 1)

        duplicate = parse_status(
            await self.plugin.canvasforge_text_to_image(
                first_event,
                "duplicate",
            ),
        )
        self.assertFalse(duplicate["accepted"])
        self.assertEqual("generating", duplicate["state"])
        self.assertEqual("busy", duplicate["code"])
        self.assertEqual(first["task_id"], duplicate["task_id"])

        for index in (2, 3, 4):
            cid = f"cid-{index}"
            self.add_conversation(cid, umo=self.default_umo)

        second = parse_status(
            await self.plugin.canvasforge_text_to_image(
                self.make_event("cid-2"),
                "second",
            ),
        )
        third = parse_status(
            await self.plugin.canvasforge_text_to_image(
                self.make_event("cid-3"),
                "third",
            ),
        )
        fourth = parse_status(
            await self.plugin.canvasforge_text_to_image(
                self.make_event("cid-4"),
                "fourth",
            ),
        )
        self.assertTrue(second["accepted"])
        self.assertTrue(third["accepted"])
        self.assertFalse(fourth["accepted"])
        self.assertEqual("busy", fourth["code"])
        self.assertNotIn("task_id", fourth)
        await wait_for(lambda: self.provider.calls == 3)
        self.assertEqual(3, await self.plugin._request_gate.active_count())

        self.provider.allow()
        await wait_plugin_quiet(self.plugin)

    async def test_failure_terminal_precedes_ai_and_new_user_does_not_suppress_it(self) -> None:
        self.context.block_llm()
        self.provider.failure = main.CanvasForgeError(
            main.ErrorCode.TIMEOUT,
            "safe provider timeout",
        )
        event = self.make_event("cid-1")
        _result, accepted = await self.invoke_and_stage(event)
        self.provider.allow()

        await wait_for(lambda: len(self.context.llm_calls) == 1)
        terminal = parse_status(self.tool_entries("cid-1")[-1]["content"])
        self.assertEqual(accepted["task_id"], terminal["task_id"])
        self.assertTrue(terminal["failed"])
        self.assertFalse(terminal["completed"])
        self.assertEqual("timeout", terminal["code"])
        self.assertLess(
            self.context.timeline.index("history"),
            self.context.timeline.index("llm"),
        )

        conversation = self.conversation("cid-1")
        history = parse_history(conversation)
        history.append({"role": "user", "content": "is it done?"})
        conversation.history = json.dumps(history, ensure_ascii=False)
        self.context.release_llm()
        await wait_plugin_quiet(self.plugin)

        self.assertEqual(1, len(self.outbound_plain_texts()))
        saved = parse_history(conversation)
        self.assertEqual("assistant", saved[-1]["role"])
        self.assertIn("图片已经画好啦", history_text(saved[-1]))

    async def test_success_notice_is_skipped_after_a_new_user_turn(self) -> None:
        self.context.block_llm()
        event = self.make_event("cid-1")
        await self.invoke_and_stage(event)
        self.provider.allow()
        await wait_for(lambda: len(self.context.llm_calls) == 1)

        conversation = self.conversation("cid-1")
        history = parse_history(conversation)
        history.append({"role": "user", "content": "next question"})
        conversation.history = json.dumps(history, ensure_ascii=False)
        self.context.release_llm()
        await wait_plugin_quiet(self.plugin)

        self.assertEqual([], self.outbound_plain_texts())
        saved = parse_history(conversation)
        self.assertEqual("user", saved[-1]["role"])
        self.assertTrue(parse_status(self.tool_entries("cid-1")[-1]["content"])["completed"])

    async def test_notification_contract_preserves_multiline_persona_text(self) -> None:
        self.context.llm_completion = "  完成啦！\n快来看看。  "
        event = self.make_event("cid-1")
        await self.invoke_and_stage(event)
        self.provider.allow()
        await wait_plugin_quiet(self.plugin)

        self.assertEqual(["完成啦！\n快来看看。"], self.outbound_plain_texts())
        self.assertEqual(1, len(self.context.llm_calls))
        call = self.context.llm_calls[0]
        self.assertIsNone(call["tools"])
        self.assertEqual(1, call["request_max_retries"])
        self.assertEqual("chat-provider", call["chat_provider_id"])
        self.assertEqual("chat-model", call["model"])
        self.assertNotIn("image", call)
        self.assertNotIn("images", call)
        self.assertNotIn("image_urls", call)

    async def test_notification_exception_empty_and_timeout_use_fallback(self) -> None:
        cases = ("exception", "empty", "timeout")
        for index, case in enumerate(cases, start=2):
            with self.subTest(case=case):
                cid = f"cid-{index}"
                self.add_conversation(cid, umo=self.default_umo)
                self.context.llm_error = None
                self.context.llm_completion = ""
                self.context.llm_release.set()
                if case == "exception":
                    self.context.llm_error = RuntimeError("chat model failed")
                elif case == "timeout":
                    main._COMPLETION_TIMEOUT_SECONDS = 0.01
                    self.context.block_llm()

                before = len(self.outbound_plain_texts())
                await self.invoke_and_stage(self.make_event(cid))
                self.provider.allow()
                await wait_plugin_quiet(self.plugin)
                self.assertEqual(
                    "图片已生成并发送。",
                    self.outbound_plain_texts()[before],
                )
                if case == "timeout":
                    self.context.release_llm()
                    main._COMPLETION_TIMEOUT_SECONDS = (
                        self.original_completion_timeout
                    )

    async def test_image_send_failure_does_not_commit_cooldown(self) -> None:
        self.config["advanced"]["cooldown_seconds"] = 60
        self.context.send_results["image"] = False
        event = self.make_event("cid-1", sender_id="same-user")
        await self.invoke_and_stage(event)
        self.provider.allow()
        await wait_plugin_quiet(self.plugin)

        terminal = parse_status(self.tool_entries("cid-1")[-1]["content"])
        self.assertTrue(terminal["failed"])
        self.assertEqual("send_failed", terminal["code"])

        self.add_conversation("cid-2", umo=self.default_umo)
        retry = parse_status(
            await self.plugin.canvasforge_text_to_image(
                self.make_event("cid-2", sender_id="same-user"),
                "retry in another conversation",
            ),
        )
        self.assertTrue(retry["accepted"])
        self.context.send_results["image"] = True
        await wait_plugin_quiet(self.plugin)

    async def test_success_cooldown_follows_user_across_conversations(self) -> None:
        self.config["advanced"]["cooldown_seconds"] = 60
        event = self.make_event("cid-1", sender_id="same-user")
        await self.invoke_and_stage(event)
        self.provider.allow()
        await wait_plugin_quiet(self.plugin)

        self.add_conversation("cid-2", umo=self.default_umo)
        result = parse_status(
            await self.plugin.canvasforge_text_to_image(
                self.make_event("cid-2", sender_id="same-user"),
                "another conversation",
            ),
        )
        self.assertFalse(result["accepted"])
        self.assertEqual("cooldown", result["code"])

    async def test_command_wait_send_controls_start_and_auto_mode(self) -> None:
        failed = self.make_event(
            "cid-1",
            raw_message="/canvasforge draw a moon",
            send_fails=True,
        )
        await self.plugin.canvasforge_command(failed)
        await asyncio.sleep(0)
        self.assertEqual(0, self.provider.calls)
        self.assertEqual(0, await self.plugin._request_gate.active_count())

        self.add_conversation("cid-2", umo=self.default_umo)
        succeeded = self.make_event(
            "cid-2",
            raw_message="/canvasforge restyle this",
            reference_sources=("https://example.test/reference.png",),
        )
        await self.plugin.canvasforge_command(succeeded)
        self.assertTrue(succeeded.sent_text)
        await wait_for(lambda: self.provider.calls == 1)
        self.assertEqual("edit", self.provider.modes[-1])
        self.assertLess(
            self.context.timeline.index("command-plain"),
            self.context.timeline.index("provider"),
        )

        self.provider.allow()
        await wait_plugin_quiet(self.plugin)

    async def test_command_empty_history_records_anchor_before_provider(self) -> None:
        cid = "cid-empty-command"
        self.add_conversation(
            cid,
            umo=self.default_umo,
            history=[],
        )
        event = self.make_event(
            cid,
            raw_message="/canvasforge draw a moonlit lake",
        )

        await self.plugin.canvasforge_command(event)
        await wait_for(lambda: self.provider.calls == 1)

        generating = [
            status
            for status in self.task_statuses(cid)
            if status.get("state") == "generating"
            and status.get("finished") is False
        ]
        self.assertEqual(1, len(generating))
        task_id = generating[0]["task_id"]
        self.assertLess(
            self.context.timeline.index("command-plain"),
            self.context.timeline.index("history"),
        )
        self.assertLess(
            self.context.timeline.index("history"),
            self.context.timeline.index("provider"),
        )

        self.provider.allow()
        await wait_plugin_quiet(self.plugin)

        terminal = [
            status
            for status in self.task_statuses(cid)
            if status.get("task_id") == task_id
            and status.get("finished") is True
        ]
        self.assertTrue(terminal)
        self.assertTrue(terminal[-1]["completed"])
        self.assertTrue(terminal[-1]["image_sent"])
        notices = self.outbound_plain_texts()
        self.assertEqual(1, len(notices))
        saved = parse_history(self.conversation(cid))
        self.assertEqual("assistant", saved[-1]["role"])
        self.assertEqual(notices[-1], history_text(saved[-1]))

    async def test_validation_failure_can_be_corrected_in_same_agent_turn(
        self,
    ) -> None:
        event = self.make_event("cid-1")
        missing_reference = parse_status(
            await self.plugin.canvasforge_image_to_image(
                event,
                "edit without a reference",
                [],
            ),
        )
        self.assertFalse(missing_reference["accepted"])
        self.assertEqual("reference_required", missing_reference["code"])
        self.assertEqual(0, await self.plugin._request_gate.active_count())

        async with fake_session_lock_manager.acquire_lock(
            event.unified_msg_origin,
        ):
            corrected_result = await self.plugin.canvasforge_text_to_image(
                event,
                "create it from text instead",
            )
            corrected = parse_status(corrected_result)
            self.assertTrue(corrected["accepted"])
            self.stage_agent_history(event, corrected_result)

        self.provider.allow()
        await wait_plugin_quiet(self.plugin)
        self.assertEqual(1, self.provider.calls)
        self.assertTrue(self.task_statuses("cid-1")[-1]["completed"])

    async def test_terminal_history_retries_silent_noops_and_preserves_tool_metadata(self) -> None:
        event = self.make_event("cid-1")
        job = await self.plugin._prepare_generation_job(
            event,
            "prepared only",
            requested_mode="generate",
            from_llm_tool=True,
        )
        pending = self.plugin._status_json(
            accepted=True,
            state="generating",
            finished=False,
            completed=False,
            task_id=job.task_id,
        )
        self.stage_agent_history(
            event,
            pending,
            tool_call_id="metadata-survives",
        )
        terminal = self.plugin._terminal_status(
            job,
            completed=True,
            failed=False,
            image_sent=True,
        )
        manager = self.context.conversation_manager
        manager.silent_update_noops = 2
        before = len(manager.update_calls)
        async with fake_session_lock_manager.acquire_lock(self.default_umo):
            checkpoint = await self.plugin._persist_terminal_state_locked(
                job,
                terminal,
            )
        self.assertIsNotNone(checkpoint)
        self.assertEqual(3, len(manager.update_calls) - before)
        tool = self.tool_entries("cid-1")[-1]
        self.assertEqual("metadata-survives", tool["tool_call_id"])
        self.assertEqual(terminal, tool["content"])
        await job.lease.release()

    async def test_missing_tool_result_appends_terminal_to_valid_history(
        self,
    ) -> None:
        event = self.make_event("cid-1")
        job = await self.plugin._prepare_generation_job(
            event,
            "prepared without a saved tool result",
            requested_mode="generate",
            from_llm_tool=True,
        )
        terminal = self.plugin._terminal_status(
            job,
            completed=True,
            failed=False,
            image_sent=True,
        )
        async with fake_session_lock_manager.acquire_lock(self.default_umo):
            checkpoint = await self.plugin._persist_terminal_state_locked(
                job,
                terminal,
            )

        self.assertIsNotNone(checkpoint)
        self.assertTrue(checkpoint.verified)
        self.assertEqual(
            terminal,
            history_text(parse_history(self.conversation("cid-1"))[-1]),
        )
        await job.lease.release()

    async def test_terminal_history_gives_up_after_three_silent_noops(self) -> None:
        event = self.make_event("cid-1")
        job = await self.plugin._prepare_generation_job(
            event,
            "prepared only",
            requested_mode="generate",
            from_llm_tool=True,
        )
        pending = self.plugin._status_json(
            accepted=True,
            state="generating",
            finished=False,
            completed=False,
            task_id=job.task_id,
        )
        self.stage_agent_history(event, pending)
        terminal = self.plugin._terminal_status(
            job,
            completed=False,
            failed=True,
            image_sent=False,
            code="timeout",
            reason="safe timeout",
        )
        manager = self.context.conversation_manager
        manager.silent_update_noops = 3
        before = len(manager.update_calls)
        async with fake_session_lock_manager.acquire_lock(self.default_umo):
            checkpoint = await self.plugin._persist_terminal_state_locked(
                job,
                terminal,
            )
        self.assertIsNotNone(checkpoint)
        self.assertFalse(checkpoint.verified)
        self.assertEqual(3, len(manager.update_calls) - before)
        self.assertEqual(pending, self.tool_entries("cid-1")[-1]["content"])
        await job.lease.release()

    async def test_unverified_terminal_uses_fixed_notice_without_llm(
        self,
    ) -> None:
        event = self.make_event("cid-1")
        async with fake_session_lock_manager.acquire_lock(
            event.unified_msg_origin,
        ):
            result = await self.plugin.canvasforge_text_to_image(
                event,
                "draw a storage failure test",
            )
            pending = parse_status(result)
            self.assertTrue(pending["accepted"])
            self.stage_agent_history(event, result)

        self.context.conversation_manager.silent_update_noops = 3
        self.provider.allow()
        await wait_plugin_quiet(self.plugin)

        self.assertEqual([], self.context.llm_calls)
        self.assertIn(
            "任务状态未能写入会话记录",
            self.outbound_plain_texts()[-1],
        )
        tool = self.tool_entries("cid-1")[-1]
        self.assertFalse(parse_status(tool["content"])["finished"])

    async def test_terminal_fast_path_detects_user_after_task_anchor(self) -> None:
        event = self.make_event("cid-1")
        job = await self.plugin._prepare_generation_job(
            event,
            "prepared only",
            requested_mode="generate",
            from_llm_tool=True,
        )
        pending = self.plugin._status_json(
            accepted=True,
            state="generating",
            finished=False,
            completed=False,
            task_id=job.task_id,
        )
        self.stage_agent_history(event, pending)
        terminal = self.plugin._terminal_status(
            job,
            completed=True,
            failed=False,
            image_sent=True,
        )
        conversation = self.conversation("cid-1")
        history = parse_history(conversation)
        tool_index = next(
            index
            for index, item in enumerate(history)
            if item.get("role") == "tool"
        )
        history[tool_index]["content"] = terminal
        history.append({"role": "user", "content": "a newer request"})
        conversation.history = json.dumps(history, ensure_ascii=False)

        try:
            async with fake_session_lock_manager.acquire_lock(
                self.default_umo,
            ):
                checkpoint = await self.plugin._persist_terminal_state_locked(
                    job,
                    terminal,
                )
            self.assertIsNotNone(checkpoint)
            self.assertTrue(checkpoint.newer_user_already_present)
            await self.plugin._run_notification(
                job,
                success=True,
                error=None,
                checkpoint=checkpoint,
            )
            self.assertEqual([], self.context.llm_calls)
            self.assertEqual([], self.outbound_plain_texts())
        finally:
            await job.lease.release()

    async def test_success_notice_skips_on_authoritative_read_errors(self) -> None:
        manager = self.context.conversation_manager
        cases = (
            ("current_read_error", "cid-current-read-error"),
            ("conversation_read_error", "cid-history-read-error"),
        )
        for attribute, cid in cases:
            with self.subTest(read=attribute):
                self.add_conversation(cid, umo=self.default_umo)
                event = self.make_event(cid)
                job = await self.plugin._prepare_generation_job(
                    event,
                    "prepared only",
                    requested_mode="generate",
                    from_llm_tool=True,
                )
                pending = self.plugin._status_json(
                    accepted=True,
                    state="generating",
                    finished=False,
                    completed=False,
                    task_id=job.task_id,
                )
                self.stage_agent_history(event, pending)
                terminal = self.plugin._terminal_status(
                    job,
                    completed=True,
                    failed=False,
                    image_sent=True,
                )
                async with fake_session_lock_manager.acquire_lock(
                    self.default_umo,
                ):
                    checkpoint = (
                        await self.plugin._persist_terminal_state_locked(
                            job,
                            terminal,
                        )
                    )
                self.assertIsNotNone(checkpoint)
                before_llm = len(self.context.llm_calls)
                before_plain = len(self.outbound_plain_texts())
                setattr(
                    manager,
                    attribute,
                    RuntimeError("simulated authoritative read failure"),
                )
                try:
                    await self.plugin._run_notification(
                        job,
                        success=True,
                        error=None,
                        checkpoint=checkpoint,
                    )
                finally:
                    setattr(manager, attribute, None)
                    await job.lease.release()
                self.assertEqual(before_llm, len(self.context.llm_calls))
                self.assertEqual(
                    before_plain,
                    len(self.outbound_plain_texts()),
                )

    async def test_reset_and_delete_never_pollute_replacement_history(self) -> None:
        event = self.make_event("cid-1")
        await self.invoke_and_stage(event)
        reset_history = [{"role": "user", "content": "fresh reset"}]
        self.conversation("cid-1").history = json.dumps(
            reset_history,
            ensure_ascii=False,
        )
        self.provider.allow()
        await wait_plugin_quiet(self.plugin)
        self.assertEqual(reset_history, parse_history(self.conversation("cid-1")))

        self.provider.block()
        self.add_conversation("cid-old", umo=self.default_umo)
        old_event = self.make_event("cid-old")
        await self.invoke_and_stage(old_event)
        manager = self.context.conversation_manager
        manager.delete(self.default_umo, "cid-old")
        replacement_history = [{"role": "user", "content": "new chat"}]
        self.add_conversation(
            "cid-new",
            umo=self.default_umo,
            history=replacement_history,
        )
        self.provider.allow()
        await wait_plugin_quiet(self.plugin)

        self.assertNotIn(
            (self.default_umo, "cid-old"),
            manager.conversations,
        )
        self.assertEqual(
            replacement_history,
            parse_history(self.conversation("cid-new")),
        )

    async def test_terminate_cancels_worker_and_notification_before_session_close(self) -> None:
        self.provider.allow()
        self.context.block_llm()
        await self.invoke_and_stage(self.make_event("cid-1"))
        await wait_for(lambda: len(self.context.llm_calls) == 1)

        self.provider.block()
        self.add_conversation("cid-2", umo=self.default_umo)
        await self.invoke_and_stage(self.make_event("cid-2"))
        await wait_for(lambda: self.provider.calls == 2)

        session = FakeSession(self.provider, self.context)
        self.plugin._session = session
        await asyncio.wait_for(self.plugin.terminate(), timeout=1)

        self.assertGreaterEqual(self.provider.cancelled, 1)
        self.assertGreaterEqual(self.context.llm_cancelled, 1)
        self.assertTrue(session.closed)
        self.assertTrue(session.closed_after_cancellations)
        self.assertFalse(self.plugin._generation_tasks)
        self.assertFalse(self.plugin._notification_tasks)
        self.assertEqual(0, await self.plugin._request_gate.active_count())

    async def test_terminate_does_not_wait_forever_for_stuck_active_send(self) -> None:
        main._ACTIVE_SEND_TIMEOUT_SECONDS = 0.02
        self.context.blocked_send_kinds.add("image")
        await self.invoke_and_stage(self.make_event("cid-1"))
        self.provider.allow()
        await asyncio.wait_for(self.context.send_started.wait(), timeout=0.5)

        session = FakeSession(self.provider, self.context)
        self.plugin._session = session
        termination = asyncio.create_task(self.plugin.terminate())
        done, _pending = await asyncio.wait({termination}, timeout=0.3)
        if termination not in done:
            self.context.send_release.set()
            await asyncio.wait_for(termination, timeout=1)
            self.fail("terminate waited indefinitely for Context.send_message")

        await termination
        self.assertTrue(session.closed)
        self.assertGreaterEqual(self.context.send_cancelled, 1)
        self.assertFalse(self.plugin._generation_tasks)
        self.assertFalse(self.plugin._notification_tasks)
        self.assertEqual(0, await self.plugin._request_gate.active_count())


if __name__ == "__main__":
    unittest.main()
