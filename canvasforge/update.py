"""Pinned GitHub Release checks and guarded AstrBot self-updates."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
import inspect
import json
import math
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import yaml
from astrbot.api import logger
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


PLUGIN_NAME = "astrbot_plugin_canvasforge"
PLUGIN_AUTHOR = "YuXya"
PLUGIN_REPOSITORY = "https://github.com/YuXya/astrbot_plugin_canvasforge"
GITHUB_REPOSITORY = "YuXya/astrbot_plugin_canvasforge"

_GITHUB_API_ROOT = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
_ARCHIVE_ROOT = f"https://github.com/{GITHUB_REPOSITORY}/archive"
_RUNTIME_CONTEXT_KEY = "_canvasforge_update_runtime_v1"
_RUNTIME_SCHEMA = 1
_STATUS_SCHEMA = 1
_STATUS_FILENAME = "update-status.json"
_STATUS_TEMP_FILENAME = "update-status.json.tmp"

_VERSION_RE = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ACTIVE_STATES = {"accepted", "updating", "verifying"}
_TERMINAL_STATES = {"succeeded", "failed", "interrupted"}
_KNOWN_STATES = {"idle", *_ACTIVE_STATES, *_TERMINAL_STATES}

_CHECK_SUCCESS_TTL_SECONDS = 15 * 60
_CHECK_ERROR_TTL_SECONDS = 60
_MANUAL_CHECK_INTERVAL_SECONDS = 60
_CHECK_ID_TTL_SECONDS = 10 * 60
_CHECK_TOTAL_TIMEOUT_SECONDS = 30
_POST_RESPONSE_START_TIMEOUT_SECONDS = 30
_MAX_TICKETS = 128
_MAX_STATUS_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_MAX_GITHUB_JSON_BYTES = 1024 * 1024

_NO_RELEASE = object()


class UpdateCoordinatorError(Exception):
    """A classified, Page-safe update error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = int(status_code)
        self.retry_after = retry_after


class _VerificationFailure(Exception):
    """Internal marker whose details must never cross the Page boundary."""


def _new_runtime() -> dict[str, Any]:
    return {
        "schema": _RUNTIME_SCHEMA,
        "lock": asyncio.Lock(),
        "status_lock": asyncio.Lock(),
        "init_lock": asyncio.Lock(),
        "initialized": False,
        "storage_available": False,
        "status": None,
        "epoch": 0,
        "check_task": None,
        "check_task_epoch": None,
        "check_cache": None,
        "tickets": {},
        "manual_checks": {},
        "apply_pending": False,
        "apply_owner": None,
        "maintenance_owner": None,
        "accepted_update": None,
        "accepted_watchdog": None,
        "update_task": None,
        "update_job_id": None,
    }


def _get_runtime(context: Any) -> dict[str, Any]:
    current = getattr(context, _RUNTIME_CONTEXT_KEY, None)
    if (
        isinstance(current, dict)
        and current.get("schema") == _RUNTIME_SCHEMA
        and hasattr(current.get("lock"), "__aenter__")
        and hasattr(current.get("status_lock"), "__aenter__")
        and hasattr(current.get("init_lock"), "__aenter__")
    ):
        defaults = _new_runtime()
        for key, value in defaults.items():
            current.setdefault(key, value)
        return current

    runtime = _new_runtime()
    setattr(context, _RUNTIME_CONTEXT_KEY, runtime)
    return runtime


def _blank_context_task(operation: Awaitable[Any]) -> asyncio.Task[Any]:
    return contextvars.Context().run(asyncio.create_task, operation)


