"""Prepare a bounded image copy for QQ delivery without changing the cache."""

from __future__ import annotations

from io import BytesIO


QQ_DELIVERY_MAX_BYTES = 6 * 1024 * 1024
_INITIAL_WEBP_QUALITIES = (90, 85, 80, 75)
_RESIZED_WEBP_QUALITY = 80
_RESIZE_FACTOR = 0.85
_MIN_MAX_EDGE = 768


def prepare_qq_delivery_bytes(
    image_bytes: bytes,
    *,
    max_bytes: int = QQ_DELIVERY_MAX_BYTES,
) -> bytes:
    """Return a QQ-friendly copy, preserving the original when already small.

    This is intentionally synchronous and CPU-bound. Callers must run it in a
    worker thread so Pillow encoding cannot pause AstrBot's event loop.
    """

    if len(image_bytes) <= max_bytes:
        return image_bytes
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    from PIL import Image

    with Image.open(BytesIO(image_bytes)) as source:
        source.load()
        has_alpha = "A" in source.getbands() or "transparency" in source.info
        prepared = source.convert("RGBA" if has_alpha else "RGB")

    smallest = image_bytes
    for quality in _INITIAL_WEBP_QUALITIES:
        encoded = _encode_webp(prepared, quality)
        if len(encoded) < len(smallest):
            smallest = encoded
        if len(encoded) <= max_bytes:
            return encoded

    current = prepared
    while max(current.size) > _MIN_MAX_EDGE:
        current_max_edge = max(current.size)
        target_max_edge = max(
            _MIN_MAX_EDGE,
            int(current_max_edge * _RESIZE_FACTOR),
        )
        scale = target_max_edge / current_max_edge
        target_size = (
            max(1, round(current.width * scale)),
            max(1, round(current.height * scale)),
        )
        if target_size == current.size:
            break

        current = current.resize(
            target_size,
            Image.Resampling.LANCZOS,
            reducing_gap=2.0,
        )
        encoded = _encode_webp(current, _RESIZED_WEBP_QUALITY)
        if len(encoded) < len(smallest):
            smallest = encoded
        if len(encoded) <= max_bytes:
            return encoded

    return smallest


def _encode_webp(image: object, quality: int) -> bytes:
    output = BytesIO()
    image.save(
        output,
        format="WEBP",
        quality=quality,
        method=4,
    )
    return output.getvalue()
