"""Resolve direct NapCat reply images without trusting local filesystem paths."""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

import aiohttp
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Reply

from .contracts import CanvasForgeError, ErrorCode, ReferenceImage


MIB = 1024 * 1024
DEFAULT_PER_IMAGE_BYTES = 15 * MIB
DEFAULT_MAX_PIXELS = 40_000_000
DEFAULT_MAX_EDGE = 8192

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=30)
_REFRESH_TIMEOUT_SECONDS = 10
_GROUP_INFO_TIMEOUT_SECONDS = 3
_HTTP_CHUNK_BYTES = 64 * 1024
_FORMAT_INFO = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
}


class _ReferenceProblem(Exception):
    """Internal, non-sensitive validation failure."""


class _RefreshableReferenceProblem(_ReferenceProblem):
    """A missing or stale source that a single get_msg call may repair."""


class _ReferenceLimitProblem(_ReferenceProblem):
    """The aggregate reference-byte limit was exceeded."""


class _RefreshProblem(Exception):
    """The quoted OneBot message could not be refreshed."""


@dataclass(frozen=True, slots=True)
class _ImageSource:
    source: str | None


@dataclass(frozen=True, slots=True)
class ReferenceSnapshot:
    """Portable direct-reply image sources captured before background work.

    The snapshot deliberately retains neither the AstrBot event nor a local
    filesystem path. Source values are limited by ``snapshot()`` to HTTP(S),
    inline Base64, or data URIs, so a worker can resolve them after the
    initiating event has finished.
    """

    sources: tuple[str, ...] = field(repr=False)
    refreshed: bool = False
    reply_message_id: int | str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.sources, tuple):
            raise TypeError("reference snapshot sources must be a tuple")
        if any(
            not isinstance(source, str) or not source.strip()
            for source in self.sources
        ):
            raise ValueError("reference snapshot sources must be non-empty strings")

    @property
    def count(self) -> int:
        """Number of direct reply images captured in this snapshot."""

        return len(self.sources)


@dataclass(frozen=True, slots=True)
class _InspectedImage:
    mime_type: str
    extension: str
    width: int
    height: int