async def _await_cancellation_safe(operation: Awaitable[Any]) -> Any:
    """Finish one operation before propagating cancellation to its caller."""

    task = _blank_context_task(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    try:
        result = task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation from None
        raise
    if cancellation is not None:
        raise cancellation
    return result


async def _call(callback: Callable[..., Any], *args: Any) -> Any:
    result = callback(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _bounded_text(value: Any, *, maximum: int, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    if not normalized:
        return fallback
    return normalized[:maximum]


def _parse_plugin_version(value: str) -> Version:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise UpdateCoordinatorError(
            "invalid_local_metadata",
            "本地插件版本格式无效，已禁用页内更新；请从插件管理检查安装。",
            status_code=500,
        )
    try:
        return Version(value[1:])
    except InvalidVersion:
        raise UpdateCoordinatorError(
            "invalid_local_metadata",
            "本地插件版本格式无效，已禁用页内更新；请从插件管理检查安装。",
            status_code=500,
        ) from None


def _normalise_release_time(value: Any) -> str:
    candidate = _bounded_text(value, maximum=64)
    if not candidate:
        raise UpdateCoordinatorError(
            "invalid_release",
            "GitHub Release 的发布时间无效，未执行更新。",
            status_code=502,
        )
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        raise UpdateCoordinatorError(
            "invalid_release",
            "GitHub Release 的发布时间无效，未执行更新。",
            status_code=502,
        ) from None
    return candidate


def _public_message(state: str) -> str:
    return {
        "accepted": "更新任务已受理，正在准备。",
        "updating": "正在更新插件，控制台可能会短暂断开。",
        "verifying": "代码已更新，正在验证插件重载结果。",
        "succeeded": "CanvasForge 已更新并完成重载验证。",
        "failed": (
            "更新失败；旧版本可能仍可用。如插件异常，请从 AstrBot 插件管理"
            "按固定 GitHub 地址重新安装。"
        ),
        "interrupted": "上次更新被中断，暂时无法确认结果；请重新检查版本。",
    }.get(state, "")


class UpdateCoordinator:
    """Coordinate checks, one-use tickets, status recovery, and self-update."""

    def __init__(
        self,
        context: Any,
        data_dir: Path,
        http_session_getter: Callable[
            [],
            aiohttp.ClientSession | Awaitable[aiohttp.ClientSession],
        ],
        local_metadata_path: Path,
        *,
        local_version: str,
        astrbot_version: str,
        reserve_update: Callable[[str], bool | Awaitable[bool]],
        release_update: Callable[[str], Any | Awaitable[Any]],
    ) -> None:
        self._context = context
        self._data_dir = Path(data_dir)
        self._status_path = self._data_dir / _STATUS_FILENAME
        self._status_temp_path = self._data_dir / _STATUS_TEMP_FILENAME
        self._http_session_getter = http_session_getter
        self._local_metadata_path = Path(local_metadata_path)
        self._local_version = str(local_version)
        self._astrbot_version = str(astrbot_version)
        self._reserve_update_callback = reserve_update
        self._release_update_callback = release_update
        self._runtime = _get_runtime(context)
        self._instance_init_lock = asyncio.Lock()
        self._instance_initialized = False
        self._active = True

    async def initialize(self) -> None:
        """Prepare the one fixed status journal and activate this instance."""

        async with self._instance_init_lock:
            if self._instance_initialized:
                return
            await self._initialize_shared_runtime()
            await self._activate_instance()
            self._instance_initialized = True

    async def deactivate(self) -> None:
        """Invalidate old checks without touching a live update task."""

        self._active = False
        check_task: asyncio.Task[Any] | None = None
        async with self._runtime["lock"]:
            self._runtime["epoch"] = int(self._runtime["epoch"]) + 1
            self._runtime["tickets"].clear()
            self._runtime["check_cache"] = None
            self._runtime["apply_pending"] = False
            self._runtime["apply_owner"] = None
            candidate = self._runtime.get("check_task")
            if isinstance(candidate, asyncio.Task) and not candidate.done():
                check_task = candidate
            self._runtime["check_task"] = None
            self._runtime["check_task_epoch"] = None
        if check_task is not None:
            check_task.cancel()

    async def check(self, username: str, *, force: bool = False) -> dict[str, Any]:
        """Return a cached or fresh strict Release check for one Dashboard user."""

        await self.initialize()
        if not self._active:
            raise UpdateCoordinatorError(
                "inactive",
                "CanvasForge 已卸载或正在重载，请稍后刷新控制台。",
                status_code=503,
            )
        normalized_user = _bounded_text(username, maximum=256)
        if not normalized_user:
            raise UpdateCoordinatorError(
                "unauthorized",
                "未授权访问。",
                status_code=401,
            )

        now = time.monotonic()
        async with self._runtime["lock"]:
            self._prune_runtime_locked(now)
            if self._update_is_active_locked():
                raise UpdateCoordinatorError(
                    "busy",
                    "CanvasForge 正在更新，请稍后再试。",
                    status_code=409,
                )

            if force:
                last_manual = self._runtime["manual_checks"].get(normalized_user)
                if isinstance(last_manual, (int, float)):
                    remaining = _MANUAL_CHECK_INTERVAL_SECONDS - (
                        now - float(last_manual)
                    )
                    if remaining > 0:
                        retry_after = max(1, math.ceil(remaining))
                        raise UpdateCoordinatorError(
                            "check_throttled",
                            f"重新检查过于频繁，请在 {retry_after} 秒后再试。",
                            status_code=429,
                            retry_after=retry_after,
                        )
                self._runtime["manual_checks"][normalized_user] = now

            cached = self._runtime.get("check_cache")
            if not force and self._cache_is_fresh(cached, now):
                fact = self._fact_from_cache(cached)
                return self._issue_public_result_locked(
                    fact,
                    normalized_user,
                    now,
                )

            epoch = int(self._runtime["epoch"])
            check_task = self._runtime.get("check_task")
            if not isinstance(check_task, asyncio.Task) or check_task.done():
                check_task = _blank_context_task(
                    self._run_shared_check(epoch)
                )
                self._runtime["check_task"] = check_task
                self._runtime["check_task_epoch"] = epoch

        try:
            fact = await asyncio.shield(check_task)
        except asyncio.CancelledError:
            raise
        except UpdateCoordinatorError:
            raise
        except Exception:
            raise UpdateCoordinatorError(
                "github_unavailable",
                "无法连接 GitHub 检查更新，请稍后重试，或使用 AstrBot 原生更新入口。",
                status_code=502,
            ) from None

        now = time.monotonic()
        async with self._runtime["lock"]:
            if (
                epoch != int(self._runtime["epoch"])
                or self._update_is_active_locked()
                or not self._active
            ):
                raise UpdateCoordinatorError(
                    "stale_check",
                    "检查结果已失效，请重新检查更新。",
                    status_code=409,
                )
            return self._issue_public_result_locked(
                fact,
                normalized_user,
                now,
            )

    async def apply(self, username: str, check_id: str) -> dict[str, Any]:
        """Consume one ticket and stage an update for post-response startup."""

        await self.initialize()
        if not self._active:
            raise UpdateCoordinatorError(
                "inactive",
                "CanvasForge 已卸载或正在重载，请稍后刷新控制台。",
                status_code=503,
            )
        normalized_user = _bounded_text(username, maximum=256)
        normalized_check_id = _bounded_text(check_id, maximum=256)
        if not normalized_user or not normalized_check_id:
            raise UpdateCoordinatorError(
                "invalid_request",
                "check_id 格式无效，请重新检查更新。",
                status_code=400,
            )

        updater, unavailable_reason = self._compatible_updater()
        if updater is None or not self._runtime.get("storage_available"):
            raise UpdateCoordinatorError(
                "updater_unavailable",
                unavailable_reason
                or "页内更新当前不可用，请使用 AstrBot 原生插件管理更新。",
                status_code=503,
            )

        ticket: dict[str, Any]
        claim_owner = secrets.token_hex(16)
        maintenance_owner = secrets.token_hex(16)
        claim_epoch = 0
        now = time.monotonic()
        async with self._runtime["lock"]:
            self._prune_runtime_locked(now)
            if self._runtime.get("apply_pending") or self._update_is_active_locked():
                raise UpdateCoordinatorError(
                    "busy",
                    "CanvasForge 已有更新任务正在处理。",
                    status_code=409,
                )
            candidate = self._runtime["tickets"].get(normalized_check_id)
            if (
                not isinstance(candidate, dict)
                or candidate.get("username") != normalized_user
                or candidate.get("current_version") != self._local_version
                or float(candidate.get("expires_at", 0)) <= now
                or _SHA_RE.fullmatch(str(candidate.get("commit_sha", ""))) is None
            ):
                raise UpdateCoordinatorError(
                    "stale_check",
                    "检查结果已失效，请重新检查更新。",
                    status_code=409,
                )
            ticket = dict(candidate)
            self._runtime["tickets"].pop(normalized_check_id, None)
            self._runtime["apply_pending"] = True
            self._runtime["apply_owner"] = claim_owner
            claim_epoch = int(self._runtime["epoch"])

        job_id = secrets.token_hex(16)
        reserved = False
        handed_off = False
        try:
            reserved = await self._reserve_update_safely(maintenance_owner)
            if not reserved:
                raise UpdateCoordinatorError(
                    "busy",
                    "当前有生成任务或页面写操作正在进行，请稍后重新检查更新。",
                    status_code=409,
                )

            check_task: asyncio.Task[Any] | None = None
            update_epoch = 0
            async with self._runtime["lock"]:
                if (
                    not self._active
                    or int(self._runtime["epoch"]) != claim_epoch
                    or self._runtime.get("apply_owner") != claim_owner
                    or self._runtime.get("apply_pending") is not True
                ):
                    raise UpdateCoordinatorError(
                        "stale_apply",
                        "插件已卸载或重载，本次更新申请已取消。",
                        status_code=409,
                    )
                update_epoch = claim_epoch + 1
                self._runtime["epoch"] = update_epoch
                self._runtime["maintenance_owner"] = maintenance_owner
                self._runtime["tickets"].clear()
                self._runtime["check_cache"] = None
                candidate = self._runtime.get("check_task")
                if isinstance(candidate, asyncio.Task) and not candidate.done():
                    check_task = candidate
                self._runtime["check_task"] = None
                self._runtime["check_task_epoch"] = None
            if check_task is not None:
                check_task.cancel()

            accepted = self._status_payload(
                job_id=job_id,
                state="accepted",
                current_version=self._local_version,
                target_version=str(ticket["target_version"]),
                release_title=str(ticket.get("release_title", "")),
                published_at=str(ticket.get("published_at", "")),
            )
            if not await self._record_status(accepted):
                raise UpdateCoordinatorError(
                    "status_unavailable",
                    "无法安全保存更新状态，未开始更新；请使用 AstrBot 原生更新入口。",
                    status_code=500,
                )

            async with self._runtime["lock"]:
                if (
                    not self._active
                    or int(self._runtime["epoch"]) != update_epoch
                    or self._runtime.get("apply_owner") != claim_owner
                    or self._runtime.get("apply_pending") is not True
                ):
                    raise UpdateCoordinatorError(
                        "stale_apply",
                        "插件已卸载或重载，本次更新申请已取消。",
                        status_code=409,
                    )
                self._runtime["accepted_update"] = {
                    "job_id": job_id,
                    "ticket": dict(ticket),
                    "updater": updater,
                    "maintenance_owner": maintenance_owner,
                }
                watchdog_operation = self._expire_accepted(job_id)
                try:
                    watchdog = _blank_context_task(watchdog_operation)
                except BaseException:
                    watchdog_operation.close()
                    self._runtime["accepted_update"] = None
                    raise UpdateCoordinatorError(
                        "background_unavailable",
                        "无法安全安排后台更新，请使用 AstrBot 原生插件管理更新。",
                        status_code=503,
                    ) from None
                self._runtime["accepted_watchdog"] = watchdog
                self._runtime["apply_pending"] = False
                self._runtime["apply_owner"] = None
                handed_off = True

            return {
                "accepted": True,
                "state": "accepted",
                "job_id": job_id,
                "current_version": self._local_version,
                "target_version": str(ticket["target_version"]),
                "message": _public_message("accepted"),
            }
        finally:
            if reserved and not handed_off:
                failed = self._status_payload(
                    job_id=job_id,
                    state="failed",
                    current_version=self._local_version,
                    target_version=str(ticket["target_version"]),
                    release_title=str(ticket.get("release_title", "")),
                    published_at=str(ticket.get("published_at", "")),
                )
                await self._run_cleanup(self._record_status(failed))
                await self._release_maintenance(maintenance_owner)
            await self._clear_apply_pending(claim_owner)

    async def start_accepted(self, job_id: str) -> bool:
        """Start a staged update after the HTTP 202 body has been sent."""

        return bool(
            await self._run_cleanup(
                self._start_accepted(job_id),
            )
        )

    async def _start_accepted(self, job_id: str) -> bool:
        normalized_job_id = (
            job_id
            if isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id)
            else ""
        )
        if not normalized_job_id:
            return False

        failed_pending: dict[str, Any] | None = None
        watchdog: asyncio.Task[Any] | None = None
        async with self._runtime["lock"]:
            live_task = self._runtime.get("update_task")
            if isinstance(live_task, asyncio.Task) and not live_task.done():
                return self._runtime.get("update_job_id") == normalized_job_id

            candidate = self._runtime.get("accepted_update")
            if (
                not isinstance(candidate, dict)
                or candidate.get("job_id") != normalized_job_id
                or not isinstance(candidate.get("ticket"), dict)
                or not callable(candidate.get("updater"))
                or _JOB_ID_RE.fullmatch(
                    str(candidate.get("maintenance_owner", ""))
                )
                is None
            ):
                return False

            self._runtime["accepted_update"] = None
            candidate_watchdog = self._runtime.get("accepted_watchdog")
            if isinstance(candidate_watchdog, asyncio.Task):
                watchdog = candidate_watchdog
            self._runtime["accepted_watchdog"] = None
            if not self._active:
                failed_pending = dict(candidate)
            else:
                operation = self._run_update(
                    job_id=normalized_job_id,
                    ticket=dict(candidate["ticket"]),
                    updater=candidate["updater"],
                    maintenance_owner=str(candidate["maintenance_owner"]),
                )
                try:
                    task = _blank_context_task(operation)
                except BaseException:
                    operation.close()
                    failed_pending = dict(candidate)
                else:
                    self._runtime["update_task"] = task
                    self._runtime["update_job_id"] = normalized_job_id
                    if watchdog is not None:
                        watchdog.cancel()
                    return True

        if watchdog is not None:
            watchdog.cancel()
        if failed_pending is not None:
            await self._fail_staged_update(failed_pending)
        return False

    async def abort_accepted(self, job_id: str) -> bool:
        """Abort a staged update if no safe response hook is available."""

        return bool(
            await self._run_cleanup(
                self._abort_accepted(job_id),
            )
        )

    async def _abort_accepted(
        self,
        job_id: str,
        *,
        cancel_watchdog: bool = True,
    ) -> bool:
        normalized_job_id = (
            job_id
            if isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id)
            else ""
        )
        if not normalized_job_id:
            return False

        pending: dict[str, Any] | None = None
        watchdog: asyncio.Task[Any] | None = None
        async with self._runtime["lock"]:
            candidate = self._runtime.get("accepted_update")
            if (
                isinstance(candidate, dict)
                and candidate.get("job_id") == normalized_job_id
            ):
                pending = dict(candidate)
                self._runtime["accepted_update"] = None
                candidate_watchdog = self._runtime.get("accepted_watchdog")
                if isinstance(candidate_watchdog, asyncio.Task):
                    watchdog = candidate_watchdog
                self._runtime["accepted_watchdog"] = None

        if pending is None:
            return False
        current_task = asyncio.current_task()
        if (
            cancel_watchdog
            and watchdog is not None
            and watchdog is not current_task
        ):
            watchdog.cancel()
        await self._fail_staged_update(pending)
        return True

    async def _expire_accepted(self, job_id: str) -> None:
        """Release maintenance if the HTTP response callback never runs."""

        try:
            await asyncio.sleep(_POST_RESPONSE_START_TIMEOUT_SECONDS)
            await self._run_cleanup(
                self._abort_accepted(
                    job_id,
                    cancel_watchdog=False,
                )
            )
        except asyncio.CancelledError:
            return
        except BaseException:
            # abort_accepted owns the cancellation-safe cleanup path.
            return

    async def _fail_staged_update(self, pending: Mapping[str, Any]) -> None:
        ticket = pending.get("ticket")
        if not isinstance(ticket, Mapping):
            ticket = {}
        maintenance_owner = str(pending.get("maintenance_owner", ""))
        failed = self._status_payload(
            job_id=str(pending.get("job_id", "")),
            state="failed",
            current_version=self._local_version,
            target_version=str(ticket.get("target_version", "")),
            release_title=str(ticket.get("release_title", "")),
            published_at=str(ticket.get("published_at", "")),
        )
        try:
            await self._record_status(failed)
        finally:
            await self._release_maintenance(maintenance_owner)

    async def status(self) -> dict[str, Any]:
        """Return only the public, reload-recoverable update status."""

        await self.initialize()
        async with self._runtime["status_lock"]:
            candidate = self._runtime.get("status")
            if isinstance(candidate, dict):
                status = dict(candidate)
            else:
                status = self._status_payload(
                    job_id="",
                    state="idle",
                    current_version=self._local_version,
                    target_version="",
                    release_title="",
                    published_at="",
                )
        status["current_version"] = self._local_version
        return self._public_status(status)

    async def _initialize_shared_runtime(self) -> None:
        async with self._runtime["init_lock"]:
            if self._runtime.get("initialized"):
                return

            status: dict[str, Any] | None = None
            needs_write = False
            try:
                status, needs_write = await _await_cancellation_safe(
                    asyncio.to_thread(self._prepare_status_storage_sync)
                )
                self._runtime["storage_available"] = True
            except Exception as exc:
                self._runtime["storage_available"] = False
                logger.error(
                    "CanvasForge update status storage is unavailable (%s).",
                    type(exc).__name__,
                )

            if status is None:
                status = self._status_payload(
                    job_id="",
                    state="idle",
                    current_version=self._local_version,
                    target_version="",
                    release_title="",
                    published_at="",
                )

            if status.get("state") in _ACTIVE_STATES:
                status = self._status_payload(
                    job_id=str(status.get("job_id", "")),
                    state="interrupted",
                    current_version=self._local_version,
                    target_version=str(status.get("target_version", "")),
                    release_title=str(status.get("release_title", "")),
                    published_at=str(status.get("published_at", "")),
                )
                needs_write = True
            elif (
                status.get("state") == "succeeded"
                and status.get("current_version") != self._local_version
            ):
                status = self._status_payload(
                    job_id=str(status.get("job_id", "")),
                    state="interrupted",
                    current_version=self._local_version,
                    target_version=str(status.get("target_version", "")),
                    release_title=str(status.get("release_title", "")),
                    published_at=str(status.get("published_at", "")),
                )
                needs_write = True

            async with self._runtime["status_lock"]:
                self._runtime["status"] = status
                if (
                    needs_write
                    and self._runtime.get("storage_available")
                ):
                    try:
                        await _await_cancellation_safe(
                            asyncio.to_thread(
                                self._write_status_sync,
                                status,
                            )
                        )
                    except Exception as exc:
                        self._runtime["storage_available"] = False
                        logger.error(
                            "CanvasForge could not recover update status (%s).",
                            type(exc).__name__,
                        )
            self._runtime["initialized"] = True

    async def _activate_instance(self) -> None:
        check_task: asyncio.Task[Any] | None = None
        accepted_watchdog: asyncio.Task[Any] | None = None
        stale_maintenance_owner = ""
        stale_active_status = False
        async with self._runtime["lock"]:
            update_task = self._runtime.get("update_task")
            live_update = (
                isinstance(update_task, asyncio.Task)
                and not update_task.done()
            )
            if not live_update:
                self._runtime["epoch"] = int(self._runtime["epoch"]) + 1
                self._runtime["tickets"].clear()
                self._runtime["check_cache"] = None
                candidate = self._runtime.get("check_task")
                if isinstance(candidate, asyncio.Task) and not candidate.done():
                    check_task = candidate
                self._runtime["check_task"] = None
                self._runtime["check_task_epoch"] = None
                self._runtime["apply_pending"] = False
                self._runtime["apply_owner"] = None
                self._runtime["accepted_update"] = None
                candidate_watchdog = self._runtime.get("accepted_watchdog")
                if isinstance(candidate_watchdog, asyncio.Task):
                    accepted_watchdog = candidate_watchdog
                self._runtime["accepted_watchdog"] = None
                self._runtime["update_job_id"] = None
                candidate_owner = str(
                    self._runtime.get("maintenance_owner", "")
                )
                if _JOB_ID_RE.fullmatch(candidate_owner):
                    stale_maintenance_owner = candidate_owner
                current_status = self._runtime.get("status")
                stale_active_status = (
                    isinstance(current_status, dict)
                    and current_status.get("state") in _ACTIVE_STATES
                )
        if check_task is not None:
            check_task.cancel()
        if accepted_watchdog is not None:
            accepted_watchdog.cancel()
        if stale_active_status or stale_maintenance_owner:
            try:
                if stale_active_status:
                    async with self._runtime["status_lock"]:
                        previous = dict(self._runtime.get("status") or {})
                    interrupted = self._status_payload(
                        job_id=str(previous.get("job_id", "")),
                        state="interrupted",
                        current_version=self._local_version,
                        target_version=str(previous.get("target_version", "")),
                        release_title=str(previous.get("release_title", "")),
                        published_at=str(previous.get("published_at", "")),
                    )
                    await self._record_status(interrupted)
            finally:
                if stale_maintenance_owner:
                    await self._release_maintenance(
                        stale_maintenance_owner
                    )

    def _prepare_status_storage_sync(
        self,
    ) -> tuple[dict[str, Any] | None, bool]:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._status_temp_path.unlink(missing_ok=True)
        except OSError:
            # A stale fixed temp file must not force a versioned fallback name.
            raise

        if not self._status_path.exists():
            return None, False
        stat = self._status_path.stat()
        if stat.st_size <= 0 or stat.st_size > _MAX_STATUS_BYTES:
            return (
                self._status_payload(
                    job_id="",
                    state="interrupted",
                    current_version=self._local_version,
                    target_version="",
                    release_title="",
                    published_at="",
                ),
                True,
            )
        try:
            payload = json.loads(self._status_path.read_text(encoding="utf-8"))
            return self._sanitize_status(payload), False
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return (
                self._status_payload(
                    job_id="",
                    state="interrupted",
                    current_version=self._local_version,
                    target_version="",
                    release_title="",
                    published_at="",
                ),
                True,
            )

    def _sanitize_status(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("invalid status")
        if payload.get("schema") != _STATUS_SCHEMA:
            raise ValueError("unsupported status schema")
        state = str(payload.get("state", ""))
        if state not in _KNOWN_STATES:
            raise ValueError("invalid status state")
        job_id = str(payload.get("job_id", ""))
        if job_id and _JOB_ID_RE.fullmatch(job_id) is None:
            raise ValueError("invalid job id")
        current_version = str(payload.get("current_version", ""))
        target_version = str(payload.get("target_version", ""))
        if current_version and _VERSION_RE.fullmatch(current_version) is None:
            raise ValueError("invalid current version")
        if target_version and _VERSION_RE.fullmatch(target_version) is None:
            raise ValueError("invalid target version")
        return {
            "schema": _STATUS_SCHEMA,
            "job_id": job_id,
            "state": state,
            "current_version": current_version or self._local_version,
            "target_version": target_version,
            "release_title": _bounded_text(
                payload.get("release_title"),
                maximum=200,
            ),
            "published_at": _bounded_text(
                payload.get("published_at"),
                maximum=64,
            ),
            "message": _bounded_text(payload.get("message"), maximum=300),
            "updated_at": _bounded_text(
                payload.get("updated_at"),
                maximum=64,
                fallback=_utc_now(),
            ),
        }

    def _write_status_sync(self, status: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            dict(status),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._status_temp_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(self._status_temp_path, self._status_path)
        except Exception:
            try:
                self._status_temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def _record_status(self, status: dict[str, Any]) -> bool:
        async with self._runtime["status_lock"]:
            self._runtime["status"] = dict(status)
            if not self._runtime.get("storage_available"):
                return False
            try:
                await _await_cancellation_safe(
                    asyncio.to_thread(self._write_status_sync, status)
                )
                return True
            except Exception as exc:
                self._runtime["storage_available"] = False
                logger.error(
                    "CanvasForge could not persist update status (%s).",
                    type(exc).__name__,
                )
                return False

    def _status_payload(
        self,
        *,
        job_id: str,
        state: str,
        current_version: str,
        target_version: str,
        release_title: str,
        published_at: str,
    ) -> dict[str, Any]:
        return {
            "schema": _STATUS_SCHEMA,
            "job_id": job_id if _JOB_ID_RE.fullmatch(job_id) else "",
            "state": state if state in _KNOWN_STATES else "interrupted",
            "current_version": current_version,
            "target_version": target_version,
            "release_title": _bounded_text(release_title, maximum=200),
            "published_at": _bounded_text(published_at, maximum=64),
            "message": _public_message(state),
            "updated_at": _utc_now(),
        }

    @staticmethod
    def _public_status(status: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "state": str(status.get("state", "idle")),
            "job_id": str(status.get("job_id", "")),
            "current_version": str(status.get("current_version", "")),
            "target_version": str(status.get("target_version", "")),
            "release_title": str(status.get("release_title", "")),
            "published_at": str(status.get("published_at", "")),
            "message": str(status.get("message", "")),
            "updated_at": str(status.get("updated_at", "")),
        }

    async def _run_shared_check(
        self,
        expected_epoch: int,
    ) -> dict[str, Any]:
        owner_task = asyncio.current_task()
        cache: dict[str, Any] | None = None
        error: UpdateCoordinatorError | None = None
        try:
            fact = await self._perform_check(expected_epoch)
        except asyncio.CancelledError:
            await _await_cancellation_safe(
                self._commit_shared_check(
                    owner_task,
                    expected_epoch,
                    None,
                )
            )
            raise
        except UpdateCoordinatorError as exc:
            error = exc
            cache = {
                "expires_at": time.monotonic() + _CHECK_ERROR_TTL_SECONDS,
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "status_code": exc.status_code,
                    "retry_after": exc.retry_after,
                },
            }
            fact = {}
        else:
            cache = {
                "expires_at": time.monotonic() + _CHECK_SUCCESS_TTL_SECONDS,
                "ok": True,
                "fact": dict(fact),
            }

        await _await_cancellation_safe(
            self._commit_shared_check(
                owner_task,
                expected_epoch,
                cache,
            )
        )
        if error is not None:
            raise error
        return fact

    async def _commit_shared_check(
        self,
        owner_task: asyncio.Task[Any] | None,
        expected_epoch: int,
        cache: dict[str, Any] | None,
    ) -> None:
        async with self._runtime["lock"]:
            if self._runtime.get("check_task") is owner_task:
                if (
                    cache is not None
                    and expected_epoch == int(self._runtime["epoch"])
                    and not self._update_is_active_locked()
                    and self._active
                ):
                    self._runtime["check_cache"] = cache
                self._runtime["check_task"] = None
                self._runtime["check_task_epoch"] = None

    @staticmethod
    def _cache_is_fresh(cache: Any, now: float) -> bool:
        return (
            isinstance(cache, dict)
            and isinstance(cache.get("expires_at"), (int, float))
            and float(cache["expires_at"]) > now
        )

    @staticmethod
    def _fact_from_cache(cache: Mapping[str, Any]) -> dict[str, Any]:
        if cache.get("ok") is True and isinstance(cache.get("fact"), dict):
            return dict(cache["fact"])
        error = cache.get("error")
        if isinstance(error, Mapping):
            raise UpdateCoordinatorError(
                str(error.get("code") or "github_unavailable"),
                str(error.get("message") or "无法检查 GitHub 更新。"),
                status_code=int(error.get("status_code") or 502),
                retry_after=(
                    int(error["retry_after"])
                    if isinstance(error.get("retry_after"), int)
                    else None
                ),
            )
        raise UpdateCoordinatorError(
            "github_unavailable",
            "无法检查 GitHub 更新。",
            status_code=502,
        )

    def _prune_runtime_locked(self, now: float) -> None:
        tickets = self._runtime["tickets"]
        for check_id, ticket in list(tickets.items()):
            if (
                not isinstance(ticket, dict)
                or float(ticket.get("expires_at", 0)) <= now
            ):
                tickets.pop(check_id, None)

        manual_checks = self._runtime["manual_checks"]
        for username, checked_at in list(manual_checks.items()):
            if (
                not isinstance(checked_at, (int, float))
                or now - float(checked_at) > 3600
            ):
                manual_checks.pop(username, None)
        if len(manual_checks) > _MAX_TICKETS:
            oldest = sorted(
                manual_checks,
                key=lambda key: float(manual_checks[key]),
            )
            for username in oldest[: len(manual_checks) - _MAX_TICKETS]:
                manual_checks.pop(username, None)

        cache = self._runtime.get("check_cache")
        if (
            isinstance(cache, dict)
            and float(cache.get("expires_at", 0)) <= now
        ):
            self._runtime["check_cache"] = None

    def _update_is_active_locked(self) -> bool:
        task = self._runtime.get("update_task")
        return bool(
            self._runtime.get("apply_pending")
            or _JOB_ID_RE.fullmatch(
                str(self._runtime.get("maintenance_owner", ""))
            )
            is not None
            or isinstance(self._runtime.get("accepted_update"), dict)
            or (isinstance(task, asyncio.Task) and not task.done())
        )

    def _issue_public_result_locked(
        self,
        fact: Mapping[str, Any],
        username: str,
        now: float,
    ) -> dict[str, Any]:
        status = str(fact.get("status", ""))
        current_version = str(fact.get("current_version", self._local_version))
        target_version = str(fact.get("target_version", ""))
        result: dict[str, Any] = {
            "status": status,
            "current_version": current_version,
            "target_version": target_version,
            "release_title": str(fact.get("release_title", "")),
            "published_at": str(fact.get("published_at", "")),
            "message": {
                "no_release": "仓库尚无正式 Release。",
                "up_to_date": "已是最新版。",
                "ahead": "当前版本高于仓库最新正式 Release。",
                "incompatible": "发现新版本，但当前 AstrBot 版本不满足要求。",
                "update_available": "发现可用更新。",
            }.get(status, "更新检查已完成。"),
        }
        required = str(fact.get("required_astrbot_version", ""))
        if required:
            result["required_astrbot_version"] = required

        if status != "update_available":
            return result

        updater, unavailable_reason = self._compatible_updater()
        can_apply = bool(
            updater is not None
            and self._runtime.get("storage_available")
        )
        result["can_apply"] = can_apply
        result["updater_available"] = updater is not None
        if not can_apply:
            result["unavailable_reason"] = (
                unavailable_reason
                or "无法使用页内更新，请使用 AstrBot 原生插件管理。"
            )
            return result

        check_id = secrets.token_urlsafe(32)
        self._runtime["tickets"][check_id] = {
            "username": username,
            "current_version": current_version,
            "target_version": target_version,
            "commit_sha": str(fact["commit_sha"]),
            "release_title": str(fact.get("release_title", "")),
            "published_at": str(fact.get("published_at", "")),
            "expires_at": now + _CHECK_ID_TTL_SECONDS,
            "created_at": now,
        }
        if len(self._runtime["tickets"]) > _MAX_TICKETS:
            oldest = sorted(
                self._runtime["tickets"],
                key=lambda key: float(
                    self._runtime["tickets"][key].get("created_at", 0)
                ),
            )
            for stale_id in oldest[
                : len(self._runtime["tickets"]) - _MAX_TICKETS
            ]:
                self._runtime["tickets"].pop(stale_id, None)
        result["check_id"] = check_id
        return result

    async def _perform_check(self, _expected_epoch: int) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self._fetch_release_fact(),
                timeout=_CHECK_TOTAL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise UpdateCoordinatorError(
                "github_timeout",
                "GitHub 更新检查超时，请稍后重试，或使用 AstrBot 原生更新入口。",
                status_code=504,
            ) from None
        except asyncio.CancelledError:
            raise
        except UpdateCoordinatorError:
            raise
        except (aiohttp.ClientError, RuntimeError):
            raise UpdateCoordinatorError(
                "github_unavailable",
                "无法连接 GitHub 检查更新，请稍后重试，或使用 AstrBot 原生更新入口。",
                status_code=502,
            ) from None
        except Exception:
            raise UpdateCoordinatorError(
                "invalid_release",
                "GitHub 返回了无法验证的更新信息，未执行更新。",
                status_code=502,
            ) from None

    async def _fetch_release_fact(self) -> dict[str, Any]:
        await asyncio.to_thread(self._validate_local_metadata_sync)
        local_version = _parse_plugin_version(self._local_version)
        try:
            astrbot_version = Version(self._astrbot_version.lstrip("v"))
        except InvalidVersion:
            raise UpdateCoordinatorError(
                "invalid_astrbot_version",
                "无法识别当前 AstrBot 版本，已禁用页内更新。",
                status_code=500,
            ) from None

        release = await self._github_json(
            f"{_GITHUB_API_ROOT}/releases/latest",
            allow_not_found=True,
            maximum_bytes=_MAX_GITHUB_JSON_BYTES,
        )
        if release is _NO_RELEASE:
            return {
                "status": "no_release",
                "current_version": self._local_version,
                "target_version": "",
                "release_title": "",
                "published_at": "",
                "required_astrbot_version": "",
                "commit_sha": "",
            }
        if not isinstance(release, Mapping):
            raise UpdateCoordinatorError(
                "invalid_release",
                "GitHub Release 响应格式无效，未执行更新。",
                status_code=502,
            )
        draft = release.get("draft")
        prerelease = release.get("prerelease")
        if draft is True or prerelease is True:
            return {
                "status": "no_release",
                "current_version": self._local_version,
                "target_version": "",
                "release_title": "",
                "published_at": "",
                "required_astrbot_version": "",
                "commit_sha": "",
            }
        if draft is not False or prerelease is not False:
            raise UpdateCoordinatorError(
                "invalid_release",
                "GitHub Release 状态字段无效，未执行更新。",
                status_code=502,
            )

        raw_tag = release.get("tag_name")
        if (
            not isinstance(raw_tag, str)
            or not raw_tag
            or len(raw_tag) > 64
            or _VERSION_RE.fullmatch(raw_tag) is None
        ):
            raise UpdateCoordinatorError(
                "invalid_release",
                "最新正式 Release 的 Tag 不是 vMAJOR.MINOR.PATCH，未执行更新。",
                status_code=502,
            )
        tag = raw_tag
        try:
            target_version = Version(tag[1:])
        except InvalidVersion:
            raise UpdateCoordinatorError(
                "invalid_release",
                "最新正式 Release 的 Tag 版本无效，未执行更新。",
                status_code=502,
            ) from None
        release_title = _bounded_text(
            release.get("name"),
            maximum=200,
            fallback=tag,
        )
        published_at = _normalise_release_time(release.get("published_at"))

        commit_sha = await self._resolve_tag_commit(tag)
        remote_metadata = await self._read_remote_metadata(commit_sha)
        required_astrbot = self._validate_remote_metadata(
            remote_metadata,
            tag,
        )
        try:
            required_spec = SpecifierSet(required_astrbot)
        except InvalidSpecifier:
            raise UpdateCoordinatorError(
                "invalid_release",
                "远端 metadata.yaml 的 AstrBot 版本要求无效，未执行更新。",
                status_code=502,
            ) from None

        if target_version < local_version:
            status = "ahead"
        elif target_version == local_version:
            status = "up_to_date"
        elif astrbot_version not in required_spec:
            status = "incompatible"
        else:
            status = "update_available"

        return {
            "status": status,
            "current_version": self._local_version,
            "target_version": tag,
            "release_title": release_title,
            "published_at": published_at,
            "required_astrbot_version": required_astrbot,
            "commit_sha": commit_sha,
        }

    def _validate_local_metadata_sync(self) -> None:
        try:
            stat = self._local_metadata_path.stat()
            if stat.st_size <= 0 or stat.st_size > _MAX_METADATA_BYTES:
                raise ValueError("invalid metadata size")
            payload = yaml.safe_load(
                self._local_metadata_path.read_text(encoding="utf-8")
            )
        except (
            OSError,
            UnicodeDecodeError,
            yaml.YAMLError,
            ValueError,
        ):
            raise UpdateCoordinatorError(
                "invalid_local_metadata",
                "本地插件元数据无效，已禁用页内更新；请从插件管理检查安装。",
                status_code=500,
            ) from None
        if not isinstance(payload, Mapping):
            raise UpdateCoordinatorError(
                "invalid_local_metadata",
                "本地插件元数据无效，已禁用页内更新；请从插件管理检查安装。",
                status_code=500,
            )
        if (
            payload.get("name") != PLUGIN_NAME
            or payload.get("author") != PLUGIN_AUTHOR
            or payload.get("repo") != PLUGIN_REPOSITORY
            or payload.get("version") != self._local_version
        ):
            raise UpdateCoordinatorError(
                "invalid_local_metadata",
                "本地插件元数据与代码常量不一致，已禁用页内更新。",
                status_code=500,
            )
        _parse_plugin_version(self._local_version)

    async def _resolve_tag_commit(self, tag: str) -> str:
        reference = await self._github_json(
            f"{_GITHUB_API_ROOT}/git/ref/tags/{quote(tag, safe='')}",
            maximum_bytes=256 * 1024,
        )
        if not isinstance(reference, Mapping):
            raise UpdateCoordinatorError(
                "invalid_release",
                "无法把 Release Tag 固定到 Git Commit，未执行更新。",
                status_code=502,
            )
        target = reference.get("object")
        seen: set[str] = set()
        for _ in range(8):
            if not isinstance(target, Mapping):
                break
            object_type = str(target.get("type", ""))
            sha = str(target.get("sha", ""))
            if _SHA_RE.fullmatch(sha) is None or sha.lower() in seen:
                break
            normalized_sha = sha.lower()
            seen.add(normalized_sha)
            if object_type == "commit":
                return normalized_sha
            if object_type != "tag":
                break
            tag_object = await self._github_json(
                f"{_GITHUB_API_ROOT}/git/tags/{normalized_sha}",
                maximum_bytes=256 * 1024,
            )
            if not isinstance(tag_object, Mapping):
                break
            target = tag_object.get("object")
        raise UpdateCoordinatorError(
            "invalid_release",
            "无法把 Release Tag 固定到 Git Commit，未执行更新。",
            status_code=502,
        )

    async def _read_remote_metadata(self, commit_sha: str) -> Mapping[str, Any]:
        payload = await self._github_json(
            f"{_GITHUB_API_ROOT}/contents/metadata.yaml",
            params={"ref": commit_sha},
            maximum_bytes=256 * 1024,
        )
        if (
            not isinstance(payload, Mapping)
            or payload.get("type") != "file"
            or payload.get("encoding") != "base64"
            or not isinstance(payload.get("content"), str)
        ):
            raise UpdateCoordinatorError(
                "invalid_release",
                "远端 metadata.yaml 响应无效，未执行更新。",
                status_code=502,
            )
        compact = "".join(str(payload["content"]).split())
        try:
            decoded = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            raise UpdateCoordinatorError(
                "invalid_release",
                "远端 metadata.yaml 编码无效，未执行更新。",
                status_code=502,
            ) from None
        if not decoded or len(decoded) > _MAX_METADATA_BYTES:
            raise UpdateCoordinatorError(
                "invalid_release",
                "远端 metadata.yaml 大小无效，未执行更新。",
                status_code=502,
            )
        try:
            metadata = yaml.safe_load(decoded.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            raise UpdateCoordinatorError(
                "invalid_release",
                "远端 metadata.yaml 内容无效，未执行更新。",
                status_code=502,
            ) from None
        if not isinstance(metadata, Mapping):
            raise UpdateCoordinatorError(
                "invalid_release",
                "远端 metadata.yaml 内容无效，未执行更新。",
                status_code=502,
            )
        return metadata

    @staticmethod
    def _validate_remote_metadata(
        metadata: Mapping[str, Any],
        release_tag: str,
    ) -> str:
        if (
            metadata.get("name") != PLUGIN_NAME
            or metadata.get("author") != PLUGIN_AUTHOR
            or metadata.get("repo") != PLUGIN_REPOSITORY
            or metadata.get("version") != release_tag
        ):
            raise UpdateCoordinatorError(
                "invalid_release",
                "Release Tag 与远端插件元数据不一致，未执行更新。",
                status_code=502,
            )
        raw_required = metadata.get("astrbot_version")
        if (
            not isinstance(raw_required, str)
            or not raw_required
            or raw_required != raw_required.strip()
            or len(raw_required) > 128
        ):
            raise UpdateCoordinatorError(
                "invalid_release",
                "远端 metadata.yaml 缺少 AstrBot 版本要求，未执行更新。",
                status_code=502,
            )
        return raw_required

    async def _github_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        allow_not_found: bool = False,
        maximum_bytes: int,
    ) -> Any:
        session = await _call(self._http_session_getter)
        if (
            not isinstance(session, aiohttp.ClientSession)
            or session.closed
        ):
            raise UpdateCoordinatorError(
                "github_unavailable",
                "GitHub 检查连接不可用，请稍后重试。",
                status_code=502,
            )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"CanvasForge/{self._local_version}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with session.get(
                url,
                params=dict(params or {}),
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=_CHECK_TOTAL_TIMEOUT_SECONDS
                ),
                allow_redirects=True,
            ) as response:
                if response.status == 404 and allow_not_found:
                    response.release()
                    return _NO_RELEASE
                if response.status in {403, 429}:
                    response.release()
                    raise UpdateCoordinatorError(
                        "github_limited",
                        "GitHub 暂时限制了更新检查，请稍后重试，或使用 AstrBot 原生更新入口。",
                        status_code=502,
                    )
                if response.status != 200:
                    response.release()
                    raise UpdateCoordinatorError(
                        "github_unavailable",
                        "GitHub 更新检查失败，请稍后重试，或使用 AstrBot 原生更新入口。",
                        status_code=502,
                    )
                content_length = response.content_length
                if (
                    content_length is not None
                    and content_length > maximum_bytes
                ):
                    raise UpdateCoordinatorError(
                        "invalid_release",
                        "GitHub 更新信息体积异常，未执行更新。",
                        status_code=502,
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > maximum_bytes:
                        raise UpdateCoordinatorError(
                            "invalid_release",
                            "GitHub 更新信息体积异常，未执行更新。",
                            status_code=502,
                        )
        except asyncio.CancelledError:
            raise
        except UpdateCoordinatorError:
            raise
        except (aiohttp.ClientError, RuntimeError):
            raise UpdateCoordinatorError(
                "github_unavailable",
                "无法连接 GitHub 检查更新，请稍后重试，或使用 AstrBot 原生更新入口。",
                status_code=502,
            ) from None

        try:
            return json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise UpdateCoordinatorError(
                "invalid_release",
                "GitHub 返回了无法解析的更新信息，未执行更新。",
                status_code=502,
            ) from None

    def _compatible_updater(
        self,
    ) -> tuple[Callable[..., Any] | None, str]:
        unavailable = (
            "当前 AstrBot 不提供兼容的页内更新接口，请使用原生插件管理更新。"
        )
        manager = getattr(self._context, "_star_manager", None)
        updater = getattr(manager, "update_plugin", None)
        if not callable(updater) or not inspect.iscoroutinefunction(updater):
            return None, unavailable
        try:
            signature = inspect.signature(updater)
        except (TypeError, ValueError):
            return None, unavailable

        parameters = signature.parameters
        expected = {"plugin_name", "proxy", "download_url"}
        if set(parameters) != expected:
            return None, unavailable
        if parameters["plugin_name"].kind not in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            return None, unavailable
        for name in ("proxy", "download_url"):
            if parameters[name].kind not in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }:
                return None, unavailable
            if parameters[name].default != "":
                return None, unavailable
        if parameters["plugin_name"].default is not inspect.Parameter.empty:
            return None, unavailable
        return updater, ""

    async def _reserve_update_safely(self, maintenance_owner: str) -> bool:
        operation = _call(
            self._reserve_update_callback,
            maintenance_owner,
        )
        task = _blank_context_task(operation)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc

        try:
            reserved = bool(task.result())
        except asyncio.CancelledError:
            if cancellation is not None:
                raise cancellation from None
            raise
        except Exception:
            if cancellation is not None:
                raise cancellation from None
            raise UpdateCoordinatorError(
                "maintenance_unavailable",
                "无法进入安全更新状态，请稍后重试。",
                status_code=500,
            ) from None
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise

        if cancellation is not None:
            if reserved:
                await self._release_maintenance(maintenance_owner)
            raise cancellation
        return reserved

    async def _clear_apply_pending(self, claim_owner: str) -> None:
        async def clear() -> None:
            async with self._runtime["lock"]:
                if self._runtime.get("apply_owner") == claim_owner:
                    self._runtime["apply_pending"] = False
                    self._runtime["apply_owner"] = None

        await self._run_cleanup(clear())

    async def _release_maintenance(self, maintenance_owner: str) -> None:
        if _JOB_ID_RE.fullmatch(str(maintenance_owner)) is None:
            return
        try:
            await self._run_cleanup(
                _call(
                    self._release_update_callback,
                    maintenance_owner,
                )
            )
        except Exception as exc:
            logger.error(
                "CanvasForge could not release update maintenance (%s).",
                type(exc).__name__,
            )
            return

        async def clear_owner() -> None:
            async with self._runtime["lock"]:
                if (
                    self._runtime.get("maintenance_owner")
                    == maintenance_owner
                ):
                    self._runtime["maintenance_owner"] = None

        await self._run_cleanup(clear_owner())

    @staticmethod
    async def _run_cleanup(operation: Awaitable[Any]) -> Any:
        task = _blank_context_task(operation)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    async def _run_update(
        self,
        *,
        job_id: str,
        ticket: Mapping[str, Any],
        updater: Callable[..., Any],
        maintenance_owner: str,
    ) -> None:
        target_version = str(ticket["target_version"])
        release_title = str(ticket.get("release_title", ""))
        published_at = str(ticket.get("published_at", ""))
        terminal_state = "failed"
        current_version = self._local_version
        try:
            updating = self._status_payload(
                job_id=job_id,
                state="updating",
                current_version=self._local_version,
                target_version=target_version,
                release_title=release_title,
                published_at=published_at,
            )
            if not await self._record_status(updating):
                raise _VerificationFailure

            commit_sha = str(ticket.get("commit_sha", "")).lower()
            if _SHA_RE.fullmatch(commit_sha) is None:
                raise _VerificationFailure
            download_url = f"{_ARCHIVE_ROOT}/{commit_sha}.zip"
            result = updater(
                PLUGIN_NAME,
                proxy="",
                download_url=download_url,
            )
            if not inspect.isawaitable(result):
                raise _VerificationFailure
            await result

            verifying = self._status_payload(
                job_id=job_id,
                state="verifying",
                current_version=self._local_version,
                target_version=target_version,
                release_title=release_title,
                published_at=published_at,
            )
            await self._record_status(verifying)
            await self._verify_reloaded_plugin(target_version)
            terminal_state = "succeeded"
            current_version = target_version
        except asyncio.CancelledError:
            terminal_state = "interrupted"
        except Exception as exc:
            terminal_state = "failed"
            logger.error(
                "CanvasForge background update failed (%s).",
                type(exc).__name__,
            )
        finally:
            terminal = self._status_payload(
                job_id=job_id,
                state=terminal_state,
                current_version=current_version,
                target_version=target_version,
                release_title=release_title,
                published_at=published_at,
            )
            await self._run_cleanup(self._record_status(terminal))
            await self._release_maintenance(maintenance_owner)

            owner_task = asyncio.current_task()

            async def clear_task() -> None:
                async with self._runtime["lock"]:
                    if self._runtime.get("update_task") is owner_task:
                        self._runtime["update_task"] = None
                        self._runtime["update_job_id"] = None
                    self._runtime["apply_pending"] = False

            await self._run_cleanup(clear_task())

    async def _verify_reloaded_plugin(self, target_version: str) -> None:
        getter = getattr(self._context, "get_registered_star", None)
        if not callable(getter):
            raise _VerificationFailure
        metadata = await _call(getter, PLUGIN_NAME)
        if metadata is None:
            raise _VerificationFailure
        if (
            getattr(metadata, "name", None) != PLUGIN_NAME
            or getattr(metadata, "author", None) != PLUGIN_AUTHOR
            or getattr(metadata, "repo", None) != PLUGIN_REPOSITORY
            or getattr(metadata, "version", None) != target_version
            or getattr(metadata, "activated", None) is not True
            or getattr(metadata, "star_cls", None) is None
        ):
            raise _VerificationFailure
