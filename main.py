"""CanvasForge AstrBot plugin entry point."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any

import aiohttp
from astrbot import __version__ as ASTRBOT_VERSION
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, StarTools, register

from .canvasforge.cache import CacheError, CacheStore
from .canvasforge.contracts import (
    CanvasForgeError,
    ErrorCode,
    GeneratedImage,
    HttpKeyProviderConfig,
    ImageProviderFactory,
    ImageRequestOptions,
)
from .canvasforge.provider import (
    Sub2APIImagesProviderFactory,
)
from .canvasforge.rate_limit import RequestGate, RequestLease
from .canvasforge.reference import (
    DEFAULT_PER_IMAGE_BYTES,
    ReferenceResolver,
    build_source_metadata,
)
from .canvasforge.update import UpdateCoordinator
from .canvasforge.web_api import WebAPI, normalize_settings


PLUGIN_NAME = "astrbot_plugin_canvasforge"
PLUGIN_AUTHOR = "YuXya"
PLUGIN_VERSION = "v0.1.0"
PLUGIN_REPOSITORY = "https://github.com/YuXya/astrbot_plugin_canvasforge"
PLUGIN_DESCRIPTION = (
    "通过 Sub2API 调用 GPT Images，为 NapCat QQ 提供文生图与引用图编辑能力。"
)
MIB = 1024 * 1024
_ADMISSION_CONTEXT_KEY = "_canvasforge_admission_runtime_v2"


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESCRIPTION,
    PLUGIN_VERSION,
    PLUGIN_REPOSITORY,
)
class CanvasForgePlugin(Star):
    """在 NapCat QQ 中生成图片，或编辑直接引用消息中的图片。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self._runtime_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._admission = self._get_admission_runtime(context)
        self._session: aiohttp.ClientSession | None = None
        self._provider_factory: ImageProviderFactory | None = None
        self._reference_resolver: ReferenceResolver | None = None
        self._request_gate = RequestGate()

        data_root = StarTools.get_data_dir(PLUGIN_NAME)
        cache_root = data_root / "cache"
        self._cache = CacheStore(cache_root)
        self._cache_ready = False
        self._update_coordinator = UpdateCoordinator(
            context,
            data_root,
            self._get_http_session,
            Path(__file__).with_name("metadata.yaml"),
            local_version=PLUGIN_VERSION,
            astrbot_version=ASTRBOT_VERSION,
            reserve_update=self._reserve_update,
            release_update=self._release_update,
        )
        self._web_api = WebAPI(
            context,
            self._cache,
            self._get_advanced_settings,
            self._save_advanced_settings,
            self._begin_page_mutation,
            self._end_page_mutation,
            self._update_coordinator,
        )
        self._web_api.register()

    async def initialize(self) -> None:
        """Create long-lived resources without requiring a configured Key."""

        await self._ensure_runtime()
        await self._update_coordinator.initialize()
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

    @filter.llm_tool(name="canvasforge_generate_image")
    async def canvasforge_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
    ) -> str:
        """生成一张图片，或使用用户直接引用消息中的图片进行编辑。

        当前聊天 AI 应自行编写完整、清晰的绘图提示词。工具不会额外调用聊天
        模型，也不会返回 revised_prompt 或 usage。

        Args:
            prompt(string): 由当前聊天 AI 编写的完整绘图或编辑提示词。
        """

        try:
            mode = await self._generate_and_send(
                event,
                prompt,
                send_progress=False,
            )
        except CanvasForgeError as exc:
            return (
                f"CanvasForge 工具调用失败（{exc.code.value}）：{exc}"
            )
        except Exception as exc:
            logger.error(
                "CanvasForge tool failed unexpectedly (%s).",
                type(exc).__name__,
            )
            return (
                "CanvasForge 工具调用失败"
                f"（{ErrorCode.INTERNAL.value}）："
                "CanvasForge 处理请求时发生内部错误，请稍后再试。"
            )

        action = "引用图编辑" if mode == "edit" else "图片生成"
        return f"CanvasForge 已完成{action}，图片已发送到当前 QQ 会话。"

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

        try:
            await self._generate_and_send(
                event,
                prompt,
                send_progress=True,
            )
        except CanvasForgeError as exc:
            await self._send_command_text(event, str(exc))
        except Exception as exc:
            logger.error(
                "CanvasForge command failed unexpectedly (%s).",
                type(exc).__name__,
            )
            await self._send_command_text(
                event,
                "CanvasForge 处理请求时发生内部错误，请稍后再试。",
            )

    async def _generate_and_send(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        send_progress: bool,
    ) -> str:
        """Run one paid request and commit cooldown only after QQ delivery."""

        await self._reject_if_updating()
        if event.get_platform_name() != "aiocqhttp":
            raise CanvasForgeError(ErrorCode.PLATFORM_UNSUPPORTED)

        base_url, api_key, settings = await self._configuration_snapshot()
        normalized_prompt = self._validate_prompt(
            prompt,
            settings["max_prompt_chars"],
        )
        if not base_url or not api_key:
            raise CanvasForgeError(ErrorCode.NOT_CONFIGURED)

        user_id = self._event_string(event, "get_sender_id")
        if not user_id:
            user_id = self._event_string(event, "get_session_id") or "unknown"
        lease = await self._acquire_generation_lease(
            user_id,
            is_admin=bool(event.is_admin()),
            cooldown_seconds=settings["cooldown_seconds"],
        )

        try:
            provider_factory, reference_resolver = await self._ensure_runtime()
            # Bind URL and Key only after winning the non-queuing global gate.
            # This request-local provider cannot be changed by a concurrent
            # invocation or a Page configuration save.
            provider = provider_factory.create(
                HttpKeyProviderConfig(base_url=base_url, api_key=api_key),
            )

            if send_progress:
                try:
                    await event.send(
                        MessageChain(
                            chain=[Comp.Plain("CanvasForge 已受理，正在生成图片…")],
                        ),
                    )
                except Exception:
                    raise CanvasForgeError(
                        ErrorCode.SEND_FAILED,
                        "无法向 QQ 发送进度消息，本次生成已取消。",
                    ) from None

            references = await reference_resolver.resolve(
                event,
                max_images=settings["max_reference_images"],
                max_total_bytes=settings["max_total_reference_mib"] * MIB,
                per_image_bytes=DEFAULT_PER_IMAGE_BYTES,
                max_pixels=settings["max_reference_megapixels"] * 1_000_000,
                max_edge=settings["max_reference_edge"],
            )
            mode = "edit" if references else "generate"

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

            if references:
                image = await provider.edit(
                    normalized_prompt,
                    references,
                    options,
                )
            else:
                image = await provider.generate(normalized_prompt, options)

            await self._cache_generated_image(
                image,
                mode=mode,
                options=options,
                source_metadata=source_metadata,
            )

            await self._send_generated_image_and_commit(
                event,
                image,
                lease,
            )
            return mode
        finally:
            if not lease.finished:
                await lease.release()

    async def _send_generated_image_and_commit(
        self,
        event: AstrMessageEvent,
        image: GeneratedImage,
        lease: RequestLease,
    ) -> None:
        """Finish QQ delivery and its cooldown commit before cancellation.

        A platform send may have completed even if the caller is cancelled
        just before it resumes from ``await``. Shielding the send lets us
        distinguish confirmed success from failure; confirmed success always
        commits cooldown before the original cancellation is propagated.
        """

        send_task = asyncio.ensure_future(
            event.send(
                MessageChain(chain=[Comp.Image.fromBytes(image.data)]),
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
            send_task.result()
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            raise cancellation
        except Exception:
            if cancellation is not None:
                raise cancellation from None
            raise CanvasForgeError(ErrorCode.SEND_FAILED) from None

        try:
            await lease.commit()
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

        if cancellation is not None:
            raise cancellation

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
    ) -> tuple[ImageProviderFactory, ReferenceResolver]:
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
            elif (
                self._provider_factory is None
                or self._reference_resolver is None
            ):
                self._provider_factory = Sub2APIImagesProviderFactory(
                    self._session,
                )
                self._reference_resolver = ReferenceResolver(
                    self.context,
                    self._session,
                )
            return self._provider_factory, self._reference_resolver

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Return the instance session used for public GitHub checks."""

        await self._ensure_runtime()
        session = self._session
        if session is None or session.closed:
            raise RuntimeError("CanvasForge HTTP session is unavailable")
        return session

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
    ) -> None:
        try:
            await event.send(MessageChain(chain=[Comp.Plain(text)]))
        except Exception as exc:
            logger.error(
                "CanvasForge could not send a command response (%s).",
                type(exc).__name__,
            )

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
    def _event_string(event: AstrMessageEvent, method_name: str) -> str:
        method = getattr(event, method_name, None)
        if not callable(method):
            return ""
        try:
            value = method()
        except Exception:
            return ""
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _get_admission_runtime(context: Context) -> dict[str, Any]:
        """Keep update admission state alive while this plugin reloads itself."""

        current = getattr(context, _ADMISSION_CONTEXT_KEY, None)
        if (
            isinstance(current, dict)
            and current.get("schema") == 2
            and hasattr(current.get("lock"), "__aenter__")
        ):
            return current

        runtime: dict[str, Any] = {
            "schema": 2,
            "lock": asyncio.Lock(),
            "update_owner": None,
            "page_mutations": 0,
        }
        setattr(context, _ADMISSION_CONTEXT_KEY, runtime)
        return runtime

    async def _acquire_generation_lease(
        self,
        user_id: str,
        *,
        is_admin: bool,
        cooldown_seconds: float,
    ) -> RequestLease:
        """Acquire without leaking a busy slot across task cancellation."""

        operation = self._reserve_generation_lease(
            user_id,
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

    async def _reject_if_updating(self) -> None:
        """Give tools and commands the maintenance result before other checks."""

        async with self._admission["lock"]:
            if self._admission.get("update_owner") is not None:
                raise CanvasForgeError(
                    ErrorCode.BUSY,
                    "CanvasForge 正在更新，请稍后再试。",
                )

    async def _reserve_generation_lease(
        self,
        user_id: str,
        *,
        is_admin: bool,
        cooldown_seconds: float,
    ) -> RequestLease:
        """Atomically exclude a new paid request from a pending self-update."""

        async with self._admission["lock"]:
            if self._admission.get("update_owner") is not None:
                raise CanvasForgeError(
                    ErrorCode.BUSY,
                    "CanvasForge 正在更新，请稍后再试。",
                )
            return await self._request_gate.acquire(
                user_id,
                is_admin=is_admin,
                cooldown_seconds=cooldown_seconds,
            )

    async def _begin_page_mutation(self) -> bool:
        """Reserve one Page write unless an update is already in progress."""

        async with self._admission["lock"]:
            if self._admission.get("update_owner") is not None:
                return False
            self._admission["page_mutations"] += 1
            return True

    async def _end_page_mutation(self) -> None:
        async def decrement() -> None:
            async with self._admission["lock"]:
                self._admission["page_mutations"] = max(
                    0,
                    int(self._admission["page_mutations"]) - 1,
                )

        await self._finish_admission_cleanup(decrement())

    async def _reserve_update(self, owner: str) -> bool:
        """Atomically enter maintenance only while every writer is idle."""

        if not isinstance(owner, str) or len(owner) != 32:
            return False
        async with self._admission["lock"]:
            if self._admission.get("update_owner") is not None:
                return False
            if int(self._admission["page_mutations"]) > 0:
                return False
            if await self._request_gate.is_busy():
                return False
            self._admission["update_owner"] = owner
            return True

    async def _release_update(self, owner: str) -> None:
        """Leave maintenance after the detached updater reaches a terminal state."""

        async def release() -> None:
            async with self._admission["lock"]:
                if self._admission.get("update_owner") == owner:
                    self._admission["update_owner"] = None

        await self._finish_admission_cleanup(release())

    @staticmethod
    async def _finish_admission_cleanup(operation: Awaitable[None]) -> None:
        """Finish state cleanup even when its request task is being cancelled."""

        task = contextvars.Context().run(asyncio.create_task, operation)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        task.result()
        if cancellation is not None:
            raise cancellation

    async def terminate(self) -> None:
        """Close every long-lived network resource on plugin unload."""

        self._web_api.deactivate()
        try:
            await self._update_coordinator.deactivate()
        except Exception as exc:
            logger.error(
                "CanvasForge update coordinator shutdown failed (%s).",
                type(exc).__name__,
            )
        async with self._runtime_lock:
            session = self._session
            self._provider_factory = None
            self._reference_resolver = None
            self._session = None

            if session is not None and not session.closed:
                await session.close()
