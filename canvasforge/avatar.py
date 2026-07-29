"""Resolve explicitly selected QQ avatars into validated reference images.

The chat model can select only symbolic targets (``mention:N``, ``sender`` or
``bot``).  QQ identifiers and avatar URLs always come from the current
aiocqhttp event and never from model-supplied arguments.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import unicodedata
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At

from .contracts import CanvasForgeError, ErrorCode, ReferenceImage


MIB = 1024 * 1024
DEFAULT_PER_IMAGE_BYTES = 15 * MIB
DEFAULT_MAX_PIXELS = 40_000_000
DEFAULT_MAX_EDGE = 8192

_AVATAR_TIMEOUT_SECONDS = 10
_GROUP_MEMBER_TIMEOUT_SECONDS = 3
_MAX_REDIRECTS = 3
_HTTP_CHUNK_BYTES = 64 * 1024
_MAX_DISPLAY_NAME_CHARS = 64
_MAX_SELECTOR_CHARS = 32
_MAX_MENTION_DIGITS = 9
_SELECTOR_PATTERN = re.compile(r"mention:([1-9][0-9]*)\Z")
_QQ_PATTERN = re.compile(r"[1-9][0-9]{0,19}\Z")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_TENCENT_IMAGE_DOMAINS = ("qlogo.cn", "qpic.cn", "gtimg.cn")
_FORMAT_INFO = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
}


class _AvatarProblem(Exception):
    """Internal avatar failure that carries no identifying information."""


class _DownloadByteBudget:
    """Atomically cap bytes buffered by concurrent avatar downloads."""

    def __init__(self, remaining_bytes: int) -> None:
        self._remaining_bytes = remaining_bytes
        self._lock = asyncio.Lock()

    async def reserve(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("avatar byte count is invalid")
        async with self._lock:
            if byte_count > self._remaining_bytes:
                raise CanvasForgeError(
                    ErrorCode.REFERENCE_LIMIT,
                    "引用图片与人物头像合计大小超过当前限制，请减少后重试。",
                )
            self._remaining_bytes -= byte_count


@dataclass(frozen=True, slots=True)
class _InspectedImage:
    mime_type: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class AvatarTarget:
    """A validated, event-derived avatar target awaiting download."""

    selector: str
    user_id: str
    name_hint: str
    fallback_name: str


@dataclass(frozen=True, slots=True)
class ResolvedAvatar:
    """A downloaded avatar and its safe prompt-facing identity label."""

    selector: str
    display_name: str
    reference: ReferenceImage


class AvatarResolver:
    """Plan symbolic avatar targets and download them through a shared session."""

    def __init__(self, context: Any, session: aiohttp.ClientSession) -> None:
        self._context = context
        self._session = session

    def plan(
        self,
        event: AstrMessageEvent,
        selectors: Sequence[str] | None,
    ) -> list[AvatarTarget]:
        """Resolve ordered selectors to event-owned QQ identities.

        This method performs no network access.  Call it before resolving
        quoted images so malformed model arguments are rejected before any
        other work begins.
        """

        if selectors is None:
            return []
        if isinstance(selectors, (str, bytes)) or not isinstance(
            selectors,
            Sequence,
        ):
            raise self._invalid_target_error()
        if not selectors:
            return []

        sender_id = self._event_qq(event, "get_sender_id")
        self_id = self._event_qq(event, "get_self_id")
        group_id = self._safe_event_value(event, "get_group_id")

        component_mentions, component_names = self._component_mentions(
            event,
            self_id,
        )
        raw_available, raw_mentions = self._raw_mentions(event, self_id)
        mentions = raw_mentions if raw_available else component_mentions

        sender_name = self._clean_display_name(
            self._safe_event_value(event, "get_sender_name"),
        )
        bot_name = component_names.get(self_id, "") if self_id else ""

        planned: list[AvatarTarget] = []
        seen_users: set[str] = set()
        for supplied_selector in selectors:
            if (
                not isinstance(supplied_selector, str)
                or len(supplied_selector) > _MAX_SELECTOR_CHARS
            ):
                raise self._invalid_target_error()
            selector = supplied_selector.strip().lower()

            if selector == "sender":
                if not sender_id:
                    raise self._invalid_target_error()
                user_id = sender_id
                name_hint = sender_name
                fallback_name = "发送者"
            elif selector == "bot":
                if not self_id:
                    raise self._invalid_target_error()
                user_id = self_id
                name_hint = bot_name
                fallback_name = "机器人"
            else:
                match = _SELECTOR_PATTERN.fullmatch(selector)
                if match is None:
                    raise self._invalid_target_error()
                if not group_id:
                    raise CanvasForgeError(
                        ErrorCode.AVATAR_TARGET_INVALID,
                        "mention:N 只能用于群聊中的直接 @ 群友；"
                        "私聊请使用 sender 或 bot。",
                    )
                ordinal_text = match.group(1)
                if len(ordinal_text) > _MAX_MENTION_DIGITS:
                    raise self._invalid_target_error()
                try:
                    ordinal = int(ordinal_text)
                except ValueError:
                    raise self._invalid_target_error() from None
                if ordinal > len(mentions):
                    if mentions:
                        detail = (
                            f"当前消息只有 {len(mentions)} 个可用的直接 @ 群友"
                        )
                    else:
                        detail = "当前消息没有可用的直接 @ 群友"
                    raise CanvasForgeError(
                        ErrorCode.AVATAR_TARGET_INVALID,
                        f"{detail}；发送者请用 sender，机器人请用 bot，"
                        "不要把两者计入 mention:N。",
                    )
                user_id = mentions[ordinal - 1]
                name_hint = component_names.get(user_id, "")
                fallback_name = f"群友{ordinal}"

            if user_id in seen_users:
                raise CanvasForgeError(
                    ErrorCode.AVATAR_TARGET_INVALID,
                    "头像人物选择包含重复身份，请只选择每个人一次。",
                )
            seen_users.add(user_id)
            planned.append(
                AvatarTarget(
                    selector=selector,
                    user_id=user_id,
                    name_hint=self._clean_display_name(name_hint),
                    fallback_name=fallback_name,
                ),
            )

        return planned

    async def download(
        self,
        event: AstrMessageEvent,
        targets: Sequence[AvatarTarget],
        *,
        filename_start_index: int,
        consumed_bytes: int,
        max_total_bytes: int,
        per_image_bytes: int = DEFAULT_PER_IMAGE_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        max_edge: int = DEFAULT_MAX_EDGE,
    ) -> list[ResolvedAvatar]:
        """Download targets concurrently while preserving their input order."""

        self._validate_download_limits(
            filename_start_index=filename_start_index,
            consumed_bytes=consumed_bytes,
            max_total_bytes=max_total_bytes,
            per_image_bytes=per_image_bytes,
            max_pixels=max_pixels,
            max_edge=max_edge,
        )
        if isinstance(targets, (str, bytes)) or not isinstance(
            targets,
            Sequence,
        ):
            raise self._invalid_target_error()
        if not targets:
            return []
        if consumed_bytes >= max_total_bytes:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_LIMIT,
                "引用图片与人物头像合计大小超过当前限制，请减少后重试。",
            )

        byte_budget = _DownloadByteBudget(
            max_total_bytes - consumed_bytes,
        )
        jobs = [
            self._resolve_one(
                event,
                target,
                filename_index=filename_start_index + offset,
                byte_budget=byte_budget,
                per_image_bytes=per_image_bytes,
                max_pixels=max_pixels,
                max_edge=max_edge,
            )
            for offset, target in enumerate(targets)
        ]
        results = await asyncio.gather(*jobs, return_exceptions=True)

        resolved: list[ResolvedAvatar] = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, CanvasForgeError):
                raise result
            if isinstance(result, BaseException):
                raise self._unavailable_error() from None
            resolved.append(result)

        avatar_bytes = sum(len(item.reference.data) for item in resolved)
        if consumed_bytes + avatar_bytes > max_total_bytes:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_LIMIT,
                "引用图片与人物头像合计大小超过当前限制，请减少后重试。",
            )
        return resolved

    async def _resolve_one(
        self,
        event: AstrMessageEvent,
        target: AvatarTarget,
        *,
        filename_index: int,
        byte_budget: _DownloadByteBudget,
        per_image_bytes: int,
        max_pixels: int,
        max_edge: int,
    ) -> ResolvedAvatar:
        if not isinstance(target, AvatarTarget) or not self._valid_qq(
            target.user_id,
        ):
            raise self._invalid_target_error()

        image_task = asyncio.create_task(
            self._download_avatar(
                target.user_id,
                byte_limit=per_image_bytes,
                byte_budget=byte_budget,
            ),
        )
        name_task = asyncio.create_task(self._display_name(event, target))
        try:
            image_data, display_name = await asyncio.gather(
                image_task,
                name_task,
            )
            inspected = await asyncio.to_thread(
                self._inspect_image,
                image_data,
                max_pixels,
                max_edge,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except CanvasForgeError:
            raise
        except Exception:
            raise self._unavailable_error() from None
        finally:
            for task in (image_task, name_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                image_task,
                name_task,
                return_exceptions=True,
            )

        return ResolvedAvatar(
            selector=target.selector,
            display_name=display_name,
            reference=ReferenceImage(
                data=image_data,
                mime_type=inspected.mime_type,
                filename=(
                    f"reference_{filename_index}.{inspected.extension}"
                ),
                width=inspected.width,
                height=inspected.height,
            ),
        )

    async def _download_avatar(
        self,
        user_id: str,
        *,
        byte_limit: int,
        byte_budget: _DownloadByteBudget,
    ) -> bytes:
        if not self._valid_qq(user_id):
            raise self._invalid_target_error()

        current_url = (
            "https://q.qlogo.cn/headimg_dl"
            f"?dst_uin={user_id}&spec=640&img_type=jpg"
        )
        try:
            async with asyncio.timeout(_AVATAR_TIMEOUT_SECONDS):
                for redirect_count in range(_MAX_REDIRECTS + 1):
                    self._validate_avatar_url(current_url)
                    async with self._session.get(
                        current_url,
                        allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(
                            total=_AVATAR_TIMEOUT_SECONDS,
                        ),
                    ) as response:
                        if response.status in _REDIRECT_STATUSES:
                            if redirect_count >= _MAX_REDIRECTS:
                                raise _AvatarProblem("too many redirects")
                            location = response.headers.get("Location")
                            if not location:
                                raise _AvatarProblem("missing redirect")
                            current_url = urljoin(current_url, location)
                            self._validate_avatar_url(current_url)
                            continue

                        if response.status < 200 or response.status >= 300:
                            raise _AvatarProblem("download status")
                        if (
                            response.content_length is not None
                            and response.content_length > byte_limit
                        ):
                            raise _AvatarProblem("body too large")

                        data = bytearray()
                        async for chunk in response.content.iter_chunked(
                            _HTTP_CHUNK_BYTES,
                        ):
                            if len(data) + len(chunk) > byte_limit:
                                raise _AvatarProblem("body too large")
                            await byte_budget.reserve(len(chunk))
                            data.extend(chunk)
                        if not data:
                            raise _AvatarProblem("empty body")
                        return bytes(data)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except CanvasForgeError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError, _AvatarProblem):
            raise self._unavailable_error() from None

        raise self._unavailable_error()

    async def _display_name(
        self,
        event: AstrMessageEvent,
        target: AvatarTarget,
    ) -> str:
        hinted = self._clean_display_name(target.name_hint)
        if hinted:
            return hinted

        group_id = self._safe_event_value(event, "get_group_id")
        if group_id:
            looked_up = await self._lookup_group_member_name(
                event,
                group_id=group_id,
                user_id=target.user_id,
            )
            if looked_up:
                return looked_up
        return target.fallback_name

    async def _lookup_group_member_name(
        self,
        event: AstrMessageEvent,
        *,
        group_id: str,
        user_id: str,
    ) -> str:
        try:
            client = self._client_for_event(event)
            parameters: dict[str, Any] = {
                "group_id": self._onebot_scalar(group_id),
                "user_id": self._onebot_scalar(user_id),
                "no_cache": False,
            }
            self_id = self._safe_event_value(event, "get_self_id")
            if self_id:
                parameters["self_id"] = self._onebot_scalar(self_id)

            async with asyncio.timeout(_GROUP_MEMBER_TIMEOUT_SECONDS):
                payload = await self._call_action(
                    client,
                    "get_group_member_info",
                    **parameters,
                )
            if not isinstance(payload, Mapping):
                return ""
            nested = payload.get("data")
            if isinstance(nested, Mapping):
                payload = nested
            for key in ("card", "nickname", "nick"):
                value = payload.get(key)
                if isinstance(value, str):
                    cleaned = self._clean_display_name(value)
                    if cleaned:
                        return cleaned
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            # A name is optional.  Do not expose or log the target identity.
            return ""
        return ""

    def _client_for_event(self, event: AstrMessageEvent) -> Any:
        client = getattr(event, "bot", None)
        if self._supports_call_action(client):
            return client

        platform_id = self._safe_event_value(event, "get_platform_id")
        get_platform_inst = getattr(self._context, "get_platform_inst", None)
        if not platform_id or not callable(get_platform_inst):
            raise RuntimeError("current aiocqhttp client is unavailable")
        platform = get_platform_inst(platform_id)
        get_client = getattr(platform, "get_client", None)
        if not callable(get_client):
            raise RuntimeError("current aiocqhttp client is unavailable")
        client = get_client()
        if not self._supports_call_action(client):
            raise RuntimeError("current aiocqhttp client is unavailable")
        return client

    @staticmethod
    def _supports_call_action(client: Any) -> bool:
        if client is None:
            return False
        if callable(getattr(client, "call_action", None)):
            return True
        return callable(
            getattr(getattr(client, "api", None), "call_action", None),
        )

    @staticmethod
    async def _call_action(
        client: Any,
        action: str,
        **parameters: Any,
    ) -> Any:
        call_action = getattr(client, "call_action", None)
        if not callable(call_action):
            call_action = getattr(
                getattr(client, "api", None),
                "call_action",
                None,
            )
        if not callable(call_action):
            raise RuntimeError("aiocqhttp call_action is unavailable")
        result = call_action(action=action, **parameters)
        if inspect.isawaitable(result):
            return await result
        return result

    @classmethod
    def _raw_mentions(
        cls,
        event: AstrMessageEvent,
        self_id: str,
    ) -> tuple[bool, list[str]]:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, Mapping):
            raw_chain = raw_message.get("message")
        else:
            raw_chain = getattr(raw_message, "message", None)
        if not isinstance(raw_chain, list):
            return False, []

        mentions: list[str] = []
        seen: set[str] = set()
        for segment in raw_chain:
            if not isinstance(segment, Mapping):
                continue
            if str(segment.get("type", "")).lower() != "at":
                continue
            data = segment.get("data")
            if not isinstance(data, Mapping):
                continue
            qq = cls._normalize_qq(data.get("qq"))
            if not qq or qq == self_id or qq in seen:
                continue
            seen.add(qq)
            mentions.append(qq)
        return True, mentions

    @classmethod
    def _component_mentions(
        cls,
        event: AstrMessageEvent,
        self_id: str,
    ) -> tuple[list[str], dict[str, str]]:
        get_messages = getattr(event, "get_messages", None)
        try:
            messages = (
                get_messages()
                if callable(get_messages)
                else getattr(getattr(event, "message_obj", None), "message", [])
            )
        except Exception:
            messages = []
        if not isinstance(messages, Sequence) or isinstance(
            messages,
            (str, bytes),
        ):
            return [], {}

        mentions: list[str] = []
        names: dict[str, str] = {}
        seen: set[str] = set()
        for component in messages:
            if not isinstance(component, At):
                continue
            qq = cls._normalize_qq(getattr(component, "qq", None))
            if not qq:
                continue
            name = cls._clean_display_name(
                getattr(component, "name", ""),
            )
            if name and qq not in names:
                names[qq] = name
            if qq == self_id or qq in seen:
                continue
            seen.add(qq)
            mentions.append(qq)
        return mentions, names

    @staticmethod
    def _inspect_image(
        data: bytes,
        max_pixels: int,
        max_edge: int,
    ) -> _InspectedImage:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(
                    "ignore",
                    PillowImage.DecompressionBombWarning,
                )
                with PillowImage.open(BytesIO(data)) as image:
                    image_format = str(image.format or "").upper()
                    if image_format not in _FORMAT_INFO:
                        raise _AvatarProblem("unsupported format")

                    width, height = image.size
                    if width <= 0 or height <= 0:
                        raise _AvatarProblem("invalid dimensions")
                    if width > max_edge or height > max_edge:
                        raise _AvatarProblem("edge limit")
                    if width * height > max_pixels:
                        raise _AvatarProblem("pixel limit")
                    if (
                        bool(getattr(image, "is_animated", False))
                        or int(getattr(image, "n_frames", 1)) != 1
                    ):
                        raise _AvatarProblem("animated image")
                    image.verify()

                with PillowImage.open(BytesIO(data)) as decoded:
                    decoded.load()

            mime_type, extension = _FORMAT_INFO[image_format]
            return _InspectedImage(
                mime_type=mime_type,
                extension=extension,
                width=width,
                height=height,
            )
        except _AvatarProblem:
            raise
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            PillowImage.DecompressionBombError,
            PillowImage.DecompressionBombWarning,
        ):
            raise _AvatarProblem("invalid image") from None

    @classmethod
    def _event_qq(cls, event: AstrMessageEvent, method_name: str) -> str:
        return cls._normalize_qq(cls._safe_event_value(event, method_name))

    @staticmethod
    def _safe_event_value(event: Any, method_name: str) -> str:
        method = getattr(event, method_name, None)
        if not callable(method):
            return ""
        try:
            value = method()
        except Exception:
            return ""
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _normalize_qq(value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if text.lower() == "all" or not _QQ_PATTERN.fullmatch(text):
            return ""
        return text

    @staticmethod
    def _valid_qq(value: Any) -> bool:
        return isinstance(value, str) and bool(_QQ_PATTERN.fullmatch(value))

    @staticmethod
    def _clean_display_name(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        visible = "".join(
            character
            for character in value
            if not unicodedata.category(character).startswith("C")
        )
        return " ".join(visible.split())[:_MAX_DISPLAY_NAME_CHARS]

    @classmethod
    def _validate_avatar_url(cls, url: str) -> None:
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except (TypeError, ValueError):
            raise _AvatarProblem("invalid avatar URL") from None
        if (
            parsed.scheme.lower() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or not any(
                hostname == domain or hostname.endswith(f".{domain}")
                for domain in _TENCENT_IMAGE_DOMAINS
            )
        ):
            raise _AvatarProblem("disallowed avatar redirect")

    @staticmethod
    def _onebot_scalar(value: Any) -> int | str:
        text = str(value).strip()
        return int(text) if text.isascii() and text.isdigit() else text

    @staticmethod
    def _validate_download_limits(
        *,
        filename_start_index: int,
        consumed_bytes: int,
        max_total_bytes: int,
        per_image_bytes: int,
        max_pixels: int,
        max_edge: int,
    ) -> None:
        if (
            filename_start_index < 1
            or consumed_bytes < 0
            or max_total_bytes < 1
            or per_image_bytes < 1
            or max_pixels < 1
            or max_edge < 1
        ):
            raise ValueError("avatar reference limits are invalid")

    @staticmethod
    def _invalid_target_error() -> CanvasForgeError:
        return CanvasForgeError(
            ErrorCode.AVATAR_TARGET_INVALID,
            "头像人物选择无效：发送者请用 sender，机器人请用 bot；"
            "mention:N 只表示当前群消息中排除机器人唤醒 @ 后的"
            "第 N 个直接 @ 群友。",
        )

    @staticmethod
    def _unavailable_error() -> CanvasForgeError:
        return CanvasForgeError(
            ErrorCode.AVATAR_UNAVAILABLE,
            "无法获取所选人物的 QQ 头像，请稍后重试。",
        )