class ReferenceResolver:
    """Resolve the direct images in the first reply component.

    The current message's top-level images and nested replies are deliberately
    ignored. Network and inline sources are decoded in memory; filesystem
    sources are never opened.
    """

    def __init__(self, context: Any, session: aiohttp.ClientSession) -> None:
        self._context = context
        self._session = session

    async def snapshot(self, event: AstrMessageEvent) -> ReferenceSnapshot:
        """Capture direct-reply image sources without downloading image bytes.

        Parsed, portable sources are copied directly. An empty/ambiguous reply
        chain or a parsed image that only exposes a local path is refreshed
        once through OneBot ``get_msg`` while the event is still available.
        """

        reply = self._first_reply(event)
        expected_count: int | None = None
        reply_message_id: int | str | None = None

        if reply is not None:
            raw_message_id = getattr(reply, "id", None)
            if raw_message_id not in (None, ""):
                reply_message_id = self._onebot_scalar(raw_message_id)
            chain = reply.chain if isinstance(reply.chain, (list, tuple)) else []
            direct_images = [
                component
                for component in chain
                if isinstance(component, Image)
            ]
            if direct_images:
                initial_sources = [
                    _ImageSource(self._component_source(image))
                    for image in direct_images
                ]
                if all(item.source is not None for item in initial_sources):
                    return self._snapshot_from_sources(
                        initial_sources,
                        refreshed=False,
                        reply_message_id=reply_message_id,
                    )
                expected_count = len(direct_images)
            refresh_target: Any = reply
        else:
            refresh_target = self._raw_reply_id(event)
            if refresh_target is None:
                return ReferenceSnapshot(())
            reply_message_id = self._onebot_scalar(refresh_target)

        try:
            refreshed = await self._refresh_sources(event, refresh_target)
        except _RefreshProblem as exc:
            logger.warning(
                "CanvasForge could not snapshot a quoted message (%s).",
                type(exc).__name__,
            )
            raise self._invalid_reference_error() from None

        if expected_count is not None and len(refreshed) != expected_count:
            # Never silently drop one member of a parsed multi-image reply.
            raise self._invalid_reference_error()
        return self._snapshot_from_sources(
            refreshed,
            refreshed=True,
            reply_message_id=reply_message_id,
        )

    async def has_direct_images(self, event: AstrMessageEvent) -> bool:
        """Check whether the directly replied-to message contains images.

        This compatibility wrapper now delegates to ``snapshot()`` so callers
        that only need mode selection use the same direct-reply boundary.
        """

        return bool((await self.snapshot(event)).sources)

    async def resolve_snapshot(
        self,
        snapshot: ReferenceSnapshot,
        max_images: int,
        max_total_bytes: int,
        per_image_bytes: int = DEFAULT_PER_IMAGE_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        max_edge: int = DEFAULT_MAX_EDGE,
        event: AstrMessageEvent | None = None,
    ) -> list[ReferenceImage]:
        """Download and validate a previously captured reference snapshot."""

        if not isinstance(snapshot, ReferenceSnapshot):
            raise TypeError("snapshot must be a ReferenceSnapshot")
        self._validate_limits(
            max_images=max_images,
            max_total_bytes=max_total_bytes,
            per_image_bytes=per_image_bytes,
            max_pixels=max_pixels,
            max_edge=max_edge,
        )
        self._check_count(snapshot.count, max_images)
        try:
            return await self._resolve_sources(
                [_ImageSource(source) for source in snapshot.sources],
                max_total_bytes=max_total_bytes,
                per_image_bytes=per_image_bytes,
                max_pixels=max_pixels,
                max_edge=max_edge,
            )
        except _RefreshableReferenceProblem as exc:
            if event is None or snapshot.reply_message_id is None:
                raise CanvasForgeError(
                    ErrorCode.REFERENCE_INVALID,
                    str(exc),
                ) from None
            logger.info(
                "CanvasForge will refresh a snapshotted quoted image batch "
                "after a validation failure (%s).",
                type(exc).__name__,
            )
        except _ReferenceLimitProblem as exc:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_LIMIT,
                str(exc),
            ) from None
        except _ReferenceProblem as exc:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_INVALID,
                str(exc),
            ) from None

        try:
            refreshed = await self._refresh_sources(
                event,
                snapshot.reply_message_id,
            )
        except _RefreshProblem as exc:
            logger.warning(
                "CanvasForge could not refresh a snapshotted quoted image "
                "batch (%s).",
                type(exc).__name__,
            )
            raise self._invalid_reference_error() from None
        if len(refreshed) != snapshot.count:
            raise self._invalid_reference_error()
        return await self._resolve_refreshed(
            refreshed,
            max_images=max_images,
            max_total_bytes=max_total_bytes,
            per_image_bytes=per_image_bytes,
            max_pixels=max_pixels,
            max_edge=max_edge,
        )

    async def resolve(
        self,
        event: AstrMessageEvent,
        max_images: int,
        max_total_bytes: int,
        per_image_bytes: int = DEFAULT_PER_IMAGE_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        max_edge: int = DEFAULT_MAX_EDGE,
    ) -> list[ReferenceImage]:
        """Compatibility wrapper for snapshotting and resolving an event."""

        self._validate_limits(
            max_images=max_images,
            max_total_bytes=max_total_bytes,
            per_image_bytes=per_image_bytes,
            max_pixels=max_pixels,
            max_edge=max_edge,
        )

        snapshot = await self.snapshot(event)
        self._check_count(snapshot.count, max_images)
        try:
            return await self._resolve_sources(
                [_ImageSource(source) for source in snapshot.sources],
                max_total_bytes=max_total_bytes,
                per_image_bytes=per_image_bytes,
                max_pixels=max_pixels,
                max_edge=max_edge,
            )
        except _RefreshableReferenceProblem as exc:
            if snapshot.refreshed or not snapshot.sources:
                raise CanvasForgeError(
                    ErrorCode.REFERENCE_INVALID,
                    str(exc),
                ) from None
            logger.info(
                "CanvasForge will refresh a quoted image batch after a "
                "validation failure (%s).",
                type(exc).__name__,
            )
        except _ReferenceLimitProblem as exc:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_LIMIT,
                str(exc),
            ) from None
        except _ReferenceProblem as exc:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_INVALID,
                str(exc),
            ) from None

        reply = self._first_reply(event)
        refresh_target: Any = (
            reply if reply is not None else self._raw_reply_id(event)
        )
        if refresh_target is None:
            raise self._invalid_reference_error()
        try:
            refreshed = await self._refresh_sources(event, refresh_target)
        except _RefreshProblem as exc:
            logger.warning(
                "CanvasForge could not refresh a quoted image batch (%s).",
                type(exc).__name__,
            )
            raise self._invalid_reference_error() from None
        if len(refreshed) != snapshot.count:
            raise self._invalid_reference_error()

        refreshed_snapshot = self._snapshot_from_sources(
            refreshed,
            refreshed=True,
        )
        return await self.resolve_snapshot(
            refreshed_snapshot,
            max_images=max_images,
            max_total_bytes=max_total_bytes,
            per_image_bytes=per_image_bytes,
            max_pixels=max_pixels,
            max_edge=max_edge,
        )

    @classmethod
    def _snapshot_from_sources(
        cls,
        sources: Sequence[_ImageSource],
        *,
        refreshed: bool,
        reply_message_id: int | str | None = None,
    ) -> ReferenceSnapshot:
        portable_sources: list[str] = []
        for descriptor in sources:
            source = cls._first_allowed_source(descriptor.source)
            if source is None:
                raise cls._invalid_reference_error()
            portable_sources.append(source)
        return ReferenceSnapshot(
            sources=tuple(portable_sources),
            refreshed=refreshed,
            reply_message_id=reply_message_id,
        )

    async def _resolve_refreshed(
        self,
        sources: list[_ImageSource],
        *,
        max_images: int,
        max_total_bytes: int,
        per_image_bytes: int,
        max_pixels: int,
        max_edge: int,
    ) -> list[ReferenceImage]:
        self._check_count(len(sources), max_images)
        try:
            return await self._resolve_sources(
                sources,
                max_total_bytes=max_total_bytes,
                per_image_bytes=per_image_bytes,
                max_pixels=max_pixels,
                max_edge=max_edge,
            )
        except _ReferenceLimitProblem as exc:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_LIMIT,
                str(exc),
            ) from None
        except _ReferenceProblem as exc:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_INVALID,
                str(exc),
            ) from None

    async def _resolve_sources(
        self,
        sources: Sequence[_ImageSource],
        *,
        max_total_bytes: int,
        per_image_bytes: int,
        max_pixels: int,
        max_edge: int,
    ) -> list[ReferenceImage]:
        result: list[ReferenceImage] = []
        consumed_bytes = 0

        for index, descriptor in enumerate(sources, start=1):
            if descriptor.source is None:
                raise _RefreshableReferenceProblem(
                    "引用图片来源不可用，请重新发送图片后再引用。",
                )

            remaining_bytes = max_total_bytes - consumed_bytes
            if remaining_bytes <= 0:
                raise _ReferenceLimitProblem(
                    "引用图片合计大小超过当前限制，请减少图片后重试。",
                )

            data = await self._load_source(
                descriptor.source,
                per_image_bytes=per_image_bytes,
                remaining_total_bytes=remaining_bytes,
            )
            consumed_bytes += len(data)

            inspected = await asyncio.to_thread(
                self._inspect_image,
                data,
                max_pixels,
                max_edge,
            )
            result.append(
                ReferenceImage(
                    data=data,
                    mime_type=inspected.mime_type,
                    filename=f"reference_{index}.{inspected.extension}",
                    width=inspected.width,
                    height=inspected.height,
                ),
            )

        return result

    async def _load_source(
        self,
        source: str,
        *,
        per_image_bytes: int,
        remaining_total_bytes: int,
    ) -> bytes:
        normalized = source.strip()
        prefix = normalized[:16].lower()

        try:
            if prefix.startswith("base64://"):
                data = await asyncio.to_thread(
                    self._decode_base64,
                    normalized[len("base64://") :],
                    per_image_bytes,
                )
            elif prefix.startswith("data:"):
                data = await asyncio.to_thread(
                    self._decode_data_uri,
                    normalized,
                    per_image_bytes,
                )
            else:
                parsed = urlsplit(normalized)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                    raise _RefreshableReferenceProblem(
                        "引用图片来源不可用，请重新发送图片后再引用。",
                    )
                data = await self._download_http(
                    normalized,
                    per_image_bytes=per_image_bytes,
                    remaining_total_bytes=remaining_total_bytes,
                )
        except _ReferenceProblem:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError):
            raise _RefreshableReferenceProblem(
                "引用图片下载失败，请重新发送图片后再引用。",
            ) from None
        except (binascii.Error, UnicodeError, ValueError):
            raise _ReferenceProblem(
                "引用图片数据无效，请重新发送图片后再引用。",
            ) from None

        if not data:
            raise _ReferenceProblem("引用图片内容为空，请重新发送后再引用。")
        if len(data) > per_image_bytes:
            raise _ReferenceProblem(
                "单张引用图片超过 15 MiB 限制，请压缩后重试。",
            )
        if len(data) > remaining_total_bytes:
            raise _ReferenceLimitProblem(
                "引用图片合计大小超过当前限制，请减少图片后重试。",
            )
        return data

    async def _download_http(
        self,
        source: str,
        *,
        per_image_bytes: int,
        remaining_total_bytes: int,
    ) -> bytes:
        body_limit = min(per_image_bytes, remaining_total_bytes)

        async with self._session.get(
            source,
            allow_redirects=True,
            timeout=_DOWNLOAD_TIMEOUT,
        ) as response:
            if response.status < 200 or response.status >= 300:
                raise _RefreshableReferenceProblem(
                    "引用图片下载失败，请重新发送图片后再引用。",
                )

            content_length = response.content_length
            if content_length is not None:
                if content_length > per_image_bytes:
                    raise _ReferenceProblem(
                        "单张引用图片超过 15 MiB 限制，请压缩后重试。",
                    )
                if content_length > remaining_total_bytes:
                    raise _ReferenceLimitProblem(
                        "引用图片合计大小超过当前限制，请减少图片后重试。",
                    )

            data = bytearray()
            async for chunk in response.content.iter_chunked(_HTTP_CHUNK_BYTES):
                data.extend(chunk)
                if len(data) > body_limit:
                    if len(data) > per_image_bytes:
                        raise _ReferenceProblem(
                            "单张引用图片超过 15 MiB 限制，请压缩后重试。",
                        )
                    raise _ReferenceLimitProblem(
                        "引用图片合计大小超过当前限制，请减少图片后重试。",
                    )
            return bytes(data)

    async def _refresh_sources(
        self,
        event: AstrMessageEvent,
        reply: Any,
    ) -> list[_ImageSource]:
        message_id = getattr(reply, "id", reply)
        if message_id in (None, ""):
            raise _RefreshProblem("missing message id")

        client = self._client_for_event(event)
        parameters: dict[str, Any] = {
            "message_id": self._onebot_scalar(message_id),
        }
        self_id = self._safe_event_value(event, "get_self_id")
        if self_id:
            parameters["self_id"] = self._onebot_scalar(self_id)

        try:
            async with asyncio.timeout(_REFRESH_TIMEOUT_SECONDS):
                payload = await _call_action(
                    client,
                    "get_msg",
                    **parameters,
                )
        except (CanvasForgeError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise _RefreshProblem(type(exc).__name__) from None

        message = self._message_from_action_payload(payload)
        if not isinstance(message, list):
            raise _RefreshProblem("invalid get_msg response")
        if not message:
            raise _RefreshProblem("empty get_msg response")

        sources: list[_ImageSource] = []
        for segment in message:
            if not isinstance(segment, Mapping):
                continue
            if str(segment.get("type", "")).lower() != "image":
                continue
            data = segment.get("data")
            if not isinstance(data, Mapping):
                sources.append(_ImageSource(None))
                continue
            sources.append(
                _ImageSource(
                    self._first_allowed_source(
                        data.get("url"),
                        data.get("file"),
                        data.get("path"),
                    ),
                ),
            )
        return sources

    def _client_for_event(self, event: AstrMessageEvent) -> Any:
        platform_name = self._safe_event_value(event, "get_platform_name")
        if platform_name != "aiocqhttp":
            raise CanvasForgeError(ErrorCode.PLATFORM_UNSUPPORTED)

        platform_id = self._safe_event_value(event, "get_platform_id")
        if not platform_id:
            raise _RefreshProblem("missing platform id")

        get_platform_inst = getattr(self._context, "get_platform_inst", None)
        if not callable(get_platform_inst):
            raise _RefreshProblem("platform lookup unavailable")
        try:
            platform = get_platform_inst(str(platform_id))
        except Exception as exc:
            raise _RefreshProblem(type(exc).__name__) from None
        if platform is None:
            raise _RefreshProblem("platform instance unavailable")

        meta = getattr(platform, "meta", None)
        try:
            metadata = meta() if callable(meta) else None
        except Exception as exc:
            raise _RefreshProblem(type(exc).__name__) from None
        if getattr(metadata, "name", None) != "aiocqhttp":
            raise CanvasForgeError(ErrorCode.PLATFORM_UNSUPPORTED)

        get_client = getattr(platform, "get_client", None)
        if not callable(get_client):
            raise _RefreshProblem("platform client unavailable")
        try:
            client = get_client()
        except Exception as exc:
            raise _RefreshProblem(type(exc).__name__) from None
        if client is None:
            raise _RefreshProblem("platform client unavailable")
        return client

    @staticmethod
    def _first_reply(event: AstrMessageEvent) -> Reply | None:
        get_messages = getattr(event, "get_messages", None)
        if callable(get_messages):
            messages = get_messages()
        else:
            message_obj = getattr(event, "message_obj", None)
            messages = getattr(message_obj, "message", [])
        if not isinstance(messages, Sequence):
            return None
        return next(
            (component for component in messages if isinstance(component, Reply)),
            None,
        )

    @staticmethod
    def _raw_reply_id(event: AstrMessageEvent) -> Any | None:
        """Extract only a direct OneBot reply id from the current raw event."""

        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, Mapping):
            raw_chain = raw_message.get("message")
        else:
            raw_chain = getattr(raw_message, "message", None)
        if not isinstance(raw_chain, list):
            return None

        for segment in raw_chain:
            if not isinstance(segment, Mapping):
                continue
            if str(segment.get("type", "")).lower() != "reply":
                continue
            data = segment.get("data")
            if not isinstance(data, Mapping):
                return None
            message_id = data.get("id")
            if message_id in (None, ""):
                message_id = data.get("seq")
            return message_id if message_id not in (None, "") else None
        return None

    @classmethod
    def _component_source(cls, image: Image) -> str | None:
        return cls._first_allowed_source(
            getattr(image, "url", None),
            getattr(image, "file", None),
            getattr(image, "path", None),
        )

    @staticmethod
    def _first_allowed_source(*candidates: Any) -> str | None:
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            value = candidate.strip()
            prefix = value[:16].lower()
            if prefix.startswith(("base64://", "data:")):
                return value
            try:
                parsed = urlsplit(value)
            except ValueError:
                continue
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
                return value
        return None

    @staticmethod
    def _decode_base64(payload: str, byte_limit: int) -> bytes:
        encoded = payload.strip()
        # Four Base64 characters represent at most three decoded bytes. This
        # prevents allocating an unbounded result before applying byte limits.
        max_encoded = ((byte_limit + 2) // 3) * 4 + 4
        if len(encoded) > max_encoded:
            raise _ReferenceProblem("引用图片大小超过当前限制，请压缩后重试。")
        encoded += "=" * (-len(encoded) % 4)
        return base64.b64decode(encoded, validate=True)

    @classmethod
    def _decode_data_uri(cls, source: str, byte_limit: int) -> bytes:
        header, separator, payload = source.partition(",")
        if not separator or not header.lower().startswith("data:"):
            raise _ReferenceProblem("引用图片 data URI 格式无效。")

        metadata = header[5:].lower().split(";")
        if "base64" in metadata[1:]:
            if len(payload) > (((byte_limit + 2) // 3) * 4 + 16):
                raise _ReferenceProblem("引用图片大小超过当前限制，请压缩后重试。")
            try:
                encoded = unquote_to_bytes(payload).decode("ascii")
            except (UnicodeDecodeError, ValueError):
                raise _ReferenceProblem("引用图片 data URI 格式无效。") from None
            return cls._decode_base64(encoded, byte_limit)

        # Percent-encoded data cannot decode to more bytes than its encoded
        # text length, but use a conservative preflight bound nonetheless.
        if len(payload) > byte_limit * 3:
            raise _ReferenceProblem("引用图片大小超过当前限制，请压缩后重试。")
        data = unquote_to_bytes(payload)
        if len(data) > byte_limit:
            raise _ReferenceProblem("引用图片大小超过当前限制，请压缩后重试。")
        return data

    @staticmethod
    def _inspect_image(
        data: bytes,
        max_pixels: int,
        max_edge: int,
    ) -> _InspectedImage:
        try:
            with warnings.catch_warnings():
                # CanvasForge applies its own configurable pixel and edge
                # limits immediately after opening. Ignore Pillow's lower
                # process-global warning threshold so an administrator-set
                # limit up to 100 MP is honored; the hard Pillow error is still
                # caught below.
                warnings.simplefilter("ignore", PillowImage.DecompressionBombWarning)
                with PillowImage.open(BytesIO(data)) as image:
                    image_format = str(image.format or "").upper()
                    if image_format not in _FORMAT_INFO:
                        raise _ReferenceProblem(
                            "引用图仅支持静态 PNG、JPEG 或 WebP。",
                        )

                    width, height = image.size
                    if width <= 0 or height <= 0:
                        raise _ReferenceProblem("引用图片尺寸无效。")
                    if width > max_edge or height > max_edge:
                        raise _ReferenceProblem(
                            "引用图片边长超过当前限制，请缩小后重试。",
                        )
                    if width * height > max_pixels:
                        raise _ReferenceProblem(
                            "引用图片像素数量超过当前限制，请缩小后重试。",
                        )
                    if (
                        bool(getattr(image, "is_animated", False))
                        or int(getattr(image, "n_frames", 1)) != 1
                    ):
                        raise _ReferenceProblem(
                            "引用图必须是静态 PNG、JPEG 或 WebP。",
                        )
                    image.verify()

                # ``verify`` checks file integrity without decoding pixels.
                # Decode once as well so a truncated/corrupt payload never
                # reaches the paid image-edit request.
                with PillowImage.open(BytesIO(data)) as decoded:
                    decoded.load()

            mime_type, extension = _FORMAT_INFO[image_format]
            return _InspectedImage(
                mime_type=mime_type,
                extension=extension,
                width=width,
                height=height,
            )
        except _ReferenceProblem:
            raise
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            PillowImage.DecompressionBombError,
            PillowImage.DecompressionBombWarning,
        ):
            raise _ReferenceProblem(
                "引用图片损坏或格式无效，请重新发送后再引用。",
            ) from None

    @staticmethod
    def _message_from_action_payload(payload: Any) -> Any:
        if not isinstance(payload, Mapping):
            return None
        if isinstance(payload.get("message"), list):
            return payload["message"]
        nested = payload.get("data")
        if isinstance(nested, Mapping):
            return nested.get("message")
        return None

    @staticmethod
    def _check_count(count: int, max_images: int) -> None:
        if count > max_images:
            raise CanvasForgeError(
                ErrorCode.REFERENCE_LIMIT,
                f"最多可引用 {max_images} 张图片，请减少后重试。",
            )

    @staticmethod
    def _validate_limits(
        *,
        max_images: int,
        max_total_bytes: int,
        per_image_bytes: int,
        max_pixels: int,
        max_edge: int,
    ) -> None:
        if (
            max_images < 1
            or max_total_bytes < 1
            or per_image_bytes < 1
            or max_pixels < 1
            or max_edge < 1
        ):
            raise ValueError("reference limits must be positive")

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
    def _onebot_scalar(value: Any) -> int | str:
        text = str(value).strip()
        numeric = text[1:] if text.startswith(("+", "-")) else text
        return int(text) if numeric.isdigit() else text

    @staticmethod
    def _invalid_reference_error() -> CanvasForgeError:
        return CanvasForgeError(
            ErrorCode.REFERENCE_INVALID,
            "引用图片无效或已失效，请重新发送图片并引用后重试。",
        )


async def build_source_metadata(event: AstrMessageEvent) -> dict[str, str]:
    """Build safe cache metadata without making generation depend on lookups."""

    user_id = ReferenceResolver._safe_event_value(event, "get_sender_id")
    user_name = ReferenceResolver._safe_event_value(event, "get_sender_name")
    user_id = user_id or "unknown"
    user_name = user_name or user_id

    group_id = ReferenceResolver._safe_event_value(event, "get_group_id")
    if group_id:
        chat_type = "group"
        session_id = group_id
        session_name = group_id

        if (
            ReferenceResolver._safe_event_value(event, "get_platform_name")
            == "aiocqhttp"
        ):
            client = getattr(event, "bot", None)
            if client is not None:
                parameters: dict[str, Any] = {
                    "group_id": ReferenceResolver._onebot_scalar(group_id),
                }
                self_id = ReferenceResolver._safe_event_value(
                    event,
                    "get_self_id",
                )
                if self_id:
                    parameters["self_id"] = ReferenceResolver._onebot_scalar(
                        self_id,
                    )
                try:
                    async with asyncio.timeout(_GROUP_INFO_TIMEOUT_SECONDS):
                        payload = await _call_action(
                            client,
                            "get_group_info",
                            **parameters,
                        )
                    if isinstance(payload, Mapping):
                        nested = payload.get("data")
                        if (
                            "group_name" not in payload
                            and isinstance(nested, Mapping)
                        ):
                            payload = nested
                        group_name = payload.get("group_name")
                        if isinstance(group_name, str) and group_name.strip():
                            session_name = group_name.strip()
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    logger.debug(
                        "CanvasForge group-name lookup failed (%s).",
                        type(exc).__name__,
                    )
    else:
        chat_type = "private"
        session_id = (
            ReferenceResolver._safe_event_value(event, "get_session_id")
            or user_id
        )
        session_name = user_name or session_id

    return {
        "user_id": user_id,
        "user_name": user_name,
        "chat_type": chat_type,
        "session_id": session_id,
        "session_name": session_name,
    }


async def _call_action(client: Any, action: str, **parameters: Any) -> Any:
    """Call the aiocqhttp client without importing its private implementation."""

    call_action = getattr(client, "call_action", None)
    if not callable(call_action):
        api = getattr(client, "api", None)
        call_action = getattr(api, "call_action", None)
    if not callable(call_action):
        raise RuntimeError("aiocqhttp call_action is unavailable")

    if inspect.iscoroutinefunction(call_action):
        return await call_action(action=action, **parameters)

    result = await asyncio.to_thread(
        call_action,
        action=action,
        **parameters,
    )
    if inspect.isawaitable(result):
        return await result
    return result
