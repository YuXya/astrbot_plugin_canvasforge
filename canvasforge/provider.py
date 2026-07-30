"""Sub2API implementation of the CanvasForge image-provider contract."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import math
import warnings
from dataclasses import dataclass, field, replace
from io import BytesIO
from typing import Any, Mapping, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit

import aiohttp
from PIL import Image as PillowImage

from .contracts import (
    CanvasForgeError,
    ErrorCode,
    GeneratedImage,
    HttpKeyProviderConfig,
    ImageProvider,
    ImageRequestOptions,
    ReferenceImage,
)


_MIB = 1024 * 1024
_SUPPORTED_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
_SUPPORTED_REFERENCE_MIMES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_FORMAT_MIMES = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpeg"),
    "WEBP": ("image/webp", "webp"),
}
_FULL_ENDPOINT_SUFFIXES = ("/images/generations", "/images/edits")


@dataclass(frozen=True, slots=True)
class Sub2APIConnection:
    """An immutable connection snapshot so hot updates cannot mix fields."""

    base_url: str = ""
    api_key: str = field(default="", repr=False)


def _is_http_allowed_host(hostname: str) -> bool:
    hostname = hostname.rstrip(".").lower()
    if not hostname:
        return False
    if (
        hostname == "localhost"
        or "." not in hostname
        or hostname.endswith(".local")
        or hostname.endswith(".lan")
        or hostname in {"host.docker.internal", "gateway.docker.internal"}
    ):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    )


def normalize_sub2api_base_url(raw_url: str) -> str:
    """Normalize a site root, ``/v1`` base, or full Images endpoint.

    The returned URL always ends in ``/v1`` and never contains credentials,
    query parameters, or a fragment.
    """

    candidate = raw_url.strip()
    if not candidate:
        raise CanvasForgeError(ErrorCode.NOT_CONFIGURED)
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)

    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID) from None

    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or port == 0
    ):
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
    if parsed.username is not None or parsed.password is not None:
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
    if parsed.query or parsed.fragment:
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
    if scheme == "http" and not _is_http_allowed_host(hostname):
        raise CanvasForgeError(
            ErrorCode.CONFIG_INVALID,
            "公网图片服务必须使用 HTTPS；HTTP 仅允许本机或内网地址。",
        )

    path = parsed.path.rstrip("/")
    lowered = path.lower()
    for suffix in _FULL_ENDPOINT_SUFFIXES:
        if lowered.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            lowered = path.lower()
            break

    if lowered.endswith("/images"):
        path = path[: -len("/images")].rstrip("/")
        lowered = path.lower()
    if not lowered.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"

    normalized = SplitResult(
        scheme=scheme,
        netloc=parsed.netloc,
        path=path,
        query="",
        fragment="",
    )
    return urlunsplit(normalized)


def _endpoint(base_url: str, operation: str) -> str:
    return f"{base_url}/images/{operation}"


def _validate_prompt(prompt: str) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise CanvasForgeError(ErrorCode.MISSING_PROMPT)


def _normalize_options(options: ImageRequestOptions) -> ImageRequestOptions:
    model = options.model.strip() if isinstance(options.model, str) else ""
    size = options.size.strip() if isinstance(options.size, str) else ""
    quality = options.quality.strip() if isinstance(options.quality, str) else ""
    output_format = (
        options.output_format.strip().lower()
        if isinstance(options.output_format, str)
        else ""
    )
    if output_format == "jpg":
        output_format = "jpeg"
    if not model or not size or not quality:
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
    if output_format not in _SUPPORTED_OUTPUT_FORMATS:
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
    if (
        isinstance(options.output_compression, bool)
        or not isinstance(options.output_compression, int)
        or not 0 <= options.output_compression <= 100
    ):
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
    if (
        isinstance(options.timeout_seconds, bool)
        or not isinstance(options.timeout_seconds, (int, float))
        or not math.isfinite(options.timeout_seconds)
        or options.timeout_seconds <= 0
    ):
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
    if (
        isinstance(options.max_output_bytes, bool)
        or not isinstance(options.max_output_bytes, int)
        or options.max_output_bytes <= 0
    ):
        raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
    return replace(
        options,
        model=model,
        size=size,
        quality=quality,
        output_format=output_format,
        timeout_seconds=float(options.timeout_seconds),
    )


def _request_fields(options: ImageRequestOptions) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "model": options.model,
        "n": 1,
        "stream": False,
        "response_format": "b64_json",
        "size": options.size,
        "quality": options.quality,
        "output_format": options.output_format,
    }
    if options.output_format in {"jpeg", "webp"}:
        fields["output_compression"] = options.output_compression
    return fields


def _multipart_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _reference_extension(mime_type: str) -> str:
    normalized = mime_type.split(";", 1)[0].strip().lower()
    try:
        return _SUPPORTED_REFERENCE_MIMES[normalized]
    except KeyError:
        raise CanvasForgeError(ErrorCode.REFERENCE_INVALID) from None


def _error_fingerprint(payload: Any) -> str:
    """Extract classification hints without ever exposing them to callers."""

    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    values: list[Any] = []
    if isinstance(error, Mapping):
        values.extend(
            (
                error.get("code"),
                error.get("type"),
                error.get("status"),
                error.get("message"),
            )
        )
    elif error is not None:
        values.append(error)
    if "code" in payload or "message" in payload:
        values.extend((payload.get("code"), payload.get("message")))
    return " ".join(str(value).lower() for value in values if value is not None)


def _error_codes(payload: Any) -> set[str]:
    """Extract structured status/code values solely for safe classification."""

    if not isinstance(payload, Mapping):
        return set()
    values: list[Any] = []
    if "code" in payload:
        values.append(payload.get("code"))
    error = payload.get("error")
    if isinstance(error, Mapping):
        values.extend((error.get("code"), error.get("status")))
    return {
        str(value).strip().lower()
        for value in values
        if value is not None and str(value).strip()
    }


def _payload_has_error(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if payload.get("error") not in (None, False, ""):
        return True
    if "code" not in payload or "message" not in payload:
        return False
    code = str(payload.get("code", "")).strip().lower()
    return code not in {"0", "200", "ok", "success"}


def _classified_upstream_error(status: int, payload: Any = None) -> CanvasForgeError:
    fingerprint = _error_fingerprint(payload)
    codes = _error_codes(payload)

    if status == 429 or "429" in codes or any(
        hint in fingerprint
        for hint in (
            "rate_limit",
            "rate limit",
            "too many",
            "insufficient_quota",
            "quota exceeded",
            "quota_exhausted",
            "quota exhausted",
            "quota depleted",
        )
    ):
        return CanvasForgeError(ErrorCode.RATE_LIMIT)
    if (
        status in {408, 504}
        or codes.intersection({"408", "504"})
        or "timeout" in fingerprint
        or "timed out" in fingerprint
    ):
        return CanvasForgeError(ErrorCode.TIMEOUT)
    if (
        status >= 500
        or any(code.isdigit() and 500 <= int(code) <= 599 for code in codes)
        or any(
            hint in fingerprint
            for hint in (
                "api_key_auth_overloaded",
                "temporarily unavailable",
                "service unavailable",
                "server overloaded",
            )
        )
    ):
        return CanvasForgeError(ErrorCode.UPSTREAM_UNAVAILABLE)
    if any(
        hint in fingerprint
        for hint in (
            "insufficient_balance",
            "insufficient balance",
            "balance insufficient",
            "余额不足",
        )
    ):
        return CanvasForgeError(ErrorCode.UPSTREAM_REJECTED)
    if status in {401, 403} or codes.intersection({"401", "403"}) or any(
        hint in fingerprint
        for hint in (
            "authentication",
            "authorization",
            "api_key_required",
            "api_key_invalid",
            "invalid_api_key",
            "incorrect_api_key",
            "missing_api_key",
            "invalid_token",
            "api key required",
            "api key invalid",
            "api key is invalid",
            "missing api key",
            "unauthorized",
            "forbidden",
            "invalid key",
            "incorrect api key",
        )
    ):
        return CanvasForgeError(ErrorCode.AUTH)
    return CanvasForgeError(ErrorCode.UPSTREAM_REJECTED)


async def _read_limited_body(
    response: aiohttp.ClientResponse,
    max_output_bytes: int,
) -> bytes:
    # Base64 expands by 4/3. The extra 2 MiB permits response metadata while
    # still bounding a malicious or accidentally huge JSON response.
    limit = max(2 * _MIB, math.ceil(max_output_bytes * 4 / 3) + 2 * _MIB)
    content_length = response.content_length
    if content_length is not None and content_length > limit:
        if response.status >= 400:
            raise _classified_upstream_error(response.status)
        if response.status < 200 or response.status >= 300:
            raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
        raise CanvasForgeError(ErrorCode.OUTPUT_TOO_LARGE)

    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > limit:
            if response.status >= 400:
                raise _classified_upstream_error(response.status)
            if response.status < 200 or response.status >= 300:
                raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
            raise CanvasForgeError(ErrorCode.OUTPUT_TOO_LARGE)
    return bytes(body)


def _parse_json(body: bytes, status: int) -> Any:
    try:
        text = body.decode("utf-8-sig").lstrip()
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        if status >= 400:
            raise _classified_upstream_error(status) from None
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE) from None


def _decode_base64_image(encoded: str, max_output_bytes: int) -> bytes:
    value = encoded.strip()
    if value.lower().startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or ";base64" not in header.lower():
            raise CanvasForgeError(ErrorCode.BAD_RESPONSE)

    compact = "".join(value.split())
    if not compact:
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
    padding = len(compact) - len(compact.rstrip("="))
    estimated_size = max(0, (len(compact) * 3) // 4 - padding)
    if estimated_size > max_output_bytes:
        raise CanvasForgeError(ErrorCode.OUTPUT_TOO_LARGE)
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE) from None
    if not decoded:
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
    if len(decoded) > max_output_bytes:
        raise CanvasForgeError(ErrorCode.OUTPUT_TOO_LARGE)
    return decoded


def _validate_generated_image(data: bytes) -> GeneratedImage:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
            with PillowImage.open(BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                if image_format not in _FORMAT_MIMES:
                    raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
                if getattr(image, "is_animated", False) or getattr(
                    image, "n_frames", 1
                ) != 1:
                    raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
                image.verify()
            # ``verify`` checks the container but does not decode pixel data.
            # Reopen and load fully so corrupt/truncated paid results cannot
            # be cached or handed to QQ.
            with PillowImage.open(BytesIO(data)) as image:
                image.load()
    except CanvasForgeError:
        raise
    except (
        OSError,
        SyntaxError,
        ValueError,
        PillowImage.DecompressionBombError,
        PillowImage.DecompressionBombWarning,
    ):
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE) from None

    mime_type, normalized_format = _FORMAT_MIMES[image_format]
    return GeneratedImage(
        data=data,
        mime_type=mime_type,
        format=normalized_format,
        width=width,
        height=height,
    )


def _extract_generated_image(
    payload: Any,
    max_output_bytes: int,
) -> GeneratedImage:
    if not isinstance(payload, Mapping):
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
    data_items = payload.get("data")
    if not isinstance(data_items, list) or len(data_items) != 1:
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
    item = data_items[0]
    if not isinstance(item, Mapping):
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
    encoded = item.get("b64_json")
    if not isinstance(encoded, str):
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE)
    decoded = _decode_base64_image(encoded, max_output_bytes)
    return _validate_generated_image(decoded)


def _process_response_body(
    body: bytes,
    status: int,
    max_output_bytes: int,
) -> GeneratedImage:
    """Parse, classify, decode, and validate one complete upstream response.

    This function is deliberately synchronous so callers can run all
    CPU-intensive response processing in a single worker-thread handoff.
    """

    if status < 200 or status >= 300:
        if status >= 400:
            payload = _parse_json(body, status)
            raise _classified_upstream_error(status, payload)
        raise CanvasForgeError(ErrorCode.BAD_RESPONSE)

    payload = _parse_json(body, status)
    if _payload_has_error(payload):
        raise _classified_upstream_error(status, payload)
    return _extract_generated_image(payload, max_output_bytes)


class Sub2APIImagesProvider:
    """Non-streaming OpenAI Images-compatible provider backed by Sub2API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        normalized_url = normalize_sub2api_base_url(base_url)
        normalized_key = api_key.strip() if isinstance(api_key, str) else ""
        if not normalized_key:
            raise CanvasForgeError(ErrorCode.NOT_CONFIGURED)
        if "\r" in normalized_key or "\n" in normalized_key:
            raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
        self._connection = Sub2APIConnection(
            base_url=normalized_url,
            api_key=normalized_key,
        )
        self._session = session
        self._owns_session = session is None
        self._session_lock = asyncio.Lock()
        self._closed = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise CanvasForgeError(ErrorCode.INTERNAL)
        if self._session is not None and not self._session.closed:
            return self._session
        if not self._owns_session:
            raise CanvasForgeError(ErrorCode.UPSTREAM_UNAVAILABLE)
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    def _connection_snapshot(self) -> tuple[str, str]:
        connection = self._connection
        return connection.base_url, connection.api_key

    async def _post(
        self,
        operation: str,
        options: ImageRequestOptions,
        *,
        json_body: Mapping[str, Any] | None = None,
        form: aiohttp.FormData | None = None,
    ) -> GeneratedImage:
        base_url, api_key = self._connection_snapshot()
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=options.timeout_seconds)

        try:
            async with session.post(
                _endpoint(base_url, operation),
                headers=headers,
                json=json_body,
                data=form,
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                body = await _read_limited_body(response, options.max_output_bytes)
                return await asyncio.to_thread(
                    _process_response_body,
                    body,
                    response.status,
                    options.max_output_bytes,
                )
        except CanvasForgeError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise CanvasForgeError(ErrorCode.TIMEOUT) from None
        except aiohttp.ClientError:
            raise CanvasForgeError(ErrorCode.UPSTREAM_UNAVAILABLE) from None

    async def generate(
        self,
        prompt: str,
        options: ImageRequestOptions,
    ) -> GeneratedImage:
        _validate_prompt(prompt)
        normalized = _normalize_options(options)
        payload = {"prompt": prompt, **_request_fields(normalized)}
        return await self._post("generations", normalized, json_body=payload)

    async def edit(
        self,
        prompt: str,
        references: Sequence[ReferenceImage],
        options: ImageRequestOptions,
    ) -> GeneratedImage:
        _validate_prompt(prompt)
        normalized = _normalize_options(options)
        if not references:
            raise CanvasForgeError(ErrorCode.REFERENCE_INVALID)

        form = aiohttp.FormData()
        image_field = "image" if len(references) == 1 else "image[]"
        for index, reference in enumerate(references, start=1):
            if not isinstance(reference.data, bytes) or not reference.data:
                raise CanvasForgeError(ErrorCode.REFERENCE_INVALID)
            extension = _reference_extension(reference.mime_type)
            normalized_mime = reference.mime_type.split(";", 1)[0].strip().lower()
            form.add_field(
                image_field,
                reference.data,
                filename=f"reference_{index}.{extension}",
                content_type=normalized_mime,
            )

        form.add_field("prompt", prompt)
        for name, value in _request_fields(normalized).items():
            form.add_field(name, _multipart_scalar(value))
        return await self._post("edits", normalized, form=form)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()


class Sub2APIImagesProviderFactory:
    """Bind immutable Sub2API providers to a shared long-lived session."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    def create(self, config: HttpKeyProviderConfig) -> ImageProvider:
        if not isinstance(config, HttpKeyProviderConfig):
            raise CanvasForgeError(ErrorCode.CONFIG_INVALID)
        return Sub2APIImagesProvider(
            config.base_url,
            config.api_key,
            session=self._session,
        )
