"""AstrBot Plugin Page backend for CanvasForge settings and image cache."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from astrbot.api.web import error_response, file_response, json_response, request

from .cache import CacheError, CacheNotFoundError, CacheStore
from .update import UpdateCoordinator, UpdateCoordinatorError


PLUGIN_NAME = "astrbot_plugin_canvasforge"
_CACHE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class _StartUpdateAfterResponse:
    """Response callback that starts the update after HTTP 202 is sent."""

    def __init__(self, updates: UpdateCoordinator, job_id: str) -> None:
        self._updates = updates
        self._job_id = job_id

    async def __call__(self) -> None:
        try:
            started = await self._updates.start_accepted(self._job_id)
            if not started:
                await self._updates.abort_accepted(self._job_id)
        except BaseException:
            # Do not let response-background failures expose internals or
            # strand the shared maintenance state.
            try:
                await self._updates.abort_accepted(self._job_id)
            except BaseException:
                pass


ADVANCED_DEFAULTS: dict[str, Any] = {
    "model": "gpt-image-2",
    "size": "1024x1024",
    "quality": "medium",
    "output_format": "png",
    "output_compression": 90,
    "request_timeout_seconds": 300,
    "cooldown_seconds": 300,
    "admin_only_generation": True,
    "max_prompt_chars": 8000,
    "max_output_mib": 20,
    "enable_avatar_references": True,
    "max_reference_images": 3,
    "max_total_reference_mib": 30,
    "max_reference_megapixels": 40,
    "max_reference_edge": 8192,
    "cache_max_images": 3,
}


def normalize_settings(
    values: Mapping[str, Any],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Return a complete, validated advanced-settings dictionary.

    In tolerant mode, unknown keys are ignored and each invalid value falls
    back to its own default. Strict mode rejects unknown or invalid values and
    is intended for writes originating from the Plugin Page.
    """

    if not isinstance(values, Mapping):
        if strict:
            raise ValueError("设置内容必须是 JSON 对象")
        values = {}

    unknown = set(values) - set(ADVANCED_DEFAULTS)
    if strict and unknown:
        raise ValueError(f"包含未知设置项：{', '.join(sorted(unknown))}")

    result = dict(ADVANCED_DEFAULTS)
    for key in ADVANCED_DEFAULTS:
        if key not in values:
            continue
        try:
            result[key] = _validate_setting(key, values[key])
        except ValueError:
            if strict:
                raise
    return result


def _validate_setting(key: str, value: Any) -> Any:
    if key == "model":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("模型名称不能为空")
        return value.strip()
    if key == "size":
        if value not in {"auto", "1024x1024", "1536x1024", "1024x1536"}:
            raise ValueError("图片尺寸选项无效")
        return value
    if key == "quality":
        if value not in {"auto", "low", "medium", "high"}:
            raise ValueError("图片质量选项无效")
        return value
    if key == "output_format":
        if value not in {"png", "jpeg", "webp"}:
            raise ValueError("输出格式选项无效")
        return value
    if key in {"admin_only_generation", "enable_avatar_references"}:
        if not isinstance(value, bool):
            raise ValueError("功能开关必须是布尔值")
        return value

    ranges = {
        "output_compression": (0, 100),
        "request_timeout_seconds": (30, 900),
        "cooldown_seconds": (0, 86400),
        "max_prompt_chars": (1, 50000),
        "max_output_mib": (1, 100),
        "max_reference_images": (1, 10),
        "max_total_reference_mib": (15, 150),
        "max_reference_megapixels": (1, 100),
        "max_reference_edge": (512, 16384),
        "cache_max_images": (0, 20),
    }
    if key not in ranges:
        raise ValueError(f"未知设置项：{key}")
    minimum, maximum = ranges[key]
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} 必须是 {minimum} 到 {maximum} 的整数")
    return value


