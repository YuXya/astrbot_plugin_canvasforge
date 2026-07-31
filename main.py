"""CanvasForge AstrBot plugin entry point."""

from __future__ import annotations

import asyncio
import copy
import contextvars
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    bind_checkpoint_messages,
)
from astrbot.core.utils.session_lock import session_lock_manager

from .canvasforge.avatar import AvatarResolver, AvatarTarget, ResolvedAvatar
from .canvasforge.cache import CacheError, CacheStore
from .canvasforge.contracts import (
    CanvasForgeError,
    ErrorCode,
    GeneratedImage,
    HttpKeyProviderConfig,
    ImageProviderFactory,
    ImageRequestOptions,
)
from .canvasforge.delivery import prepare_qq_delivery_bytes
from .canvasforge.provider import (
    Sub2APIImagesProviderFactory,
)
from .canvasforge.rate_limit import RequestGate, RequestLease
from .canvasforge.reference import (
    DEFAULT_PER_IMAGE_BYTES,
    ReferenceSnapshot,
    ReferenceResolver,
    build_source_metadata,
)
from .canvasforge.web_api import WebAPI, normalize_settings


PLUGIN_NAME = "astrbot_plugin_canvasforge"
PLUGIN_AUTHOR = "YuXya"
PLUGIN_VERSION = "v0.1.11"
PLUGIN_REPOSITORY = "https://github.com/YuXya/astrbot_plugin_canvasforge"
PLUGIN_DESCRIPTION = (
    "通过 Sub2API 调用 GPT Images，为 NapCat QQ 提供文生图与引用图编辑能力。"
)
MIB = 1024 * 1024
_LEGACY_UPDATE_CONTEXT_KEY = "_canvasforge_update_runtime_v1"
_LEGACY_ADMISSION_CONTEXT_KEY = "_canvasforge_admission_runtime_v2"
_LEGACY_UPDATE_STATUS_FILENAMES = (
    "update-status.json",
    "update-status.json.tmp",
)
_TEXT_TO_IMAGE_TOOL_NAME = "canvasforge_text_to_image"
_IMAGE_TO_IMAGE_TOOL_NAME = "canvasforge_image_to_image"
_LLM_TOOL_NAMES = (
    _TEXT_TO_IMAGE_TOOL_NAME,
    _IMAGE_TO_IMAGE_TOOL_NAME,
)
_DEFAULT_COMPLETION_MESSAGE = "图片已生成并发送。"
_COMMAND_PENDING_MESSAGE = "CanvasForge 任务已受理，正在生成，请稍等。"
_COMPLETION_TIMEOUT_SECONDS = 20
_ACTIVE_SEND_TIMEOUT_SECONDS = 30
_CONVERSATION_ATTEMPT_TIMEOUT_SECONDS = 5
_SHUTDOWN_TASK_TIMEOUT_SECONDS = 50
_TERMINAL_HISTORY_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class _ConversationContext:
    """Immutable chat context captured before the original Agent finishes."""

    unified_origin: str
    conversation_id: str | None
    provider_id: str | None
    model: str | None
    system_prompt: str = field(repr=False)
    contexts: tuple[dict[str, Any], ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _GenerationJob:
    """One immutable, leased background request.

    User content, connection settings, the event and the lease are excluded
    from ``repr`` so diagnostics cannot accidentally disclose them.
    """

    task_id: str
    event: AstrMessageEvent = field(repr=False)
    prompt: str = field(repr=False)
    requested_mode: str
    reference_snapshot: ReferenceSnapshot = field(repr=False)
    planned_avatars: tuple[AvatarTarget, ...] = field(repr=False)
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    settings: Mapping[str, Any] = field(repr=False)
    lease: RequestLease = field(repr=False)
    conversation: _ConversationContext = field(repr=False)
    from_llm_tool: bool


@dataclass(frozen=True, slots=True)
class _TerminalCheckpoint:
    """Verified terminal-history position used to detect a newer user turn."""

    user_message_count: int
    newer_user_already_present: bool = False
    verified: bool = True


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESCRIPTION,
    PLUGIN_VERSION,
    PLUGIN_REPOSITORY,
)
class CanvasForgePlugin(Star):
    """在 NapCat QQ 中生成图片，或用引用图片及人物头像进行编辑。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self._runtime_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._closing = False
        self._generation_tasks: set[asyncio.Task[None]] = set()
        self._generation_leases: dict[
            asyncio.Task[None],
            RequestLease,
        ] = {}
        self._generation_jobs: dict[
            asyncio.Task[None],
            _GenerationJob,
        ] = {}
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._bounded_io_tasks: set[asyncio.Task[Any]] = set()
        self._lease_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._termination_task: asyncio.Task[None] | None = None
        self._legacy_cleanup_task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._provider_factory: ImageProviderFactory | None = None
        self._reference_resolver: ReferenceResolver | None = None
        self._avatar_resolver: AvatarResolver | None = None
        self._request_gate = RequestGate()

        data_root = StarTools.get_data_dir(PLUGIN_NAME)
        self._data_root = data_root
        cache_root = data_root / "cache"
        self._cache = CacheStore(cache_root)
        self._cache_ready = False
        self._web_api = WebAPI(
            context,
            self._cache,
            self._get_advanced_settings,
            self._save_advanced_settings,
            PLUGIN_VERSION,
        )
        self._web_api.register()

    async def initialize(self) -> None:
        """Create long-lived resources without requiring a configured Key."""

        self._configure_llm_tool_schemas()
        await self._start_legacy_update_cleanup()
        await self._ensure_runtime()
        settings = await self._get_advanced_settings()
        try:
            await self._cache.initialize(settings["cache_max_images"])
            self._cache_ready = True
        except CacheError as exc:
            self._cache_ready = False
            logger.error(
                "CanvasForge cache initialization failed (%s); generation "
                "will continue without caching.",
                type(exc).__name__,
            )

    @filter.llm_tool(name=_TEXT_TO_IMAGE_TOOL_NAME)
    async def canvasforge_text_to_image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
    ) -> str:
        """异步文生图：根据文字提示从零生成图片，不使用消息图片或聊天头像。

        适合不需要参考图的创作；需要基于当前消息附图、直接回复图或聊天参与者
        头像进行创作时，通常使用 canvasforge_image_to_image。请提交完整提示词。
        返回 state=generating 表示后台正在处理，结果稍后由 CanvasForge 发送。

        Args:
            prompt(string): 当前聊天 AI 编写的完整文生图提示词。
        """

        return await self._run_llm_tool(
            event,
            prompt,
            requested_mode="generate",
            avatar_targets=None,
        )

    @filter.llm_tool(name=_IMAGE_TO_IMAGE_TOOL_NAME)
    async def canvasforge_image_to_image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        avatar_targets: list[str] | None = None,
    ) -> str:
        """异步图生图：基于消息图片或所选聊天头像生成、修改图片。

        参考图依次来自当前消息附图、直接回复图片和 avatar_targets。头像选择器
        sender 表示发送者，bot 表示机器人，mention:N 表示当前消息第 N 个有效
        直接 @；不需要头像时可省略。至少需要一张参考图，否则返回
        reference_required。参考图用于保持人物身份与整体外貌，需要改变的内容写入
        完整 prompt。返回 state=generating 表示
        后台正在处理，结果稍后由 CanvasForge 发送。

        Args:
            prompt(string): 当前聊天 AI 编写的完整图生图提示词。
            avatar_targets(array[string]): 可选；需要聊天头像时按 sender、bot、mention:N 填写。
        """

        return await self._run_llm_tool(
            event,
            prompt,
            requested_mode="edit",
            avatar_targets=avatar_targets,
        )

    async def _run_llm_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        requested_mode: str,
        avatar_targets: list[str] | None,
    ) -> str:
        """Run either public LLM tool with common error formatting."""

        job: _GenerationJob | None = None
        handed_off = False
        try:
            job = await self._prepare_generation_job(
                event,
                prompt,
                avatar_targets=avatar_targets,
                requested_mode=requested_mode,
                from_llm_tool=True,
            )
            await self._start_generation_task(job)
            handed_off = True
        except CanvasForgeError as exc:
            if exc.code is ErrorCode.BUSY and exc.task_id:
                return self._status_json(
                    accepted=False,
                    state="generating",
                    code=exc.code.value,
                    task_id=exc.task_id,
                    reason="当前会话已有图片正在生成。",
                )
            return self._status_json(
                accepted=False,
                state="idle",
                finished=True,
                completed=False,
                failed=True,
                code=exc.code.value,
                reason=str(exc),
            )
        except Exception as exc:
            logger.error(
                "CanvasForge tool failed unexpectedly (%s).",
                type(exc).__name__,
            )
            return self._status_json(
                accepted=False,
                state="idle",
                finished=True,
                completed=False,
                failed=True,
                code=ErrorCode.INTERNAL.value,
                reason="CanvasForge 处理请求时发生内部错误，请稍后再试。",
            )
        finally:
            if (
                job is not None
                and not handed_off
                and not job.lease.finished
            ):
                await job.lease.release()

        return self._status_json(
            accepted=True,
            state="generating",
            finished=False,
            completed=False,
            task_id=job.task_id,
        )

    @filter.command("canvasforge")
    async def canvasforge_command(self, event: AstrMessageEvent) -> None:
        """直接使用命令后的提示词生成或编辑一张图片。"""

        event.stop_event()
        raw_message = event.get_message_str().strip()
        parts = raw_message.split(maxsplit=1)
        prompt = parts[1].strip() if len(parts) == 2 else ""

        if not prompt:
            await self._send_command_text(
                event,
                "用法：/canvasforge <提示词>",
            )
            return

        job: _GenerationJob | None = None
        handed_off = False
        try:
            job = await self._prepare_generation_job(
                event,
                prompt,
                from_llm_tool=False,
            )
        except CanvasForgeError as exc:
            await self._send_command_text(event, str(exc))
            return
        except Exception as exc:
            logger.error(
                "CanvasForge command preflight failed unexpectedly (%s).",
                type(exc).__name__,
            )
            await self._send_command_text(
                event,
                "CanvasForge 处理请求时发生内部错误，请稍后再试。",
            )
            return

        try:
            accepted = await self._send_command_text(
                event,
                _COMMAND_PENDING_MESSAGE,
            )
            if not accepted:
                return
            if not await self._persist_command_admission(job):
                raise CanvasForgeError(
                    ErrorCode.INTERNAL,
                    "CanvasForge 任务未能写入原会话，请重新发起。",
                )
            await self._start_generation_task(job)
            handed_off = True
        except asyncio.CancelledError:
            raise
        except CanvasForgeError as exc:
            await self._send_command_text(event, str(exc))
        except Exception as exc:
            logger.error(
                "CanvasForge command handoff failed unexpectedly (%s).",
                type(exc).__name__,
            )
            await self._send_command_text(
                event,
                "CanvasForge 后台任务未能启动，请稍后再试。",
            )
        finally:
            if not handed_off and not job.lease.finished:
                await job.lease.release()

    async def _prepare_generation_job(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        avatar_targets: list[str] | None = None,
        requested_mode: str | None = None,
        from_llm_tool: bool = False,
    ) -> _GenerationJob:
        """Validate locally, freeze request context and reserve the slot."""

        if requested_mode not in (None, "generate", "edit"):
            raise CanvasForgeError(ErrorCode.INTERNAL)
        if event.get_platform_name() != "aiocqhttp":
            raise CanvasForgeError(ErrorCode.PLATFORM_UNSUPPORTED)

        base_url, api_key, settings = await self._configuration_snapshot()
        is_admin = bool(event.is_admin())
        if settings["admin_only_generation"] and not is_admin:
            raise CanvasForgeError(ErrorCode.ADMIN_ONLY)
        normalized_prompt = self._validate_prompt(
            prompt,
            settings["max_prompt_chars"],
        )
        if not base_url or not api_key:
            raise CanvasForgeError(ErrorCode.NOT_CONFIGURED)
        if self._closing:
            raise CanvasForgeError(
                ErrorCode.BUSY,
                "CanvasForge 正在关闭，暂时不能接收新的生图任务。",
            )
        if avatar_targets is not None and not isinstance(
            avatar_targets,
            list,
        ):
            raise CanvasForgeError(ErrorCode.AVATAR_TARGET_INVALID)
        conversation = await self._capture_conversation_context(
            event,
            prefer_authoritative_history=not from_llm_tool,
        )
        task_id = f"cf_{secrets.token_hex(6)}"
        conversation_key = self._conversation_key(conversation)
        user_id = self._event_string(event, "get_sender_id")
        if not user_id:
            user_id = self._event_string(event, "get_session_id") or "unknown"

        lease: RequestLease | None = None
        async with self._lifecycle_lock:
            if self._closing:
                raise CanvasForgeError(
                    ErrorCode.BUSY,
                    "CanvasForge 正在关闭，暂时不能接收新的生图任务。",
                )
            lease = await self._acquire_generation_lease(
                user_id,
                conversation_key=conversation_key,
                task_id=task_id,
                max_concurrent=settings["max_concurrent_generations"],
                is_admin=is_admin,
                cooldown_seconds=settings["cooldown_seconds"],
            )

        try:
            (
                _provider_factory,
                reference_resolver,
                avatar_resolver,
            ) = await self._ensure_runtime()
            normalized_avatar_targets = avatar_targets or []
            if requested_mode in (None, "edit"):
                if (
                    normalized_avatar_targets
                    and not settings["enable_avatar_references"]
                ):
                    raise CanvasForgeError(ErrorCode.AVATAR_DISABLED)
                planned_avatars = avatar_resolver.plan(
                    event,
                    normalized_avatar_targets,
                )
                reference_snapshot = await reference_resolver.snapshot_all(event)
            else:
                # Text-to-image deliberately ignores every message image.
                planned_avatars = []
                reference_snapshot = ReferenceSnapshot(())

            has_message_references = bool(reference_snapshot.sources)
            if requested_mode == "edit":
                if not has_message_references and not planned_avatars:
                    raise CanvasForgeError(ErrorCode.REFERENCE_REQUIRED)
                resolved_mode = "edit"
            elif requested_mode == "generate":
                resolved_mode = "generate"
            else:
                resolved_mode = (
                    "edit" if has_message_references else "generate"
                )

            if (
                len(reference_snapshot.sources) + len(planned_avatars)
                > settings["max_reference_images"]
            ):
                raise CanvasForgeError(
                    ErrorCode.REFERENCE_LIMIT,
                    "消息图片与人物头像的合计数量超过当前限制，请减少后重试。",
                )
        except BaseException:
            if lease is not None and not lease.finished:
                await lease.release()
            raise

        return _GenerationJob(
            task_id=task_id,
            event=event,
            prompt=normalized_prompt,
            requested_mode=resolved_mode,
            reference_snapshot=reference_snapshot,
            planned_avatars=tuple(planned_avatars),
            base_url=base_url,
            api_key=api_key,
            settings=MappingProxyType(dict(settings)),
            lease=lease,
            conversation=conversation,
            from_llm_tool=from_llm_tool,
        )

    async def _persist_command_admission(self, job: _GenerationJob) -> bool:
        """Anchor a command task in its original conversation before work.

        LLM tools acquire their anchor when AstrBot saves the tool result.
        Commands have no tool result, so an explicit generating state is
        required, including for a brand-new conversation with empty history.
        """

        conversation_id = job.conversation.conversation_id
        manager = getattr(self.context, "conversation_manager", None)
        if not conversation_id or manager is None:
            # Some platform-only command contexts have no AstrBot
            # conversation. They can still receive image/result messages.
            return True

        admission = list(job.conversation.contexts)
        generating_status = self._status_json(
            accepted=True,
            state="generating",
            finished=False,
            completed=False,
            task_id=job.task_id,
        )
        async with session_lock_manager.acquire_lock(
            job.conversation.unified_origin,
        ):
            for attempt in range(1, _TERMINAL_HISTORY_ATTEMPTS + 1):
                try:
                    finished, admitted = await self._await_bounded_operation(
                        self._persist_command_admission_once(
                            job,
                            manager,
                            conversation_id,
                            admission,
                            generating_status,
                        ),
                        timeout_seconds=(
                            _CONVERSATION_ATTEMPT_TIMEOUT_SECONDS
                        ),
                    )
                    if finished:
                        return admitted
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "CanvasForge could not persist command admission "
                        "on attempt %d/%d (%s).",
                        attempt,
                        _TERMINAL_HISTORY_ATTEMPTS,
                        type(exc).__name__,
                    )
                await asyncio.sleep(0)

        logger.error(
            "CanvasForge command admission could not be verified after "
            "%d attempts.",
            _TERMINAL_HISTORY_ATTEMPTS,
        )
        return False

    async def _persist_command_admission_once(
        self,
        job: _GenerationJob,
        manager: Any,
        conversation_id: str,
        admission: list[dict[str, Any]],
        generating_status: str,
    ) -> tuple[bool, bool]:
        current_id = await manager.get_curr_conversation_id(
            job.conversation.unified_origin,
        )
        if current_id and current_id != conversation_id:
            return True, False
        conversation = await manager.get_conversation(
            job.conversation.unified_origin,
            conversation_id,
            create_if_not_exists=False,
        )
        if conversation is None:
            return True, False
        history = self._parse_history(getattr(conversation, "history", ""))
        if self._history_contains_task_id(history, job.task_id):
            return True, True
        if history[: len(admission)] != admission:
            return True, False
        history.append(
            AssistantMessageSegment(
                content=[TextPart(text=generating_status)],
            ).model_dump(),
        )
        await manager.update_conversation(
            job.conversation.unified_origin,
            conversation_id=conversation_id,
            history=history,
            token_usage=0,
        )
        verified = await manager.get_conversation(
            job.conversation.unified_origin,
            conversation_id,
            create_if_not_exists=False,
        )
        if verified is None:
            return True, False
        verified_history = self._parse_history(
            getattr(verified, "history", ""),
        )
        if self._history_contains_task_id(verified_history, job.task_id):
            return True, True
        return False, False

    async def _start_generation_task(self, job: _GenerationJob) -> None:
        """Atomically hand one leased job to a strongly held background task."""

        task: asyncio.Task[None] | None = None
        creation_error: Exception | None = None
        async with self._lifecycle_lock:
            if not self._closing:
                operation = self._run_background_generation(job)
                try:
                    task = asyncio.create_task(
                        operation,
                        name="canvasforge-generation",
                    )
                except Exception as exc:
                    operation.close()
                    creation_error = exc
                else:
                    self._generation_tasks.add(task)
                    self._generation_leases[task] = job.lease
                    self._generation_jobs[task] = job
                    task.add_done_callback(self._generation_task_done)

        if task is not None:
            return

        if not job.lease.finished:
            await job.lease.release()
        if creation_error is not None:
            logger.error(
                "CanvasForge could not create a background task (%s).",
                type(creation_error).__name__,
            )
            raise CanvasForgeError(
                ErrorCode.INTERNAL,
                "CanvasForge 后台任务未能启动，请稍后再试。",
            )
        raise CanvasForgeError(
            ErrorCode.BUSY,
            "CanvasForge 正在关闭，暂时不能接收新的生图任务。",
        )

    def _generation_task_done(self, task: asyncio.Task[None]) -> None:
        """Drop the strong reference and always retrieve the task result."""

        self._generation_tasks.discard(task)
        lease = self._generation_leases.pop(task, None)
        self._generation_jobs.pop(task, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                "CanvasForge background task escaped its guard (%s).",
                type(exc).__name__,
            )
        if lease is None or lease.finished:
            return

        operation = self._release_abandoned_lease(lease)
        try:
            cleanup_task = asyncio.create_task(
                operation,
                name="canvasforge-lease-cleanup",
            )
        except Exception as exc:
            operation.close()
            logger.error(
                "CanvasForge could not schedule lease cleanup (%s).",
                type(exc).__name__,
            )
            return
        self._lease_cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._lease_cleanup_done)

    @staticmethod
    async def _release_abandoned_lease(lease: RequestLease) -> None:
        if not lease.finished:
            await lease.release()

    def _lease_cleanup_done(self, task: asyncio.Task[None]) -> None:
        self._lease_cleanup_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(
                "CanvasForge could not release an abandoned lease (%s).",
                type(exc).__name__,
            )

    async def _run_background_generation(
        self,
        job: _GenerationJob,
    ) -> None:
        """Run a handed-off job and report late failures directly to QQ."""

        try:
            await self._execute_generation_job(job)
        except asyncio.CancelledError:
            raise
        except CanvasForgeError as exc:
            await self._finalize_failure(job, exc)
        except Exception as exc:
            logger.error(
                "CanvasForge background generation failed unexpectedly (%s).",
                type(exc).__name__,
            )
            await self._finalize_failure(
                job,
                CanvasForgeError(
                    ErrorCode.INTERNAL,
                    "CanvasForge 处理生图任务时发生内部错误，请稍后再试。",
                ),
            )

    async def _execute_generation_job(
        self,
        job: _GenerationJob,
    ) -> str:
        """Run one paid request and commit cooldown only after QQ delivery."""

        event = job.event
        requested_mode = job.requested_mode
        settings = job.settings
        lease = job.lease
        try:
            (
                provider_factory,
                reference_resolver,
                avatar_resolver,
            ) = await self._ensure_runtime()

            # Bind URL and Key only after winning the non-queuing global gate.
            # This request-local provider cannot be changed by a concurrent
            # invocation or a Page configuration save.
            provider = provider_factory.create(
                HttpKeyProviderConfig(
                    base_url=job.base_url,
                    api_key=job.api_key,
                ),
            )

            if requested_mode == "generate":
                references = []
            else:
                references = await reference_resolver.resolve_snapshot(
                    job.reference_snapshot,
                    max_images=settings["max_reference_images"],
                    max_total_bytes=settings["max_total_reference_mib"] * MIB,
                    per_image_bytes=DEFAULT_PER_IMAGE_BYTES,
                    max_pixels=settings["max_reference_megapixels"] * 1_000_000,
                    max_edge=settings["max_reference_edge"],
                    event=job.event,
                )
            if (
                len(references) + len(job.planned_avatars)
                > settings["max_reference_images"]
            ):
                raise CanvasForgeError(
                    ErrorCode.REFERENCE_LIMIT,
                    "引用图片与人物头像的合计数量超过当前限制，请减少后重试。",
                )

            max_total_bytes = settings["max_total_reference_mib"] * MIB
            consumed_bytes = sum(len(reference.data) for reference in references)
            resolved_avatars = await avatar_resolver.download(
                event,
                job.planned_avatars,
                filename_start_index=len(references) + 1,
                consumed_bytes=consumed_bytes,
                max_total_bytes=max_total_bytes,
                per_image_bytes=DEFAULT_PER_IMAGE_BYTES,
                max_pixels=settings["max_reference_megapixels"] * 1_000_000,
                max_edge=settings["max_reference_edge"],
            )
            avatar_references = [
                resolved.reference for resolved in resolved_avatars
            ]
            references = [*references, *avatar_references]
            request_prompt = self._with_avatar_mapping(
                job.prompt,
                resolved_avatars,
                message_reference_count=(
                    len(references) - len(resolved_avatars)
                ),
            )
            request_prompt = self._with_edit_reference_guard(
                request_prompt,
                has_references=bool(references),
            )
            request_prompt = self._validate_prompt(
                request_prompt,
                settings["max_prompt_chars"],
            )
            mode = requested_mode
            message_reference_count = len(references) - len(resolved_avatars)
            logger.info(
                "CanvasForge prepared an Images API request "
                "(mode=%s, message_references=%d, avatar_references=%d, "
                "total_references=%d).",
                mode,
                message_reference_count,
                len(resolved_avatars),
                len(references),
            )

            # Resolve best-effort display names before the paid request. A
            # cancellation after payment therefore cannot lose the only copy
            # merely because a metadata lookup was still pending.
            source_metadata = await build_source_metadata(event)
            options = ImageRequestOptions(
                model=settings["model"],
                size=settings["size"],
                quality=settings["quality"],
                output_format=settings["output_format"],
                output_compression=settings["output_compression"],
                timeout_seconds=settings["request_timeout_seconds"],
                max_output_bytes=settings["max_output_mib"] * MIB,
            )

            if mode == "edit":
                image = await provider.edit(
                    request_prompt,
                    references,
                    options,
                )
            else:
                image = await provider.generate(request_prompt, options)

            await self._cache_generated_image(
                image,
                mode=mode,
                options=options,
                source_metadata=source_metadata,
            )

            await self._finalize_success(
                job,
                image,
            )
            return mode
        finally:
            if not lease.finished:
                await lease.release()

    async def _send_generated_image_and_commit(
        self,
        job: _GenerationJob,
        image: GeneratedImage,
    ) -> None:
        """Finish QQ delivery and its cooldown commit before cancellation.

        A platform send may have completed even if the caller is cancelled
        just before it resumes from ``await``. Shielding the send lets us
        distinguish confirmed success from failure; confirmed success always
        commits cooldown before the original cancellation is propagated.
        """

        try:
            delivery_bytes = await asyncio.to_thread(
                prepare_qq_delivery_bytes,
                image.data,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "CanvasForge could not optimize the QQ delivery copy (%s); "
                "the original generated image will be used.",
                type(exc).__name__,
            )
            delivery_bytes = image.data

        try:
            image_component = await asyncio.to_thread(
                Comp.Image.fromBytes,
                delivery_bytes,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CanvasForgeError(ErrorCode.SEND_FAILED) from None

        send_task = asyncio.ensure_future(
            self._send_active_message(
                job.conversation.unified_origin,
                MessageChain(chain=[image_component]),
            ),
        )
        cancellation: asyncio.CancelledError | None = None
        while not send_task.done():
            try:
                await asyncio.shield(send_task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
            except Exception:
                break

        try:
            sent = send_task.result()
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            raise cancellation
        except Exception:
            if cancellation is not None:
                raise cancellation from None
            raise CanvasForgeError(ErrorCode.SEND_FAILED) from None
        if not sent:
            raise CanvasForgeError(ErrorCode.SEND_FAILED)

        try:
            await job.lease.commit()
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

        if cancellation is not None:
            raise cancellation

    async def _finalize_success(
        self,
        job: _GenerationJob,
        image: GeneratedImage,
    ) -> None:
        """Finish an already generated image without leaving a half-state."""

        operation = self._finalize_success_inner(job, image)
        task = asyncio.create_task(
            operation,
            name="canvasforge-success-finalizer",
        )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc

        try:
            task.result()
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
        if cancellation is not None:
            raise cancellation

    async def _finalize_success_inner(
        self,
        job: _GenerationJob,
        image: GeneratedImage,
    ) -> None:
        """Send the image and persist success before any optional notice."""

        async with session_lock_manager.acquire_lock(
            job.conversation.unified_origin,
        ):
            await self._send_generated_image_and_commit(job, image)
            checkpoint = await self._persist_terminal_state_locked(
                job,
                self._terminal_status(
                    job,
                    completed=True,
                    failed=False,
                    image_sent=True,
                ),
            )

        if checkpoint is not None and not checkpoint.verified:
            await self._send_unverified_terminal_notice(
                job,
                success=True,
                error=None,
            )
            return
        await self._schedule_notification(
            job,
            success=True,
            error=None,
            checkpoint=checkpoint,
        )

    async def _generate_completion_message(
        self,
        job: _GenerationJob,
    ) -> str:
        """Ask the original chat model once for an in-character receipt."""

        return await self._generate_notification_message(
            job,
            prompt=(
                "CanvasForge 已经成功发送图片，任务已经完成。"
                "请按当前人格自然地通知用户。"
            ),
            fallback=_DEFAULT_COMPLETION_MESSAGE,
            kind="completion",
        )

    async def _generate_failure_message(
        self,
        job: _GenerationJob,
        error: CanvasForgeError,
    ) -> str:
        """Ask the original chat model to relay one safe failure reason."""

        fallback = f"CanvasForge 生图失败（{error.code.value}）：{error}"
        return await self._generate_notification_message(
            job,
            prompt=(
                f"CanvasForge 后台生图任务失败（{error.code.value}）：{error}\n"
                "请按当前人格自然地告知用户这次失败及原因。"
            ),
            fallback=fallback,
            kind="failure",
        )

    async def _generate_notification_message(
        self,
        job: _GenerationJob,
        *,
        prompt: str,
        fallback: str,
        kind: str,
    ) -> str:
        """Generate one persona-aware notice without tools or image input."""

        provider_id = job.conversation.provider_id
        if not provider_id:
            logger.warning(
                "CanvasForge has no captured chat provider for %s; "
                "using the fixed fallback.",
                kind,
            )
            return fallback

        raw_history = await self._load_authoritative_history(job)
        if (
            raw_history is None
            or (
                self._requires_task_marker(job)
                and not self._history_contains_task_id(
                    raw_history,
                    job.task_id,
                )
            )
        ):
            # A deleted/reset conversation must not be recreated or borrowed
            # as completion context.  The immutable admission snapshot keeps
            # the final notice in the original persona without touching the
            # user's new conversation state.
            raw_history = [
                copy.deepcopy(item)
                for item in job.conversation.contexts
            ]
        try:
            contexts = bind_checkpoint_messages(raw_history)
        except Exception as exc:
            logger.warning(
                "CanvasForge could not bind %s history (%s); "
                "using the admission snapshot.",
                kind,
                type(exc).__name__,
            )
            try:
                contexts = bind_checkpoint_messages(
                    [
                        copy.deepcopy(item)
                        for item in job.conversation.contexts
                    ],
                )
            except Exception:
                contexts = []

        kwargs: dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": prompt,
            "tools": None,
            "system_prompt": job.conversation.system_prompt,
            "contexts": contexts,
            "request_max_retries": 1,
        }
        if job.conversation.model:
            kwargs["model"] = job.conversation.model

        try:
            async with asyncio.timeout(_COMPLETION_TIMEOUT_SECONDS):
                response = await self.context.llm_generate(**kwargs)
            text = getattr(response, "completion_text", None)
            normalized = self._normalize_generated_notification(text)
            if normalized:
                return normalized
            logger.warning(
                "CanvasForge %s LLM returned no usable text; "
                "using the fixed fallback.",
                kind,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "CanvasForge %s LLM failed (%s); using the fixed "
                "fallback without retrying.",
                kind,
                type(exc).__name__,
            )
        return fallback

    async def _finalize_failure(
        self,
        job: _GenerationJob,
        error: CanvasForgeError,
    ) -> None:
        """Release admission and persist failure before asking the chat AI."""

        if not job.lease.finished:
            await job.lease.release()
        operation = self._finalize_failure_inner(job, error)
        task = asyncio.create_task(
            operation,
            name="canvasforge-failure-finalizer",
        )
        await self._await_task_cancellation_safe(task)

    async def _finalize_failure_inner(
        self,
        job: _GenerationJob,
        error: CanvasForgeError,
    ) -> None:
        async with session_lock_manager.acquire_lock(
            job.conversation.unified_origin,
        ):
            checkpoint = await self._persist_terminal_state_locked(
                job,
                self._terminal_status(
                    job,
                    completed=False,
                    failed=True,
                    image_sent=False,
                    code=error.code.value,
                    reason=str(error),
                ),
            )

        if checkpoint is not None and not checkpoint.verified:
            await self._send_unverified_terminal_notice(
                job,
                success=False,
                error=error,
            )
            return
        await self._schedule_notification(
            job,
            success=False,
            error=error,
            checkpoint=checkpoint,
        )

    async def _send_unverified_terminal_notice(
        self,
        job: _GenerationJob,
        *,
        success: bool,
        error: CanvasForgeError | None,
    ) -> None:
        """Report a result without AI when authoritative history is broken."""

        if success:
            text = "图片已生成并发送，但任务状态未能写入会话记录。"
        else:
            failure = error or CanvasForgeError(ErrorCode.INTERNAL)
            text = (
                f"CanvasForge 生图失败（{failure.code.value}）：{failure}"
                "；任务状态未能写入会话记录。"
            )
        try:
            sent = await self._send_active_message(
                job.conversation.unified_origin,
                MessageChain(chain=[Comp.Plain(text)]),
            )
            if not sent:
                logger.warning(
                    "CanvasForge could not deliver the unverified terminal "
                    "notice."
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "CanvasForge could not deliver the unverified terminal "
                "notice (%s).",
                type(exc).__name__,
            )

    async def _schedule_notification(
        self,
        job: _GenerationJob,
        *,
        success: bool,
        error: CanvasForgeError | None,
        checkpoint: _TerminalCheckpoint | None,
    ) -> None:
        """Create and strongly retain one post-terminal AI notification."""

        operation = self._run_notification(
            job,
            success=success,
            error=error,
            checkpoint=checkpoint,
        )
        task: asyncio.Task[None] | None = None
        async with self._lifecycle_lock:
            if not self._closing:
                try:
                    task = asyncio.create_task(
                        operation,
                        name="canvasforge-result-notification",
                    )
                except Exception as exc:
                    logger.warning(
                        "CanvasForge could not schedule a result notification "
                        "(%s).",
                        type(exc).__name__,
                    )
                else:
                    self._notification_tasks.add(task)
                    task.add_done_callback(self._notification_task_done)
        if task is None:
            operation.close()

    def _notification_task_done(self, task: asyncio.Task[None]) -> None:
        self._notification_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(
                "CanvasForge result notification failed unexpectedly (%s).",
                type(exc).__name__,
            )

    async def _run_notification(
        self,
        job: _GenerationJob,
        *,
        success: bool,
        error: CanvasForgeError | None,
        checkpoint: _TerminalCheckpoint | None,
    ) -> None:
        if success:
            async with session_lock_manager.acquire_lock(
                job.conversation.unified_origin,
            ):
                if await self._has_new_user_turn_locked(job, checkpoint):
                    logger.info(
                        "CanvasForge skipped a stale completion notification."
                    )
                    return
            text = await self._generate_completion_message(job)
        else:
            if error is None:
                error = CanvasForgeError(ErrorCode.INTERNAL)
            text = await self._generate_failure_message(job, error)

        async with session_lock_manager.acquire_lock(
            job.conversation.unified_origin,
        ):
            if success and await self._has_new_user_turn_locked(
                job,
                checkpoint,
            ):
                logger.info(
                    "CanvasForge skipped a stale completion notification."
                )
                return
            sent = False
            try:
                sent = await self._send_active_message(
                    job.conversation.unified_origin,
                    MessageChain(chain=[Comp.Plain(text)]),
                )
            except Exception as exc:
                logger.warning(
                    "CanvasForge could not deliver the %s notification (%s).",
                    "completion" if success else "failure",
                    type(exc).__name__,
                )
            if sent:
                await self._append_assistant_history(
                    job,
                    text,
                    require_task_marker=self._requires_task_marker(job),
                )

    @classmethod
    def _terminal_status(
        cls,
        job: _GenerationJob,
        *,
        completed: bool,
        failed: bool,
        image_sent: bool,
        code: str | None = None,
        reason: str | None = None,
    ) -> str:
        values: dict[str, Any] = {
            "accepted": True,
            "state": "idle",
            "finished": True,
            "completed": completed,
            "failed": failed,
            "image_sent": image_sent,
            "task_id": job.task_id,
        }
        if code:
            values["code"] = code
        if reason:
            values["reason"] = reason
        return cls._status_json(**values)

    async def _persist_terminal_state_locked(
        self,
        job: _GenerationJob,
        terminal_status: str,
    ) -> _TerminalCheckpoint | None:
        """Persist and verify one authoritative task terminal state."""

        conversation_id = job.conversation.conversation_id
        manager = getattr(self.context, "conversation_manager", None)
        if not conversation_id or manager is None:
            return None

        for attempt in range(1, _TERMINAL_HISTORY_ATTEMPTS + 1):
            try:
                finished, checkpoint = await self._await_bounded_operation(
                    self._persist_terminal_state_once(
                        job,
                        terminal_status,
                        manager,
                        conversation_id,
                    ),
                    timeout_seconds=(
                        _CONVERSATION_ATTEMPT_TIMEOUT_SECONDS
                    ),
                )
                if finished:
                    return checkpoint
                logger.warning(
                    "CanvasForge terminal history verification failed "
                    "(attempt %d/%d).",
                    attempt,
                    _TERMINAL_HISTORY_ATTEMPTS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "CanvasForge could not persist terminal history "
                    "on attempt %d/%d (%s).",
                    attempt,
                    _TERMINAL_HISTORY_ATTEMPTS,
                    type(exc).__name__,
                )
            await asyncio.sleep(0)

        logger.error(
            "CanvasForge terminal history could not be verified after %d attempts.",
            _TERMINAL_HISTORY_ATTEMPTS,
        )
        return _TerminalCheckpoint(
            user_message_count=0,
            verified=False,
        )

    async def _persist_terminal_state_once(
        self,
        job: _GenerationJob,
        terminal_status: str,
        manager: Any,
        conversation_id: str,
    ) -> tuple[bool, _TerminalCheckpoint | None]:
        """Perform one read-modify-write-read terminal-state attempt."""

        conversation = await manager.get_conversation(
            job.conversation.unified_origin,
            conversation_id,
            create_if_not_exists=False,
        )
        if conversation is None:
            return True, None
        history = self._parse_history(getattr(conversation, "history", ""))
        if self._history_contains_terminal_status(
            history,
            job.task_id,
            terminal_status,
        ):
            anchor_index = self._find_task_anchor(history, job.task_id)
            newer_user_already_present = (
                anchor_index is not None
                and any(
                    item.get("role") == "user"
                    for item in history[anchor_index + 1 :]
                )
            )
            return True, _TerminalCheckpoint(
                user_message_count=self._count_user_messages(history),
                newer_user_already_present=newer_user_already_present,
            )

        tool_index = self._find_task_tool_result(history, job.task_id)
        anchor_index = self._find_task_anchor(history, job.task_id)
        if tool_index is not None:
            replacement = copy.deepcopy(history[tool_index])
            replacement["content"] = terminal_status
            history[tool_index] = replacement
            anchor_index = tool_index
        elif anchor_index is not None:
            history.append(
                AssistantMessageSegment(
                    content=[TextPart(text=terminal_status)],
                ).model_dump(),
            )
        elif self._history_matches_admission(job, history):
            anchor_index = len(job.conversation.contexts) - 1
            history.append(
                AssistantMessageSegment(
                    content=[TextPart(text=terminal_status)],
                ).model_dump(),
            )
        else:
            logger.warning(
                "CanvasForge terminal task marker is missing; "
                "the original conversation will not be modified."
            )
            return True, None

        start_index = anchor_index + 1 if anchor_index is not None else 0
        newer_user_already_present = any(
            item.get("role") == "user"
            for item in history[start_index:]
        )
        await manager.update_conversation(
            job.conversation.unified_origin,
            conversation_id=conversation_id,
            history=history,
            token_usage=0,
        )

        verified_conversation = await manager.get_conversation(
            job.conversation.unified_origin,
            conversation_id,
            create_if_not_exists=False,
        )
        if verified_conversation is None:
            return True, None
        verified_history = self._parse_history(
            getattr(verified_conversation, "history", ""),
        )
        if not self._history_contains_terminal_status(
            verified_history,
            job.task_id,
            terminal_status,
        ):
            return False, None
        return True, _TerminalCheckpoint(
            user_message_count=self._count_user_messages(verified_history),
            newer_user_already_present=newer_user_already_present,
        )

    async def _has_new_user_turn_locked(
        self,
        job: _GenerationJob,
        checkpoint: _TerminalCheckpoint | None,
    ) -> bool:
        if checkpoint is None:
            return False
        if checkpoint.newer_user_already_present:
            return True
        conversation_id = job.conversation.conversation_id
        manager = getattr(self.context, "conversation_manager", None)
        if not conversation_id or manager is None:
            return False
        try:
            return bool(
                await self._await_bounded_operation(
                    self._has_new_user_turn_once(
                        job,
                        checkpoint,
                        manager,
                        conversation_id,
                    ),
                    timeout_seconds=(
                        _CONVERSATION_ATTEMPT_TIMEOUT_SECONDS
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A success notice is optional. If authoritative state cannot be
            # checked, suppress it instead of risking a stale interruption.
            logger.warning(
                "CanvasForge could not verify completion freshness (%s); "
                "the optional completion notice will be skipped.",
                type(exc).__name__,
            )
            return True

    async def _has_new_user_turn_once(
        self,
        job: _GenerationJob,
        checkpoint: _TerminalCheckpoint,
        manager: Any,
        conversation_id: str,
    ) -> bool:
        current_id = await manager.get_curr_conversation_id(
            job.conversation.unified_origin,
        )
        if current_id != conversation_id:
            return True
        conversation = await manager.get_conversation(
            job.conversation.unified_origin,
            conversation_id,
            create_if_not_exists=False,
        )
        if conversation is None:
            return True
        history = self._parse_history(getattr(conversation, "history", ""))
        return self._count_user_messages(history) > checkpoint.user_message_count

    @classmethod
    def _find_task_tool_result(
        cls,
        history: list[dict[str, Any]],
        task_id: str,
    ) -> int | None:
        for index, item in enumerate(history):
            if item.get("role") != "tool":
                continue
            if cls._text_has_task_id(cls._history_item_text(item), task_id):
                return index
        return None

    @classmethod
    def _find_task_anchor(
        cls,
        history: list[dict[str, Any]],
        task_id: str,
    ) -> int | None:
        tool_index = cls._find_task_tool_result(history, task_id)
        if tool_index is not None:
            return tool_index
        for index, item in enumerate(history):
            if item.get("role") == "_checkpoint":
                content = item.get("content")
                if isinstance(content, Mapping) and content.get("id") == task_id:
                    return index
            if cls._text_has_task_id(cls._history_item_text(item), task_id):
                return index
        return None

    @staticmethod
    def _history_item_text(item: Mapping[str, Any]) -> str:
        content = item.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        values: list[str] = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, str):
                values.append(text)
        return "".join(values)

    @staticmethod
    def _text_has_task_id(text: str, task_id: str) -> bool:
        if not text:
            return False
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping) and payload.get("task_id") == task_id:
            return True
        return f"task_id={task_id}" in text

    @classmethod
    def _history_contains_terminal_status(
        cls,
        history: list[dict[str, Any]],
        task_id: str,
        terminal_status: str,
    ) -> bool:
        for item in history:
            text = cls._history_item_text(item)
            if text != terminal_status:
                continue
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                continue
            if (
                isinstance(payload, Mapping)
                and payload.get("task_id") == task_id
                and payload.get("finished") is True
            ):
                return True
        return False

    @staticmethod
    def _count_user_messages(history: list[dict[str, Any]]) -> int:
        return sum(item.get("role") == "user" for item in history)

    @staticmethod
    def _history_matches_admission(
        job: _GenerationJob,
        history: list[dict[str, Any]],
    ) -> bool:
        admission = list(job.conversation.contexts)
        return bool(admission) and history[: len(admission)] == admission

    async def _send_active_message(
        self,
        unified_origin: str,
        chain: MessageChain,
    ) -> bool:
        """Send through Context with a hard caller-side deadline.

        ``asyncio.wait_for`` can itself wait forever when a third-party
        coroutine suppresses cancellation. Waiting on the task set lets
        CanvasForge stop waiting at the deadline; the abandoned operation is
        still cancelled, strongly retained and its eventual result consumed.
        """

        try:
            sent = await self._await_bounded_operation(
                self.context.send_message(unified_origin, chain),
                timeout_seconds=_ACTIVE_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "CanvasForge active message send timed out after %s seconds.",
                _ACTIVE_SEND_TIMEOUT_SECONDS,
            )
            return False
        return bool(sent)

    async def _await_bounded_operation(
        self,
        operation: Any,
        *,
        timeout_seconds: float,
    ) -> Any:
        """Await external I/O without trusting it to honour cancellation."""

        task = asyncio.ensure_future(operation)
        self._bounded_io_tasks.add(task)
        task.add_done_callback(self._bounded_io_task_done)
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task not in done:
            task.cancel()
            raise TimeoutError
        return task.result()

    def _bounded_io_task_done(self, task: asyncio.Task[Any]) -> None:
        self._bounded_io_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "CanvasForge detached I/O task ended with %s.",
                type(exc).__name__,
            )

    @staticmethod
    def _normalize_generated_notification(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()

    async def _cache_generated_image(
        self,
        image: GeneratedImage,
        *,
        mode: str,
        options: ImageRequestOptions,
        source_metadata: Mapping[str, str],
    ) -> None:
        """Cache before QQ delivery, without making caching user-visible."""

        if not self._cache_ready:
            return
        metadata: dict[str, Any] = {
            **source_metadata,
            "mode": mode,
            "model": options.model,
            "size": f"{image.width}x{image.height}",
        }
        try:
            await self._cache.store(
                image.data,
                metadata,
            )
        except CacheError as exc:
            logger.error(
                "CanvasForge could not cache a generated image (%s); QQ "
                "delivery will continue.",
                type(exc).__name__,
            )

    async def _ensure_runtime(
        self,
    ) -> tuple[ImageProviderFactory, ReferenceResolver, AvatarResolver]:
        """Create or recreate the shared HTTP resources when necessary."""

        async with self._runtime_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
                self._provider_factory = Sub2APIImagesProviderFactory(
                    self._session,
                )
                self._reference_resolver = ReferenceResolver(
                    self.context,
                    self._session,
                )
                self._avatar_resolver = AvatarResolver(
                    self.context,
                    self._session,
                )
            elif (
                self._provider_factory is None
                or self._reference_resolver is None
                or self._avatar_resolver is None
            ):
                self._provider_factory = Sub2APIImagesProviderFactory(
                    self._session,
                )
                self._reference_resolver = ReferenceResolver(
                    self.context,
                    self._session,
                )
                self._avatar_resolver = AvatarResolver(
                    self.context,
                    self._session,
                )
            return (
                self._provider_factory,
                self._reference_resolver,
                self._avatar_resolver,
            )

    async def _configuration_snapshot(
        self,
    ) -> tuple[str, str, dict[str, Any]]:
        """Read credentials and advanced options as one request snapshot."""

        async with self._settings_lock:
            base_url = self.config.get("base_url", "")
            api_key = self.config.get("api_key", "")
            advanced = self.config.get("advanced", {})
            settings = normalize_settings(
                advanced if isinstance(advanced, Mapping) else {},
            )
            return (
                base_url.strip() if isinstance(base_url, str) else "",
                api_key.strip() if isinstance(api_key, str) else "",
                settings,
            )

    async def _get_advanced_settings(self) -> dict[str, Any]:
        async with self._settings_lock:
            advanced = self.config.get("advanced", {})
            return normalize_settings(
                advanced if isinstance(advanced, Mapping) else {},
            )

    async def _save_advanced_settings(
        self,
        values: dict[str, Any],
    ) -> int:
        validated = normalize_settings(values, strict=True)
        async with self._settings_lock:
            transaction = asyncio.create_task(
                self._commit_advanced_settings(validated),
            )
            try:
                return await asyncio.shield(transaction)
            except asyncio.CancelledError as cancellation:
                # A closed Page must not interrupt the configuration/cache
                # transaction halfway through and leave mismatched limits.
                while not transaction.done():
                    try:
                        await asyncio.shield(transaction)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                try:
                    transaction.result()
                except BaseException as exc:
                    logger.error(
                        "CanvasForge settings transaction failed after "
                        "request cancellation (%s).",
                        type(exc).__name__,
                    )
                raise cancellation

    async def _commit_advanced_settings(
        self,
        validated: dict[str, Any],
    ) -> int:
        """Persist settings and align the cache without stale-value rollback."""

        try:
            committed = await self.config.save_config_async(
                {"advanced": validated},
            )
        except asyncio.CancelledError:
            # The write worker may still be finishing, so no destructive
            # cache adjustment is safe until persistence is confirmed.
            self._cache_ready = False
            raise
        except Exception as exc:
            if not self._advanced_settings_equal(validated):
                self._cache_ready = False
                raise RuntimeError(
                    "advanced settings changed before persistence recovery",
                ) from exc
            persisted = await self._persist_current_configuration()
            if persisted is not True or not self._advanced_settings_equal(
                validated,
            ):
                self._cache_ready = False
                raise RuntimeError(
                    "advanced settings could not be persisted",
                ) from exc
            committed = True

        # If only another top-level Dashboard field superseded this snapshot,
        # persist the complete current state once more. Never touch cache
        # files unless the advanced values are both current and confirmed.
        if committed is not True:
            if not self._advanced_settings_equal(validated):
                self._cache_ready = False
                raise RuntimeError("advanced settings save was superseded")
            persisted = await self._persist_current_configuration()
            if (
                persisted is not True
                or not self._advanced_settings_equal(validated)
            ):
                self._cache_ready = False
                raise RuntimeError("advanced settings save was superseded")
        elif not self._advanced_settings_equal(validated):
            self._cache_ready = False
            raise RuntimeError("advanced settings save was superseded")

        try:
            evicted = await self._cache.set_limit(
                validated["cache_max_images"],
            )
            self._cache_ready = True
        except asyncio.CancelledError:
            self._cache_ready = False
            await self._retry_validated_cache_limit(validated)
            raise
        except Exception as exc:
            self._cache_ready = False
            if not self._advanced_settings_equal(validated):
                raise RuntimeError(
                    "advanced settings changed concurrently",
                ) from exc
            reconciled = await self._retry_validated_cache_limit(validated)
            if (
                reconciled is not None
                and self._advanced_settings_equal(validated)
            ):
                return reconciled
            if not self._advanced_settings_equal(validated):
                raise RuntimeError(
                    "advanced settings changed concurrently",
                ) from exc
            raise

        # Hidden advanced values can still be carried through a concurrent
        # native Dashboard save. Pause new cache writes if such a revision
        # appeared while cache files were being adjusted.
        if not self._advanced_settings_equal(validated):
            self._cache_ready = False
            raise RuntimeError("advanced settings changed concurrently")
        return evicted

    async def _persist_current_configuration(self) -> bool:
        """Retry persistence without replacing any concurrently updated key."""

        try:
            return await self.config.save_config_async() is True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "CanvasForge could not persist the current configuration (%s).",
                type(exc).__name__,
            )
            return False

    async def _retry_validated_cache_limit(
        self,
        validated: Mapping[str, Any],
    ) -> int | None:
        """Retry only a confirmed Page value, pausing on any revision change."""

        if not self._advanced_settings_equal(validated):
            self._cache_ready = False
            return None
        try:
            evicted = await self._cache.set_limit(
                int(validated["cache_max_images"]),
            )
        except asyncio.CancelledError:
            self._cache_ready = False
            raise
        except Exception as exc:
            self._cache_ready = False
            logger.error(
                "CanvasForge could not apply the confirmed cache limit (%s).",
                type(exc).__name__,
            )
            return None

        if not self._advanced_settings_equal(validated):
            self._cache_ready = False
            return None
        self._cache_ready = True
        return evicted

    def _advanced_settings_equal(
        self,
        expected: Mapping[str, Any],
    ) -> bool:
        current = self.config.get("advanced", {})
        return isinstance(current, Mapping) and dict(current) == dict(expected)

    async def _send_command_text(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> bool:
        try:
            await self._await_bounded_operation(
                event.send(MessageChain(chain=[Comp.Plain(text)])),
                timeout_seconds=_ACTIVE_SEND_TIMEOUT_SECONDS,
            )
            return True
        except Exception as exc:
            logger.error(
                "CanvasForge could not send a command response (%s).",
                type(exc).__name__,
            )
            return False

    @staticmethod
    def _validate_prompt(prompt: str, maximum: int) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise CanvasForgeError(ErrorCode.MISSING_PROMPT)
        normalized = prompt.strip()
        if len(normalized) > maximum:
            raise CanvasForgeError(
                ErrorCode.UPSTREAM_REJECTED,
                f"提示词超过当前 {maximum} 字符限制，请缩短后重试。",
            )
        return normalized

    @staticmethod
    def _status_json(**values: Any) -> str:
        """Return one deterministic, model-readable tool status."""

        return json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _conversation_key(conversation: _ConversationContext) -> str:
        conversation_id = conversation.conversation_id or ""
        return f"{conversation.unified_origin}\x1f{conversation_id}"

    async def _capture_conversation_context(
        self,
        event: AstrMessageEvent,
        *,
        prefer_authoritative_history: bool = False,
    ) -> _ConversationContext:
        """Freeze the exact chat/provider identity used by this request."""

        unified_origin = getattr(event, "unified_msg_origin", "")
        if not isinstance(unified_origin, str) or not unified_origin.strip():
            raise CanvasForgeError(
                ErrorCode.INTERNAL,
                "无法识别当前会话，任务没有启动。",
            )
        unified_origin = unified_origin.strip()

        request = event.get_extra("provider_request")
        conversation = getattr(request, "conversation", None)
        conversation_id = getattr(conversation, "cid", None)
        if not isinstance(conversation_id, str) or not conversation_id:
            conversation_id = None

        system_prompt = getattr(request, "system_prompt", "")
        if not isinstance(system_prompt, str):
            system_prompt = ""
        model = getattr(request, "model", None)
        if not isinstance(model, str) or not model.strip():
            model = None
        else:
            model = model.strip()

        contexts = self._copy_history_items(getattr(request, "contexts", None))
        manager = getattr(self.context, "conversation_manager", None)
        if manager is not None:
            if conversation_id is None:
                try:
                    conversation_id = await manager.get_curr_conversation_id(
                        unified_origin,
                    )
                except Exception:
                    conversation_id = None
            if conversation_id:
                try:
                    current = await manager.get_conversation(
                        unified_origin,
                        conversation_id,
                        create_if_not_exists=False,
                    )
                except Exception:
                    current = None
                if current is not None:
                    conversation = current
                    if prefer_authoritative_history or not contexts:
                        contexts = tuple(
                            self._parse_history(
                                getattr(current, "history", ""),
                            ),
                        )

        if not system_prompt:
            system_prompt = await self._resolve_persona_prompt(
                unified_origin,
                conversation,
                platform_name=self._event_string(
                    event,
                    "get_platform_name",
                ),
            )

        provider_id: str | None = None
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                unified_origin,
            )
        except Exception:
            provider_id = None
        if not isinstance(provider_id, str) or not provider_id.strip():
            provider_id = None
        else:
            provider_id = provider_id.strip()

        return _ConversationContext(
            unified_origin=unified_origin,
            conversation_id=conversation_id,
            provider_id=provider_id,
            model=model,
            system_prompt=system_prompt,
            contexts=contexts,
        )

    async def _resolve_persona_prompt(
        self,
        unified_origin: str,
        conversation: Any,
        *,
        platform_name: str,
    ) -> str:
        """Best-effort persona prompt for the direct command entry point."""

        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        persona_id = getattr(conversation, "persona_id", None)
        selected_resolver = getattr(
            manager,
            "resolve_selected_persona",
            None,
        )
        if callable(selected_resolver):
            provider_settings: dict[str, Any] = {}
            config_getter = getattr(self.context, "get_config", None)
            if callable(config_getter):
                try:
                    config = config_getter(umo=unified_origin)
                    if isinstance(config, Mapping):
                        configured = config.get("provider_settings", {})
                        if isinstance(configured, Mapping):
                            provider_settings = dict(configured)
                except Exception:
                    provider_settings = {}
            try:
                resolved = await selected_resolver(
                    umo=unified_origin,
                    conversation_persona_id=(
                        persona_id
                        if isinstance(persona_id, str)
                        else None
                    ),
                    platform_name=platform_name,
                    provider_settings=provider_settings,
                )
                persona = (
                    resolved[1]
                    if isinstance(resolved, tuple) and len(resolved) >= 2
                    else None
                )
                if isinstance(persona, Mapping):
                    prompt = persona.get("prompt")
                    if isinstance(prompt, str):
                        return prompt
            except Exception:
                pass
        try:
            resolver = getattr(manager, "get_persona_v3_by_id", None)
            persona = resolver(persona_id) if callable(resolver) else None
            if persona is None:
                default_resolver = getattr(
                    manager,
                    "get_default_persona_v3",
                    None,
                )
                if callable(default_resolver):
                    persona = await default_resolver(unified_origin)
            if isinstance(persona, Mapping):
                prompt = persona.get("prompt")
                if isinstance(prompt, str):
                    return prompt
        except Exception:
            pass

        if isinstance(persona_id, str) and persona_id:
            try:
                legacy = await manager.get_persona(persona_id)
                prompt = getattr(legacy, "system_prompt", "")
                if isinstance(prompt, str):
                    return prompt
            except Exception:
                pass
        return ""

    @staticmethod
    def _copy_history_items(value: Any) -> tuple[dict[str, Any], ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        copied: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                copied.append(copy.deepcopy(dict(item)))
                continue
            model_dump = getattr(item, "model_dump", None)
            if callable(model_dump):
                try:
                    dumped = model_dump()
                except Exception:
                    continue
                if isinstance(dumped, Mapping):
                    copied.append(copy.deepcopy(dict(dumped)))
        return tuple(copied)

    @staticmethod
    def _parse_history(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, str) or not value:
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        return [
            copy.deepcopy(dict(item))
            for item in parsed
            if isinstance(item, Mapping)
        ]

    async def _load_authoritative_history(
        self,
        job: _GenerationJob,
    ) -> list[dict[str, Any]] | None:
        conversation_id = job.conversation.conversation_id
        manager = getattr(self.context, "conversation_manager", None)
        if not conversation_id or manager is None:
            return None
        try:
            conversation = await self._await_bounded_operation(
                manager.get_conversation(
                    job.conversation.unified_origin,
                    conversation_id,
                    create_if_not_exists=False,
                ),
                timeout_seconds=_CONVERSATION_ATTEMPT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "CanvasForge could not load the original conversation (%s).",
                type(exc).__name__,
            )
            return None
        if conversation is None:
            return None
        return self._parse_history(getattr(conversation, "history", ""))

    @staticmethod
    def _requires_task_marker(job: _GenerationJob) -> bool:
        return job.conversation.conversation_id is not None

    @staticmethod
    def _history_contains_task_id(
        history: list[dict[str, Any]],
        task_id: str,
    ) -> bool:
        try:
            serialized = json.dumps(history, ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        return task_id in serialized

    async def _append_assistant_history(
        self,
        job: _GenerationJob,
        text: str,
        *,
        require_task_marker: bool,
    ) -> bool:
        conversation_id = job.conversation.conversation_id
        manager = getattr(self.context, "conversation_manager", None)
        if not conversation_id or manager is None:
            return False
        try:
            return bool(
                await self._await_bounded_operation(
                    self._append_assistant_history_once(
                        job,
                        text,
                        require_task_marker=require_task_marker,
                        manager=manager,
                        conversation_id=conversation_id,
                    ),
                    timeout_seconds=(
                        _CONVERSATION_ATTEMPT_TIMEOUT_SECONDS
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "CanvasForge could not persist the terminal assistant state "
                "(%s).",
                type(exc).__name__,
            )
            return False

    async def _append_assistant_history_once(
        self,
        job: _GenerationJob,
        text: str,
        *,
        require_task_marker: bool,
        manager: Any,
        conversation_id: str,
    ) -> bool:
        conversation = await manager.get_conversation(
            job.conversation.unified_origin,
            conversation_id,
            create_if_not_exists=False,
        )
        if conversation is None:
            return False
        history = self._parse_history(getattr(conversation, "history", ""))
        if require_task_marker and not self._history_contains_task_id(
            history,
            job.task_id,
        ):
            return False
        history.append(
            AssistantMessageSegment(
                content=[TextPart(text=text)],
            ).model_dump(),
        )
        await manager.update_conversation(
            job.conversation.unified_origin,
            conversation_id=conversation_id,
            history=history,
            token_usage=0,
        )
        return True

    @staticmethod
    def _with_avatar_mapping(
        prompt: str,
        avatars: list[ResolvedAvatar],
        *,
        message_reference_count: int,
    ) -> str:
        """Append bounded identity labels without exposing QQ identifiers."""

        if not avatars:
            return prompt
        lines = [
            "",
            "",
            "QQ 人物参考图映射（按输入图编号；昵称只用于人物标识）：",
        ]
        for person_index, avatar in enumerate(avatars, start=1):
            input_index = message_reference_count + person_index
            encoded_name = json.dumps(
                avatar.display_name,
                ensure_ascii=False,
            )
            lines.append(f"输入图{input_index}：QQ 头像，昵称{encoded_name}")
        return prompt + "\n".join(lines)

    @staticmethod
    def _with_edit_reference_guard(
        prompt: str,
        *,
        has_references: bool,
    ) -> str:
        """Add one lightweight reference hint without prescribing the edit."""

        if not has_references:
            return prompt
        guard = "\n\n参考图用于保持人物身份与整体外貌，需要改变的内容按提示词处理。"
        return prompt + guard

    def _configure_llm_tool_schemas(self) -> None:
        """Keep prompt explicit while making avatar references optional."""

        try:
            manager = self.context.get_llm_tool_manager()
            tools = getattr(manager, "func_list", ())
            found: set[str] = set()
            for tool in tools:
                tool_name = getattr(tool, "name", "")
                if tool_name not in _LLM_TOOL_NAMES:
                    continue
                parameters = copy.deepcopy(getattr(tool, "parameters", {}))
                properties = parameters.get("properties")
                if not isinstance(properties, dict):
                    raise TypeError("tool properties are unavailable")
                if not isinstance(properties.get("prompt"), dict):
                    raise TypeError("prompt schema is unavailable")
                properties.pop("completion_message", None)

                if tool_name == _IMAGE_TO_IMAGE_TOOL_NAME:
                    avatar_schema = properties.get("avatar_targets")
                    if not isinstance(avatar_schema, dict):
                        raise TypeError("avatar target schema is unavailable")
                    avatar_schema.update(
                        {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "pattern": (
                                    r"^(sender|bot|mention:[1-9][0-9]*)$"
                                ),
                            },
                            "maxItems": 10,
                            "uniqueItems": True,
                            "default": [],
                        },
                    )
                    required = ["prompt"]
                else:
                    required = ["prompt"]

                parameters["required"] = required
                parameters["additionalProperties"] = False
                tool.parameters = parameters
                found.add(tool_name)

            if found != set(_LLM_TOOL_NAMES):
                raise LookupError("tool registration is unavailable")
        except Exception as exc:
            logger.warning(
                "CanvasForge could not tighten the LLM tool schemas (%s); "
                "runtime validation remains enabled.",
                type(exc).__name__,
            )

    @staticmethod
    def _event_string(event: AstrMessageEvent, method_name: str) -> str:
        method = getattr(event, method_name, None)
        if not callable(method):
            return ""
        try:
            value = method()
        except Exception:
            return ""
        return str(value).strip() if value is not None else ""

    async def _start_legacy_update_cleanup(self) -> None:
        """Remove only the retired updater's two fixed status files.

        During an in-place upgrade, the v0.1.6 update task can still be
        finishing after v0.1.7 has initialized. In that case cleanup is
        detached until every known legacy writer has stopped, preventing the
        obsolete status file from being recreated after it was removed.
        """

        runtime = getattr(self.context, _LEGACY_UPDATE_CONTEXT_KEY, None)
        if self._live_legacy_update_tasks(runtime):
            operation = self._wait_for_legacy_update_cleanup(runtime)
            try:
                task = asyncio.create_task(
                    operation,
                    name="canvasforge-legacy-update-cleanup",
                )
            except Exception as exc:
                operation.close()
                logger.warning(
                    "CanvasForge could not schedule legacy update cleanup "
                    "(%s); it will be retried on the next startup.",
                    type(exc).__name__,
                )
                return
            self._legacy_cleanup_task = task
            task.add_done_callback(self._legacy_cleanup_done)
            return
        try:
            await self._cleanup_legacy_update_state(runtime)
        except Exception as exc:
            logger.warning(
                "CanvasForge legacy update cleanup failed (%s); "
                "it will be retried on the next startup.",
                type(exc).__name__,
            )

    async def _wait_for_legacy_update_cleanup(
        self,
        runtime: Any,
    ) -> None:
        """Wait for legacy writers without ever delaying plugin startup."""

        while True:
            live_tasks = self._live_legacy_update_tasks(runtime)
            if not live_tasks:
                break
            await asyncio.gather(
                *(asyncio.shield(task) for task in live_tasks),
                return_exceptions=True,
            )
        await self._cleanup_legacy_update_state(runtime)

    @staticmethod
    def _live_legacy_update_tasks(runtime: Any) -> tuple[asyncio.Task[Any], ...]:
        if not isinstance(runtime, dict):
            return ()
        tasks: set[asyncio.Task[Any]] = set()
        for key in ("update_task", "accepted_watchdog", "check_task"):
            candidate = runtime.get(key)
            if (
                isinstance(candidate, asyncio.Task)
                and not candidate.done()
            ):
                tasks.add(candidate)
        return tuple(tasks)

    def _legacy_cleanup_done(self, task: asyncio.Task[None]) -> None:
        if self._legacy_cleanup_task is task:
            self._legacy_cleanup_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "CanvasForge legacy update cleanup failed (%s); "
                "it will be retried on the next startup.",
                type(exc).__name__,
            )

    async def _cleanup_legacy_update_state(self, runtime: Any) -> None:
        failures = await asyncio.to_thread(
            self._remove_legacy_update_status_files,
            self._data_root,
        )
        if failures:
            logger.warning(
                "CanvasForge could not remove every legacy update status "
                "file (%s); cleanup will be retried on the next startup.",
                ", ".join(failures),
            )

        for key, expected in (
            (_LEGACY_UPDATE_CONTEXT_KEY, runtime),
            (
                _LEGACY_ADMISSION_CONTEXT_KEY,
                getattr(self.context, _LEGACY_ADMISSION_CONTEXT_KEY, None),
            ),
        ):
            if expected is None or getattr(self.context, key, None) is not expected:
                continue
            try:
                delattr(self.context, key)
            except (AttributeError, TypeError):
                pass

    @staticmethod
    def _remove_legacy_update_status_files(
        data_root: Path,
    ) -> tuple[str, ...]:
        failures: list[str] = []
        for filename in _LEGACY_UPDATE_STATUS_FILENAMES:
            try:
                (data_root / filename).unlink(missing_ok=True)
            except OSError as exc:
                failures.append(type(exc).__name__)
        return tuple(failures)

    async def _acquire_generation_lease(
        self,
        user_id: str,
        *,
        conversation_key: str,
        task_id: str,
        max_concurrent: int,
        is_admin: bool,
        cooldown_seconds: float,
    ) -> RequestLease:
        """Acquire without leaking a busy slot across task cancellation."""

        operation = self._request_gate.acquire(
            user_id,
            conversation_key=conversation_key,
            task_id=task_id,
            max_concurrent=max_concurrent,
            is_admin=is_admin,
            cooldown_seconds=cooldown_seconds,
        )
        task = contextvars.Context().run(asyncio.create_task, operation)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc

        try:
            lease = task.result()
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise

        if cancellation is not None:
            await lease.release()
            raise cancellation
        return lease

    @staticmethod
    async def _await_task_cancellation_safe(
        task: asyncio.Task[None],
    ) -> None:
        """Finish one shutdown task before propagating caller cancellation."""

        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        try:
            task.result()
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise
        if cancellation is not None:
            raise cancellation

    async def terminate(self) -> None:
        """Block admission, drain jobs, then close the shared HTTP session."""

        async with self._lifecycle_lock:
            self._closing = True
            task = self._termination_task
            if task is None:
                operation = self._terminate_resources()
                task = contextvars.Context().run(
                    asyncio.create_task,
                    operation,
                )
                self._termination_task = task
        await self._await_task_cancellation_safe(task)

    async def _terminate_resources(self) -> None:
        self._web_api.deactivate()
        async with self._lifecycle_lock:
            generation_tasks = tuple(self._generation_tasks)
            notification_tasks = tuple(self._notification_tasks)
            bounded_io_tasks = tuple(self._bounded_io_tasks)
            generation_jobs = tuple(self._generation_jobs.values())

        tasks = (*generation_tasks, *notification_tasks)
        jobs = generation_jobs

        for task in bounded_io_tasks:
            task.cancel()
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=_SHUTDOWN_TASK_TIMEOUT_SECONDS,
            )
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass
            if pending:
                logger.error(
                    "CanvasForge shutdown abandoned %d task(s) after %s "
                    "seconds because external I/O did not stop.",
                    len(pending),
                    _SHUTDOWN_TASK_TIMEOUT_SECONDS,
                )
                for task in pending:
                    task.cancel()

        seen_leases: set[int] = set()
        for job in jobs:
            lease = job.lease
            lease_identity = id(lease)
            if lease_identity in seen_leases:
                continue
            seen_leases.add(lease_identity)
            if lease.finished:
                continue
            try:
                await lease.release()
            except Exception as exc:
                logger.error(
                    "CanvasForge could not release a generation slot (%s).",
                    type(exc).__name__,
                )

        lease_cleanup_tasks = tuple(self._lease_cleanup_tasks)
        if lease_cleanup_tasks:
            await asyncio.gather(
                *lease_cleanup_tasks,
                return_exceptions=True,
            )

        cleanup_task = self._legacy_cleanup_task
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)

        async with self._runtime_lock:
            session = self._session
            self._provider_factory = None
            self._reference_resolver = None
            self._avatar_resolver = None
            self._session = None

            if session is not None and not session.closed:
                await session.close()
