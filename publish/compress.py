"""发送前压缩：把原图缩成表情包尺寸与体积，再交给平台。

目标是「发得出去、不刷屏、省流量」，不是归档：原图永远保留在库里，压缩产物落在
`cache/out/`，可以随时整目录删掉重建。压缩失败一律回落原图（宁可发得难看，也不能不发）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

CACHE_SUBDIR = "cache/out"
ANIMATED_MAX_FRAMES = 60
ANIMATED_MAX_FPS = 15
JPEG_QUALITIES = (85, 75, 65, 55, 45)
STATIC_MIN_SIDE = 128


@dataclass(slots=True)
class CompressResult:
    path: Path | None
    reason: str
    byte_size: int = 0

    @property
    def ok(self) -> bool:
        return self.path is not None


def _pick_indices(total: int, wanted: int) -> list[int]:
    if total <= 0:
        return []
    if total <= wanted:
        return list(range(total))
    if wanted <= 1:
        return [0]
    return [round(i * (total - 1) / (wanted - 1)) for i in range(wanted)]


def _cache_path(source: Path, cache_dir: Path, ext: str, params: str) -> Path:
    try:
        stat = source.stat()
        key = f"{source.name}:{stat.st_size}:{int(stat.st_mtime)}:{params}"
    except OSError:
        key = f"{source.name}:{params}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return cache_dir / f"{digest}{ext}"


def _save_static(image: object, target: Path, max_bytes: int) -> bool:
    from ..ingest.frames import flatten_to_rgb

    # 透明 PNG/WebP 直接 convert("RGB") 会把透明区画成黑底，这里统一合到白底。
    frame = flatten_to_rgb(image)
    for quality in JPEG_QUALITIES:
        frame.save(target, format="JPEG", quality=quality, optimize=True)
        if target.stat().st_size <= max_bytes:
            return True
    return target.stat().st_size <= max_bytes


def _save_animated(image: object, target: Path, max_side: int, max_bytes: int) -> bool:
    from PIL import Image

    from ..ingest.frames import flatten_to_rgb

    total = int(getattr(image, "n_frames", 1))
    source_duration = int(getattr(image, "info", {}).get("duration", 100) or 100)
    duration = max(source_duration, int(1000 / ANIMATED_MAX_FPS))
    frames = []
    for index in _pick_indices(total, min(total, ANIMATED_MAX_FRAMES)):
        image.seek(index)  # type: ignore[attr-defined]
        frame = flatten_to_rgb(image.copy())
        frame.thumbnail((max_side, max_side), Image.LANCZOS)  # type: ignore[attr-defined]
        frames.append(frame)
    if not frames:
        return False
    frames[0].save(  # type: ignore[attr-defined]
        target,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
        optimize=True,
    )
    return target.stat().st_size <= max_bytes


def compress_for_send(
    source: Path,
    cache_dir: Path,
    *,
    max_side: int = 512,
    max_bytes: int = 512 * 1024,
) -> CompressResult:
    """返回压缩后的文件；任何失败都返回 path=None，调用方用原图。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - requirements 已声明
        return CompressResult(None, "pillow_missing")

    try:
        if not source.is_file():
            return CompressResult(None, "source_missing")
        try:
            size = source.stat().st_size
        except OSError:
            return CompressResult(None, "source_missing")
        side = max(STATIC_MIN_SIDE, int(max_side))
        with Image.open(source) as image:
            # 原图本来就够小：直接发原图，免得压完反而更糊。
            if size <= max_bytes and max(image.size) <= side:
                return CompressResult(source, "as_is", size)
            animated = int(getattr(image, "n_frames", 1)) > 1
            cache_dir.mkdir(parents=True, exist_ok=True)
            if animated:
                target = _cache_path(source, cache_dir, ".gif", f"{side}:{max_bytes}:a")
                if target.is_file() and target.stat().st_size > 0:
                    return CompressResult(target, "cached", target.stat().st_size)
                ok = _save_animated(image, target, side, max_bytes)
            else:
                target = _cache_path(source, cache_dir, ".jpg", f"{side}:{max_bytes}:s")
                if target.is_file() and target.stat().st_size > 0:
                    return CompressResult(target, "cached", target.stat().st_size)
                frame = image.copy()
                frame.thumbnail((side, side), Image.LANCZOS)
                ok = _save_static(frame, target, max_bytes)
        if not ok:
            _silent_unlink(target)
            return CompressResult(None, "too_large_after_compress")
        size = target.stat().st_size
        if size > max_bytes:
            _silent_unlink(target)
            return CompressResult(None, "too_large_after_compress")
        return CompressResult(target, "compressed", size)
    except Exception as exc:
        return CompressResult(None, f"compress_error_{type(exc).__name__}")


def clear_cache(cache_dir: Path) -> int:
    """清空压缩缓存（产物可再生，随时可删）。"""
    removed = 0
    if not cache_dir.exists():
        return 0
    for entry in cache_dir.iterdir():
        try:
            if entry.is_file():
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = ["CACHE_SUBDIR", "CompressResult", "clear_cache", "compress_for_send"]