class WebAPI:
    """Register authenticated APIs consumed by the CanvasForge Plugin Page."""

    def __init__(
        self,
        context: Any,
        cache_store: CacheStore,
        get_settings: Callable[[], Mapping[str, Any] | Awaitable[Mapping[str, Any]]],
        save_settings: Callable[[dict[str, Any]], Any | Awaitable[Any]],
        begin_mutation: Callable[[], bool | Awaitable[bool]],
        end_mutation: Callable[[], Any | Awaitable[Any]],
        update_coordinator: UpdateCoordinator,
        plugin_version: str,
    ) -> None:
        self._context = context
        self._cache = cache_store
        self._get_settings_callback = get_settings
        self._save_settings_callback = save_settings
        self._begin_mutation_callback = begin_mutation
        self._end_mutation_callback = end_mutation
        self._updates = update_coordinator
        self._plugin_version = str(plugin_version)
        self._settings_save_lock = asyncio.Lock()
        self._registered = False
        self._active = True

    def register(self) -> None:
        """Register all routes once."""

        if self._registered:
            return
        routes = (
            (
                "/update/check",
                self.check_update,
                ["GET"],
                "Check CanvasForge release updates",
            ),
            (
                "/update/apply",
                self.apply_update,
                ["POST"],
                "Apply one verified CanvasForge update",
            ),
            (
                "/update/status",
                self.get_update_status,
                ["GET"],
                "Read CanvasForge update status",
            ),
            ("/settings", self.get_settings, ["GET"], "Get CanvasForge advanced settings"),
            ("/settings", self.save_settings, ["POST"], "Save CanvasForge advanced settings"),
            ("/cache", self.list_cache, ["GET"], "List CanvasForge image cache"),
            (
                "/cache/<cache_id>/thumbnail",
                self.get_thumbnail,
                ["GET"],
                "Read CanvasForge cache thumbnail",
            ),
            (
                "/cache/<cache_id>/preview",
                self.get_preview,
                ["GET"],
                "Read CanvasForge cache preview",
            ),
            (
                "/cache/<cache_id>/download",
                self.download,
                ["GET"],
                "Download CanvasForge cached image",
            ),
            (
                "/cache/<cache_id>/delete",
                self.delete,
                ["POST"],
                "Delete CanvasForge cached image",
            ),
            ("/cache/clear", self.clear, ["POST"], "Clear CanvasForge image cache"),
        )
        for suffix, handler, methods, description in routes:
            self._context.register_web_api(
                f"/{PLUGIN_NAME}{suffix}",
                handler,
                methods,
                description,
            )
        self._registered = True

    def deactivate(self) -> None:
        """Fail closed if AstrBot keeps old bound handlers after plugin unload."""

        self._active = False

    async def check_update(self):
        unauthorized = self._require_authenticated(no_store=True)
        if unauthorized is not None:
            return unauthorized
        force_value = request.query.get("force")
        force = str(force_value).strip().lower() in {"1", "true", "yes"}
        try:
            result = await self._updates.check(
                str(request.username),
                force=force,
            )
            return json_response(result, headers=_NO_STORE_HEADERS)
        except UpdateCoordinatorError as exc:
            return error_response(
                str(exc),
                status_code=exc.status_code,
                headers=_NO_STORE_HEADERS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return error_response(
                "无法检查 CanvasForge 更新，请稍后重试",
                status_code=500,
                headers=_NO_STORE_HEADERS,
            )

    async def apply_update(self):
        unauthorized = self._require_authenticated(no_store=True)
        if unauthorized is not None:
            return unauthorized
        payload = await request.json(default=None)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"check_id"}
            or not isinstance(payload.get("check_id"), str)
            or not payload["check_id"].strip()
            or len(payload["check_id"]) > 256
        ):
            return error_response(
                "请求只能包含有效的 check_id",
                status_code=400,
                headers=_NO_STORE_HEADERS,
            )
        staged_job_id = ""
        try:
            result = await self._updates.apply(
                str(request.username),
                payload["check_id"],
            )
            staged_job_id = str(result["job_id"])
            response = json_response(
                result,
                status_code=202,
                headers=_NO_STORE_HEADERS,
            )
            try:
                if not hasattr(response, "background"):
                    raise AttributeError
                if response.background is not None:
                    raise RuntimeError
                response.background = _StartUpdateAfterResponse(
                    self._updates,
                    staged_job_id,
                )
            except BaseException:
                await self._updates.abort_accepted(staged_job_id)
                staged_job_id = ""
                return error_response(
                    "当前 AstrBot 无法安全地在响应后启动更新，请使用原生插件管理更新。",
                    status_code=503,
                    headers=_NO_STORE_HEADERS,
                )
            staged_job_id = ""
            return response
        except UpdateCoordinatorError as exc:
            return error_response(
                str(exc),
                status_code=exc.status_code,
                headers=_NO_STORE_HEADERS,
            )
        except asyncio.CancelledError:
            if staged_job_id:
                await self._updates.abort_accepted(staged_job_id)
            raise
        except Exception:
            if staged_job_id:
                await self._updates.abort_accepted(staged_job_id)
            return error_response(
                "无法受理 CanvasForge 更新，请重新检查后再试",
                status_code=500,
                headers=_NO_STORE_HEADERS,
            )

    async def get_update_status(self):
        unauthorized = self._require_authenticated(
            allow_inactive=True,
            no_store=True,
        )
        if unauthorized is not None:
            return unauthorized
        try:
            result = await self._updates.status()
            return json_response(result, headers=_NO_STORE_HEADERS)
        except asyncio.CancelledError:
            raise
        except Exception:
            return error_response(
                "暂时无法读取 CanvasForge 更新状态",
                status_code=500,
                headers=_NO_STORE_HEADERS,
            )

    async def get_settings(self):
        unauthorized = self._require_authenticated()
        if unauthorized is not None:
            return unauthorized
        try:
            current = await self._call(self._get_settings_callback)
            payload = normalize_settings(current)
            payload["plugin_version"] = self._plugin_version
            return json_response(payload)
        except Exception:
            return error_response("无法读取生成设置", status_code=500)

    async def save_settings(self):
        unauthorized = self._require_authenticated()
        if unauthorized is not None:
            return unauthorized
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("设置内容必须是 JSON 对象", status_code=400)
        if not await self._reserve_mutation():
            return error_response("CanvasForge 正在更新，暂时不能保存设置", status_code=409)
        try:
            async with self._settings_save_lock:
                try:
                    current = await self._call(self._get_settings_callback)
                    merged = normalize_settings(current)
                    merged.update(payload)
                    merged = normalize_settings(merged, strict=True)
                    evicted = await self._call(
                        self._save_settings_callback,
                        merged,
                    )
                    if (
                        isinstance(evicted, bool)
                        or not isinstance(evicted, int)
                        or evicted < 0
                    ):
                        raise RuntimeError("invalid settings callback result")
                    return json_response(
                        {
                            "saved": True,
                            "settings": merged,
                            "evicted": evicted,
                        },
                    )
                except ValueError as exc:
                    return error_response(str(exc), status_code=400)
                except CacheError:
                    return error_response(
                        "生成设置已保存，但缓存调整失败；已暂停新增缓存，请重载插件后重试。",
                        status_code=500,
                    )
                except Exception:
                    return error_response("无法保存生成设置", status_code=500)
        finally:
            await self._call(self._end_mutation_callback)

    async def list_cache(self):
        unauthorized = self._require_authenticated()
        if unauthorized is not None:
            return unauthorized
        try:
            items = await self._cache.list()
            return json_response({"items": items, "count": len(items), "limit": self._cache.limit})
        except CacheError:
            return error_response("无法读取图片缓存", status_code=500)

    async def get_thumbnail(self, cache_id: str):
        unauthorized = self._require_authenticated()
        if unauthorized is not None:
            return unauthorized
        if not self._valid_cache_id(cache_id):
            return error_response("缓存图片不存在", status_code=404)
        try:
            data = await self._cache.read_thumbnail(cache_id)
            return json_response(
                {
                    "id": cache_id,
                    "mime_type": "image/webp",
                    "base64_data": base64.b64encode(data).decode("ascii"),
                }
            )
        except CacheNotFoundError:
            return error_response("缓存图片不存在", status_code=404)
        except CacheError:
            return error_response("无法读取缓存缩略图", status_code=500)

    async def get_preview(self, cache_id: str):
        unauthorized = self._require_authenticated()
        if unauthorized is not None:
            return unauthorized
        if not self._valid_cache_id(cache_id):
            return error_response("缓存图片不存在", status_code=404)
        try:
            data = await self._cache.render_preview(cache_id, max_edge=2048)
            return json_response(
                {
                    "id": cache_id,
                    "mime_type": "image/webp",
                    "base64_data": base64.b64encode(data).decode("ascii"),
                }
            )
        except CacheNotFoundError:
            return error_response("缓存图片不存在", status_code=404)
        except CacheError:
            return error_response("无法生成缓存预览图", status_code=500)

    async def download(self, cache_id: str):
        unauthorized = self._require_authenticated()
        if unauthorized is not None:
            return unauthorized
        if not self._valid_cache_id(cache_id):
            return error_response("缓存图片不存在", status_code=404)
        try:
            item = await self._cache.get(cache_id)
            if item is None:
                return error_response("缓存图片不存在", status_code=404)
            path = await self._cache.get_original_path(cache_id)
            extension = {
                "png": "png",
                "jpg": "jpg",
                "jpeg": "jpg",
                "webp": "webp",
            }.get(str(item.get("format", "")).lower(), "bin")
            return file_response(
                path,
                filename=f"canvasforge-{cache_id}.{extension}",
                content_type=str(item.get("content_type") or "application/octet-stream"),
            )
        except CacheNotFoundError:
            return error_response("缓存图片不存在", status_code=404)
        except CacheError:
            return error_response("无法下载缓存图片", status_code=500)

    async def delete(self, cache_id: str):
        unauthorized = self._require_authenticated()
        if unauthorized is not None:
            return unauthorized
        if not self._valid_cache_id(cache_id):
            return error_response("缓存图片不存在", status_code=404)
        if not await self._reserve_mutation():
            return error_response("CanvasForge 正在更新，暂时不能删除缓存", status_code=409)
        try:
            deleted = await self._cache.delete(cache_id)
            if not deleted:
                return error_response("缓存图片不存在", status_code=404)
            return json_response({"deleted": True, "id": cache_id})
        except CacheError:
            return error_response("无法删除缓存图片", status_code=500)
        finally:
            await self._call(self._end_mutation_callback)

    async def clear(self):
        unauthorized = self._require_authenticated()
        if unauthorized is not None:
            return unauthorized
        if not await self._reserve_mutation():
            return error_response("CanvasForge 正在更新，暂时不能清空缓存", status_code=409)
        try:
            removed = await self._cache.clear()
            return json_response({"cleared": True, "removed": removed})
        except CacheError:
            return error_response("无法清空图片缓存", status_code=500)
        finally:
            await self._call(self._end_mutation_callback)

    def _require_authenticated(
        self,
        *,
        allow_inactive: bool = False,
        no_store: bool = False,
    ):
        headers = _NO_STORE_HEADERS if no_store else None
        if not getattr(request, "username", None):
            return error_response(
                "未授权访问",
                status_code=401,
                headers=headers,
            )
        if not allow_inactive and not self._active:
            return error_response(
                "CanvasForge 已卸载或正在重载，请稍后刷新控制台",
                status_code=503,
                headers=headers,
            )
        return None

    @staticmethod
    def _valid_cache_id(cache_id: str) -> bool:
        return isinstance(cache_id, str) and bool(_CACHE_ID_RE.fullmatch(cache_id))

    @staticmethod
    async def _call(callback: Callable[..., Any], *args: Any) -> Any:
        result = callback(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _reserve_mutation(self) -> bool:
        """Transfer a Page mutation reservation without a cancellation leak."""

        operation = self._call(self._begin_mutation_callback)
        task = contextvars.Context().run(asyncio.create_task, operation)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc

        try:
            reserved = bool(task.result())
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise

        if cancellation is not None:
            if reserved:
                await self._call(self._end_mutation_callback)
            raise cancellation
        return reserved
