"""Fault-tolerant on-disk cache for generated CanvasForge images."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
import warnings
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Mapping, TypeVar


_CACHE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SUPPORTED_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}
_METADATA_STRING_LIMIT = 512
_T = TypeVar("_T")


async def _shield_to_completion(awaitable: Awaitable[_T]) -> _T:
    """Finish an internal transaction before propagating caller cancellation.

    ``asyncio.to_thread`` cannot stop its worker when the awaiting coroutine is
    cancelled. Keeping the internal task shielded prevents the caller from
    releasing ``CacheStore._lock`` while a worker still mutates cache state.
    Repeated cancellation requests are consumed until the transaction finishes;
    its result or exception is then retrieved before cancellation is re-raised.
    """

    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except Exception:
            # The task has completed with an exception. Retrieve it below so it
            # is never reported as an unhandled task exception.
            break

    try:
        result = task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation from None
        raise
    if cancellation is not None:
        raise cancellation
    return result


class CacheError(RuntimeError):
    """Raised when a cache operation cannot be completed safely."""


class CacheNotFoundError(CacheError):
    """Raised when a requested cache item does not exist."""


class CacheStore:
    """Store generated images, thumbnails, and a small atomic metadata index."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.originals_dir = self.root / "originals"
        self.thumbnails_dir = self.root / "thumbnails"
        self.orphaned_dir = self.root / "orphaned"
        self.index_path = self.root / "index.json"
        self._lock = asyncio.Lock()
        self._items: list[dict[str, Any]] = []
        self._limit = 3
        self._initialized = False

    @property
    def limit(self) -> int:
        return self._limit

    async def initialize(self, limit: int = 3) -> None:
        """Create cache directories, recover the index, and enforce ``limit``."""

        normalized_limit = self._validate_limit(limit)
        async with self._lock:
            await _shield_to_completion(
                self._initialize_locked(normalized_limit),
            )

    async def store(
        self,
        image_bytes: bytes,
        metadata: Mapping[str, Any] | None = None,
        *,
        limit: int | None = None,
    ) -> dict[str, Any] | None:
        """Atomically cache an original image and its 512px WebP thumbnail.

        ``None`` is returned when caching is disabled (limit is zero).
        """

        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise CacheError("待缓存的图片数据为空")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise CacheError("缓存元数据格式无效")

        async with self._lock:
            return await _shield_to_completion(
                self._store_locked(image_bytes, metadata, limit),
            )

    async def list(self) -> list[dict[str, Any]]:
        """Return public cache metadata, newest first."""

        async with self._lock:
            self._require_initialized()
            await _shield_to_completion(
                asyncio.to_thread(self._cleanup_pending_deletes_sync),
            )
            return [self._public_entry(item) for item in reversed(self._items)]

    async def get(self, cache_id: str) -> dict[str, Any] | None:
        """Return public metadata for one item, or ``None`` when absent."""

        self._validate_cache_id(cache_id)
        async with self._lock:
            self._require_initialized()
            item = self._find_item(cache_id)
            return self._public_entry(item) if item is not None else None

    async def delete(self, cache_id: str) -> bool:
        """Delete one item and both image files."""

        self._validate_cache_id(cache_id)
        async with self._lock:
            return await _shield_to_completion(
                self._delete_locked(cache_id),
            )

    async def clear(self) -> int:
        """Remove every indexed cache item and return the removed count."""

        async with self._lock:
            return await _shield_to_completion(self._clear_locked())

    async def set_limit(self, limit: int) -> int:
        """Apply a 0-20 item limit and return the number immediately evicted.

        A zero limit disables future writes but deliberately preserves existing
        images. Any positive limit is enforced immediately.
        """

        normalized_limit = self._validate_limit(limit)
        async with self._lock:
            return await _shield_to_completion(
                self._set_limit_locked(normalized_limit),
            )

    async def get_original_path(self, cache_id: str) -> Path:
        """Return a validated path to an original cache file."""

        self._validate_cache_id(cache_id)
        async with self._lock:
            self._require_initialized()
            item = self._find_item(cache_id)
            if item is None:
                raise CacheNotFoundError("缓存图片不存在")
            path = self._item_path(item, thumbnail=False)
            if not path.is_file():
                raise CacheNotFoundError("缓存原图不存在")
            return path

    async def get_thumbnail_path(self, cache_id: str) -> Path:
        """Return a validated path to a thumbnail cache file."""

        self._validate_cache_id(cache_id)
        async with self._lock:
            self._require_initialized()
            item = self._find_item(cache_id)
            if item is None:
                raise CacheNotFoundError("缓存图片不存在")
            path = self._item_path(item, thumbnail=True)
            if not path.is_file():
                raise CacheNotFoundError("缓存缩略图不存在")
            return path

    async def read_original(self, cache_id: str) -> bytes:
        path = await self.get_original_path(cache_id)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise CacheNotFoundError("缓存原图不存在") from exc
        except OSError as exc:
            raise CacheError("无法读取缓存原图") from exc

    async def read_thumbnail(self, cache_id: str) -> bytes:
        path = await self.get_thumbnail_path(cache_id)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise CacheNotFoundError("缓存缩略图不存在") from exc
        except OSError as exc:
            raise CacheError("无法读取缓存缩略图") from exc

    async def render_preview(
        self,
        cache_id: str,
        *,
        max_edge: int = 2048,
    ) -> bytes:
        """Render an in-memory WebP preview without reading the original whole.

        The cache lock keeps deletion/eviction from moving the original while
        Pillow is decoding it. No preview file is persisted on disk.
        """

        self._validate_cache_id(cache_id)
        if isinstance(max_edge, bool) or not isinstance(max_edge, int) or max_edge <= 0:
            raise CacheError("预览图尺寸无效")
        async with self._lock:
            return await _shield_to_completion(
                self._render_preview_locked(cache_id, max_edge),
            )

    async def _initialize_locked(self, normalized_limit: int) -> None:
        evicted_items: list[dict[str, Any]] = []
        staged_files: list[tuple[Path, Path]] = []
        try:
            await asyncio.to_thread(self._initialize_sync)
            self._limit = normalized_limit
            changed = await asyncio.to_thread(self._reconcile_sync)
            evicted_items = await asyncio.to_thread(self._pop_excess_sync)
            staged_files = await asyncio.to_thread(
                self._stage_item_files_sync,
                evicted_items,
            )
            if changed or evicted_items:
                await asyncio.to_thread(self._write_index_sync)
            await asyncio.to_thread(
                self._discard_staged_files_sync,
                staged_files,
            )
            self._initialized = True
        except CacheError:
            await asyncio.to_thread(
                self._restore_staged_files_sync,
                staged_files,
            )
            if evicted_items:
                self._items.extend(evicted_items)
                self._items.sort(key=self._sort_key)
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self._restore_staged_files_sync,
                staged_files,
            )
            if evicted_items:
                self._items.extend(evicted_items)
                self._items.sort(key=self._sort_key)
            raise CacheError("无法初始化图片缓存") from exc

    async def _store_locked(
        self,
        image_bytes: bytes,
        metadata: Mapping[str, Any] | None,
        limit: int | None,
    ) -> dict[str, Any] | None:
        self._require_initialized()
        await asyncio.to_thread(self._cleanup_pending_deletes_sync)
        if limit is not None:
            self._limit = self._validate_limit(limit)
        if self._limit == 0:
            return None

        try:
            prepared = await asyncio.to_thread(
                self._prepare_image_sync,
                image_bytes,
            )
            entry = await asyncio.to_thread(
                self._store_sync,
                image_bytes,
                prepared,
                dict(metadata or {}),
            )
            return self._public_entry(entry)
        except CacheError:
            raise
        except Exception as exc:
            raise CacheError("无法写入图片缓存") from exc

    async def _delete_locked(self, cache_id: str) -> bool:
        self._require_initialized()
        item = self._find_item(cache_id)
        if item is None:
            return False
        staged_files: list[tuple[Path, Path]] = []
        try:
            staged_files = await asyncio.to_thread(
                self._stage_item_files_sync,
                [item],
            )
            self._items.remove(item)
            await asyncio.to_thread(self._write_index_sync)
            await asyncio.to_thread(
                self._discard_staged_files_sync,
                staged_files,
            )
            return True
        except Exception as exc:
            await asyncio.to_thread(
                self._restore_staged_files_sync,
                staged_files,
            )
            if item not in self._items:
                self._items.append(item)
                self._items.sort(key=self._sort_key)
            raise CacheError("无法删除缓存图片") from exc

    async def _clear_locked(self) -> int:
        self._require_initialized()
        old_items = list(self._items)
        staged_files: list[tuple[Path, Path]] = []
        try:
            staged_files = await asyncio.to_thread(
                self._stage_item_files_sync,
                old_items,
            )
            self._items.clear()
            await asyncio.to_thread(self._write_index_sync)
            await asyncio.to_thread(
                self._discard_staged_files_sync,
                staged_files,
            )
            return len(old_items)
        except Exception as exc:
            await asyncio.to_thread(
                self._restore_staged_files_sync,
                staged_files,
            )
            self._items = old_items
            raise CacheError("无法清空图片缓存") from exc

    async def _set_limit_locked(self, normalized_limit: int) -> int:
        self._require_initialized()
        old_limit = self._limit
        self._limit = normalized_limit
        if normalized_limit == 0:
            return 0
        evicted_items: list[dict[str, Any]] = []
        staged_files: list[tuple[Path, Path]] = []
        try:
            evicted_items = await asyncio.to_thread(self._pop_excess_sync)
            if evicted_items:
                staged_files = await asyncio.to_thread(
                    self._stage_item_files_sync,
                    evicted_items,
                )
                await asyncio.to_thread(self._write_index_sync)
                await asyncio.to_thread(
                    self._discard_staged_files_sync,
                    staged_files,
                )
            return len(evicted_items)
        except Exception as exc:
            await asyncio.to_thread(
                self._restore_staged_files_sync,
                staged_files,
            )
            self._limit = old_limit
            if evicted_items:
                self._items.extend(evicted_items)
                self._items.sort(key=self._sort_key)
            raise CacheError("无法调整缓存数量") from exc

    async def _render_preview_locked(
        self,
        cache_id: str,
        max_edge: int,
    ) -> bytes:
        self._require_initialized()
        item = self._find_item(cache_id)
        if item is None:
            raise CacheNotFoundError("缓存图片不存在")
        path = self._item_path(item, thumbnail=False)
        if not path.is_file():
            raise CacheNotFoundError("缓存原图不存在")
        try:
            return await asyncio.to_thread(
                self._render_preview_sync,
                path,
                max_edge,
            )
        except CacheError:
            raise
        except FileNotFoundError as exc:
            raise CacheNotFoundError("缓存原图不存在") from exc
        except OSError as exc:
            raise CacheError("无法生成缓存预览图") from exc

    def _initialize_sync(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        self.orphaned_dir.mkdir(parents=True, exist_ok=True)
        self._assert_within_root(self.originals_dir)
        self._assert_within_root(self.thumbnails_dir)
        self._assert_within_root(self.orphaned_dir)
        self._cleanup_pending_deletes_sync()

        if self.index_path.is_symlink():
            self._quarantine_if_exists_sync(self.index_path)
        if not self.index_path.exists():
            self._items = []
            self._write_index_sync()
            return

        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            raw_items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(raw_items, list):
                raise ValueError("index items is not a list")
            self._items = [
                normalized
                for raw in raw_items
                if (normalized := self._normalize_index_entry(raw)) is not None
            ]
            self._items.sort(key=self._sort_key)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self._backup_corrupt_index_sync()
            self._items = []

    def _reconcile_sync(self) -> bool:
        changed = False
        retained: list[dict[str, Any]] = []
        known_ids: set[str] = set()

        for item in self._items:
            cache_id = item["id"]
            try:
                original = self._item_path(item, thumbnail=False)
                thumbnail = self._item_path(item, thumbnail=True)
            except CacheError:
                changed = True
                continue
            if original.is_file() and thumbnail.is_file():
                retained.append(item)
                known_ids.add(cache_id)
            else:
                changed = True
                self._quarantine_if_exists_sync(original)
                self._quarantine_if_exists_sync(thumbnail)

        self._items = retained
        original_candidates: dict[str, Path] = {}
        thumbnail_candidates: dict[str, Path] = {}
        for path in self.originals_dir.iterdir():
            if path.is_symlink():
                self._quarantine_if_exists_sync(path)
                changed = True
                continue
            if path.is_file() and _CACHE_ID_RE.fullmatch(path.stem):
                if path.stem in known_ids:
                    indexed = self._find_item(path.stem)
                    if indexed is None or path.resolve() != self._item_path(indexed, thumbnail=False):
                        self._quarantine_if_exists_sync(path)
                        changed = True
                    continue
                if path.stem in original_candidates:
                    self._quarantine_if_exists_sync(path)
                    changed = True
                    continue
                original_candidates[path.stem] = path
            elif path.is_file():
                self._quarantine_if_exists_sync(path)
                changed = True
        for path in self.thumbnails_dir.iterdir():
            if path.is_symlink():
                self._quarantine_if_exists_sync(path)
                changed = True
                continue
            if path.is_file() and path.suffix.lower() == ".webp" and _CACHE_ID_RE.fullmatch(path.stem):
                if path.stem in known_ids:
                    indexed = self._find_item(path.stem)
                    if indexed is None or path.resolve() != self._item_path(indexed, thumbnail=True):
                        self._quarantine_if_exists_sync(path)
                        changed = True
                    continue
                thumbnail_candidates[path.stem] = path
            elif path.is_file():
                self._quarantine_if_exists_sync(path)
                changed = True

        orphan_ids = (set(original_candidates) | set(thumbnail_candidates)) - known_ids
        for cache_id in sorted(orphan_ids):
            original = original_candidates.get(cache_id)
            thumbnail = thumbnail_candidates.get(cache_id)
            if original is not None and thumbnail is not None:
                recovered = self._recover_entry_sync(cache_id, original, thumbnail)
                if recovered is not None:
                    self._items.append(recovered)
                else:
                    self._quarantine_if_exists_sync(original)
                    self._quarantine_if_exists_sync(thumbnail)
                changed = True
            else:
                if original is not None:
                    self._quarantine_if_exists_sync(original)
                if thumbnail is not None:
                    self._quarantine_if_exists_sync(thumbnail)
                changed = True

        self._items.sort(key=self._sort_key)
        return changed

    def _prepare_image_sync(self, image_bytes: bytes) -> dict[str, Any]:
        try:
            from PIL import Image, UnidentifiedImageError
        except ImportError as exc:
            raise CacheError("图片缓存需要 Pillow") from exc

        try:
            with Image.open(BytesIO(image_bytes)) as image:
                image_format = str(image.format or "").upper()
                if image_format not in _SUPPORTED_FORMATS:
                    raise CacheError("缓存仅支持 PNG、JPEG 和 WebP")
                if bool(getattr(image, "is_animated", False)) or int(getattr(image, "n_frames", 1)) != 1:
                    raise CacheError("无法缓存动态图片")
                image.load()
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise CacheError("图片尺寸无效")
                thumbnail = image.copy()
                if thumbnail.mode not in {"RGB", "RGBA"}:
                    thumbnail = thumbnail.convert("RGBA" if "A" in thumbnail.getbands() else "RGB")
                thumbnail.thumbnail((512, 512), Image.Resampling.LANCZOS)
                output = BytesIO()
                thumbnail.save(output, format="WEBP", quality=82, method=6)
                extension, content_type = _SUPPORTED_FORMATS[image_format]
                return {
                    "format": image_format.lower().replace("jpeg", "jpg"),
                    "extension": extension,
                    "content_type": content_type,
                    "width": width,
                    "height": height,
                    "thumbnail_bytes": output.getvalue(),
                }
        except CacheError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise CacheError("生成结果不是有效的静态图片") from exc

    def _render_preview_sync(self, path: Path, max_edge: int) -> bytes:
        try:
            from PIL import Image, UnidentifiedImageError
        except ImportError as exc:
            raise CacheError("图片预览需要 Pillow") from exc

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    image_format = str(image.format or "").upper()
                    if image_format not in _SUPPORTED_FORMATS:
                        raise CacheError("缓存原图格式无效")
                    if bool(getattr(image, "is_animated", False)) or int(
                        getattr(image, "n_frames", 1)
                    ) != 1:
                        raise CacheError("无法预览动态图片")
                    image.load()
                    has_alpha = (
                        "A" in image.getbands()
                        or "transparency" in image.info
                    )
                    preview = image.convert("RGBA" if has_alpha else "RGB")
                    preview.thumbnail(
                        (max_edge, max_edge),
                        Image.Resampling.LANCZOS,
                        reducing_gap=2.0,
                    )
                    output = BytesIO()
                    preview.save(
                        output,
                        format="WEBP",
                        quality=88,
                        method=4,
                    )
                    return output.getvalue()
        except CacheError:
            raise
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise CacheError("缓存原图不是有效的静态图片") from exc

    def _store_sync(
        self,
        image_bytes: bytes,
        prepared: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        cache_id = uuid.uuid4().hex
        original_name = f"{cache_id}.{prepared['extension']}"
        thumbnail_name = f"{cache_id}.webp"
        original_path = self._safe_child(self.originals_dir, original_name)
        thumbnail_path = self._safe_child(self.thumbnails_dir, thumbnail_name)
        original_temp = self._safe_child(self.originals_dir, f".{cache_id}.{uuid.uuid4().hex}.tmp")
        thumbnail_temp = self._safe_child(self.thumbnails_dir, f".{cache_id}.{uuid.uuid4().hex}.tmp")
        moved_original = False
        moved_thumbnail = False
        evicted_items: list[dict[str, Any]] = []
        staged_files: list[tuple[Path, Path]] = []

        entry = self._build_entry(
            cache_id,
            original_name,
            thumbnail_name,
            len(image_bytes),
            prepared,
            metadata,
        )
        try:
            self._write_bytes_sync(original_temp, image_bytes)
            self._write_bytes_sync(thumbnail_temp, prepared["thumbnail_bytes"])
            os.replace(original_temp, original_path)
            moved_original = True
            os.replace(thumbnail_temp, thumbnail_path)
            moved_thumbnail = True
            self._items.append(entry)
            self._items.sort(key=self._sort_key)
            evicted_items = self._pop_excess_sync()
            staged_files = self._stage_item_files_sync(evicted_items)
            self._write_index_sync()
            self._discard_staged_files_sync(staged_files)
            return entry
        except Exception:
            self._restore_staged_files_sync(staged_files)
            if entry in self._items:
                self._items.remove(entry)
            if evicted_items:
                self._items.extend(evicted_items)
                self._items.sort(key=self._sort_key)
            if moved_original:
                original_path.unlink(missing_ok=True)
            if moved_thumbnail:
                thumbnail_path.unlink(missing_ok=True)
            raise
        finally:
            original_temp.unlink(missing_ok=True)
            thumbnail_temp.unlink(missing_ok=True)

    def _build_entry(
        self,
        cache_id: str,
        original_name: str,
        thumbnail_name: str,
        byte_count: int,
        prepared: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        width = int(prepared["width"])
        height = int(prepared["height"])
        return {
            "id": cache_id,
            "created_at": created_at,
            "mode": self._bounded_string(metadata.get("mode"), "generate"),
            "model": self._bounded_string(metadata.get("model"), "unknown"),
            "size": self._bounded_string(metadata.get("size"), f"{width}x{height}"),
            "width": width,
            "height": height,
            "format": str(prepared["format"]),
            "file_size": byte_count,
            "user_id": self._bounded_string(metadata.get("user_id", metadata.get("sender_id")), ""),
            "user_name": self._bounded_string(metadata.get("user_name", metadata.get("sender_name")), ""),
            "chat_type": self._bounded_string(metadata.get("chat_type"), "unknown"),
            "conversation_id": self._bounded_string(
                metadata.get("conversation_id", metadata.get("session_id")),
                "",
            ),
            "conversation_name": self._bounded_string(
                metadata.get("conversation_name", metadata.get("session_name")),
                "",
            ),
            "original_file": original_name,
            "thumbnail_file": thumbnail_name,
            "content_type": str(prepared["content_type"]),
        }

    def _normalize_index_entry(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        cache_id = raw.get("id")
        original_name = raw.get("original_file")
        thumbnail_name = raw.get("thumbnail_file")
        if not isinstance(cache_id, str) or not _CACHE_ID_RE.fullmatch(cache_id):
            return None
        if (
            not isinstance(original_name, str)
            or Path(original_name).name != original_name
            or not original_name.startswith(f"{cache_id}.")
        ):
            return None
        if thumbnail_name != f"{cache_id}.webp":
            return None
        extension = Path(original_name).suffix.lower()
        if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
            return None

        entry = {
            "id": cache_id,
            "created_at": self._bounded_string(raw.get("created_at"), ""),
            "mode": self._bounded_string(raw.get("mode"), "generate"),
            "model": self._bounded_string(raw.get("model"), "unknown"),
            "size": self._bounded_string(raw.get("size"), ""),
            "width": self._safe_int(raw.get("width")),
            "height": self._safe_int(raw.get("height")),
            "format": self._bounded_string(raw.get("format"), extension.lstrip(".")),
            "file_size": self._safe_int(raw.get("file_size")),
            "user_id": self._bounded_string(raw.get("user_id"), ""),
            "user_name": self._bounded_string(raw.get("user_name"), ""),
            "chat_type": self._bounded_string(raw.get("chat_type"), "unknown"),
            "conversation_id": self._bounded_string(raw.get("conversation_id"), ""),
            "conversation_name": self._bounded_string(raw.get("conversation_name"), ""),
            "original_file": original_name,
            "thumbnail_file": thumbnail_name,
            "content_type": self._content_type_for_extension(extension),
        }
        return entry

    def _recover_entry_sync(self, cache_id: str, original: Path, thumbnail: Path) -> dict[str, Any] | None:
        extension = original.suffix.lower()
        if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
            return None
        try:
            stat = original.stat()
            created = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            return {
                "id": cache_id,
                "created_at": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "mode": "recovered",
                "model": "unknown",
                "size": "",
                "width": 0,
                "height": 0,
                "format": extension.lstrip(".").replace("jpeg", "jpg"),
                "file_size": stat.st_size,
                "user_id": "",
                "user_name": "",
                "chat_type": "unknown",
                "conversation_id": "",
                "conversation_name": "",
                "original_file": original.name,
                "thumbnail_file": thumbnail.name,
                "content_type": self._content_type_for_extension(extension),
            }
        except OSError:
            return None

    def _write_index_sync(self) -> None:
        payload = {"version": 1, "items": self._items}
        temp_path = self._safe_child(self.root, f".index.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.index_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _backup_corrupt_index_sync(self) -> None:
        if not self.index_path.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self._safe_child(self.orphaned_dir, f"index.corrupt.{stamp}.{uuid.uuid4().hex}.json")
        try:
            os.replace(self.index_path, backup)
        except OSError:
            shutil.copy2(self.index_path, backup)
            self.index_path.unlink(missing_ok=True)

    def _pop_excess_sync(self) -> list[dict[str, Any]]:
        if self._limit == 0 or len(self._items) <= self._limit:
            return []
        count = len(self._items) - self._limit
        evicted = self._items[:count]
        del self._items[:count]
        return evicted

    def _remove_item_files_sync(self, item: Mapping[str, Any]) -> None:
        for thumbnail in (False, True):
            try:
                self._item_path(item, thumbnail=thumbnail).unlink(missing_ok=True)
            except (OSError, CacheError):
                continue

    def _stage_item_files_sync(
        self,
        items: list[dict[str, Any]],
    ) -> list[tuple[Path, Path]]:
        staged: list[tuple[Path, Path]] = []
        try:
            for item in items:
                for thumbnail in (False, True):
                    source = self._item_path(item, thumbnail=thumbnail)
                    if not source.is_file():
                        continue
                    target = self._safe_child(
                        self.orphaned_dir,
                        f".pending-delete.{uuid.uuid4().hex}.{source.name}",
                    )
                    os.replace(source, target)
                    staged.append((target, source))
            return staged
        except Exception:
            self._restore_staged_files_sync(staged)
            raise

    @staticmethod
    def _restore_staged_files_sync(staged: list[tuple[Path, Path]]) -> None:
        for staged_path, original_path in reversed(staged):
            try:
                if staged_path.exists():
                    os.replace(staged_path, original_path)
            except OSError:
                continue

    @staticmethod
    def _discard_staged_files_sync(staged: list[tuple[Path, Path]]) -> None:
        for staged_path, _ in staged:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                # The item is already absent from the index and inaccessible
                # through the Page. Keep the randomly named staged file for a
                # later cleanup attempt (initialize/store/list all retry).
                continue

    def _cleanup_pending_deletes_sync(self) -> None:
        try:
            resolved_dir = self.orphaned_dir.resolve()
            self._assert_within_root(resolved_dir)
            if not resolved_dir.is_dir():
                return
            candidates = list(resolved_dir.iterdir())
        except (OSError, CacheError):
            return
        for path in candidates:
            if not path.name.startswith(".pending-delete."):
                continue
            try:
                self._assert_within_root(path.parent.resolve())
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
            except (OSError, CacheError):
                continue

    def _quarantine_if_exists_sync(self, path: Path) -> None:
        try:
            self._assert_within_root(path.parent.resolve())
            if not path.is_file() and not path.is_symlink():
                return
            # Unknown, incomplete, or unsafe cache files must not survive
            # outside the indexed cache limit. Unlinking a symlink removes the
            # link itself and never follows it to an external target.
            path.unlink(missing_ok=True)
        except (OSError, CacheError):
            return

    def _item_path(self, item: Mapping[str, Any], *, thumbnail: bool) -> Path:
        key = "thumbnail_file" if thumbnail else "original_file"
        name = item.get(key)
        if not isinstance(name, str) or Path(name).name != name:
            raise CacheError("缓存索引包含无效路径")
        directory = self.thumbnails_dir if thumbnail else self.originals_dir
        return self._safe_child(directory, name)

    def _safe_child(self, directory: Path, filename: str) -> Path:
        if not filename or Path(filename).name != filename:
            raise CacheError("缓存路径无效")
        path = (directory / filename).resolve()
        self._assert_within_root(path)
        try:
            path.relative_to(directory.resolve())
        except ValueError as exc:
            raise CacheError("缓存路径越界") from exc
        return path

    def _assert_within_root(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise CacheError("缓存路径越界") from exc

    def _find_item(self, cache_id: str) -> dict[str, Any] | None:
        return next((item for item in self._items if item["id"] == cache_id), None)

    def _public_entry(self, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in item.items()
            if key not in {"original_file", "thumbnail_file"}
        }

    def _sort_key(self, item: Mapping[str, Any]) -> tuple[str, str]:
        return str(item.get("created_at", "")), str(item.get("id", ""))

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise CacheError("图片缓存尚未初始化")

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 20:
            raise CacheError("缓存数量必须是 0 到 20 的整数")
        return limit

    @staticmethod
    def _validate_cache_id(cache_id: str) -> None:
        if not isinstance(cache_id, str) or not _CACHE_ID_RE.fullmatch(cache_id):
            raise CacheNotFoundError("缓存图片不存在")

    @staticmethod
    def _write_bytes_sync(path: Path, data: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _bounded_string(value: Any, default: str) -> str:
        if value is None:
            return default
        text = str(value)
        return text[:_METADATA_STRING_LIMIT]

    @staticmethod
    def _safe_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _content_type_for_extension(extension: str) -> str:
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(extension.lower(), "application/octet-stream")
