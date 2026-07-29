"""Provider-neutral contracts shared by CanvasForge subsystems."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence, runtime_checkable


class ErrorCode(str, Enum):
    """Stable error categories exposed to the plugin orchestration layer."""

    REFERENCE_LIMIT = "reference_limit"
    REFERENCE_INVALID = "reference_invalid"
    AVATAR_DISABLED = "avatar_disabled"
    AVATAR_TARGET_INVALID = "avatar_target_invalid"
    AVATAR_UNAVAILABLE = "avatar_unavailable"
    PLATFORM_UNSUPPORTED = "platform_unsupported"
    NOT_CONFIGURED = "not_configured"
    CONFIG_INVALID = "config_invalid"
    MISSING_PROMPT = "missing_prompt"
    MODE_MISMATCH = "mode_mismatch"
    BUSY = "busy"
    COOLDOWN = "cooldown"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    UPSTREAM_REJECTED = "upstream_rejected"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    TIMEOUT = "timeout"
    BAD_RESPONSE = "bad_response"
    OUTPUT_TOO_LARGE = "output_too_large"
    SEND_FAILED = "send_failed"
    INTERNAL = "internal"


_DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.REFERENCE_LIMIT: "引用图片数量超过当前限制，请减少后重试。",
    ErrorCode.REFERENCE_INVALID: "引用图片无效或已失效，请重新发送图片并引用后重试。",
    ErrorCode.AVATAR_DISABLED: "人物头像引用功能当前已由管理员关闭。",
    ErrorCode.AVATAR_TARGET_INVALID: "人物头像选择器无效、重复或不适用于当前会话。",
    ErrorCode.AVATAR_UNAVAILABLE: "人物头像暂时无法获取或图片无效，请稍后重试。",
    ErrorCode.PLATFORM_UNSUPPORTED: "CanvasForge 当前仅支持 NapCat（aiocqhttp）平台。",
    ErrorCode.NOT_CONFIGURED: "CanvasForge 尚未配置图片服务站点或 Key。",
    ErrorCode.CONFIG_INVALID: "CanvasForge 的图片服务配置无效，请联系管理员检查。",
    ErrorCode.MISSING_PROMPT: "请提供要生成或编辑的图片描述。",
    ErrorCode.MODE_MISMATCH: "当前请求与所选生图工具不匹配，请选择正确的工具后重试。",
    ErrorCode.BUSY: "CanvasForge 正在处理另一张图片，请稍后再试。",
    ErrorCode.COOLDOWN: "生成冷却尚未结束，请稍后再试。",
    ErrorCode.AUTH: "图片服务鉴权失败，请管理员检查站点和 Key。",
    ErrorCode.RATE_LIMIT: "图片服务当前限流，请稍后再试。",
    ErrorCode.UPSTREAM_REJECTED: "图片请求被上游拒绝，请调整描述或生成设置后重试。",
    ErrorCode.UPSTREAM_UNAVAILABLE: "图片服务暂时不可用，请稍后再试。",
    ErrorCode.TIMEOUT: "图片生成超时，请稍后再试。",
    ErrorCode.BAD_RESPONSE: "图片服务返回了无法识别的结果，请稍后再试。",
    ErrorCode.OUTPUT_TOO_LARGE: "生成图片超过允许的大小，请调整生成设置后重试。",
    ErrorCode.SEND_FAILED: "图片已生成，但发送到 QQ 失败，请稍后再试。",
    ErrorCode.INTERNAL: "CanvasForge 处理请求时发生内部错误，请稍后再试。",
}


class CanvasForgeError(Exception):
    """An error safe to surface to a QQ user or the current chat model.

    ``message`` must remain a user-facing, non-sensitive summary. Provider
    implementations intentionally never place an upstream response, URL, key,
    or signed media URL in this exception.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        retry_after: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or _DEFAULT_MESSAGES[code]
        self.retry_after = retry_after
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    """An already validated image supplied to an image-edit request."""

    data: bytes
    mime_type: str
    filename: str = "reference"
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """A single decoded and validated provider result."""

    data: bytes
    mime_type: str
    format: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ImageRequestOptions:
    """Provider-neutral image generation options for one output image."""

    model: str = "gpt-image-2"
    size: str = "1024x1024"
    quality: str = "medium"
    output_format: str = "png"
    output_compression: int = 90
    timeout_seconds: float = 300.0
    max_output_bytes: int = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpKeyProviderConfig:
    """Provider connection values supported by CanvasForge v0.1."""

    base_url: str
    api_key: str


@runtime_checkable
class ImageProvider(Protocol):
    """Interface implemented by current and future image providers."""

    async def generate(
        self,
        prompt: str,
        options: ImageRequestOptions,
    ) -> GeneratedImage:
        """Generate one image from a text prompt."""

    async def edit(
        self,
        prompt: str,
        references: Sequence[ReferenceImage],
        options: ImageRequestOptions,
    ) -> GeneratedImage:
        """Generate one image using one or more reference images."""


@runtime_checkable
class ImageProviderFactory(Protocol):
    """Create a request-bound provider from the generic HTTP + Key config."""

    def create(self, config: HttpKeyProviderConfig) -> ImageProvider:
        """Bind one immutable provider instance to a request snapshot."""
