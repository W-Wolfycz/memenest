"""动图合成（R-7）：把多帧压成一张等距拼图再送模型。

约定：只产出静态图，绝不把整段动图交给视觉模型；失败返回 False，由调用方
回落到「单帧」路径。临时文件清理责任在调用方（finally 里删）。
"""

from __future__ import annotations

import math
from pathlib import Path

GRID_FRAMES = 9
GRID_COLUMNS = 3
GRID_MAX_SIDE = 2048
GRID_GAP = 2
GRID_BACKGROUND = (255, 255, 255)


def has_animation(path: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 依赖缺失时按静态图处理
        return False
    try:
        with Image.open(path) as image:
            return int(getattr(image, "n_frames", 1)) > 1
    except Exception:
        return False


def frame_count(path: Path) -> int:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return 1
    try:
        with Image.open(path) as image:
            return max(1, int(getattr(image, "n_frames", 1)))
    except Exception:
        return 1


def _pick_indices(total: int, wanted: int) -> list[int]:
    if total <= 0:
        return []
    if total <= wanted:
        # 帧数不足时重复补齐，保证宫格形状稳定。
        return [min(total - 1, i * total // wanted) for i in range(wanted)]
    if wanted == 1:
        return [0]
    return [round(i * (total - 1) / (wanted - 1)) for i in range(wanted)]


def flatten_to_rgb(frame: object) -> object:
    """把任意模式（含透明）合到不透明底色上。合成拼图与发送压缩共用。"""
    from PIL import Image

    rgba = frame.convert("RGBA")  # type: ignore[attr-defined]
    canvas = Image.new("RGB", rgba.size, GRID_BACKGROUND)
    canvas.paste(rgba, mask=rgba.split()[-1])
    return canvas


def _flatten(frame: object) -> object:
    return flatten_to_rgb(frame)


def compose_grid(
    source: Path,
    target: Path,
    frames: int = GRID_FRAMES,
    max_side: int = GRID_MAX_SIDE,
) -> bool:
    """把动图前 N 帧拼成一张静态图；源是静态图或合成失败时返回 False。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return False

    try:
        with Image.open(source) as image:
            total = int(getattr(image, "n_frames", 1))
            if total <= 1:
                return False
            indices = _pick_indices(total, max(1, frames))
            tiles: list[object] = []
            for index in indices:
                image.seek(index)
                tiles.append(_flatten(image.copy()))
            if not tiles:
                return False
            base = tiles[0]
            width, height = base.size  # type: ignore[attr-defined]
            if width <= 0 or height <= 0:
                return False
            normalized = [
                tile
                if tile.size == (width, height)  # type: ignore[attr-defined]
                else tile.resize((width, height), Image.LANCZOS)  # type: ignore[attr-defined]
                for tile in tiles
            ]
            columns = min(GRID_COLUMNS, len(normalized))
            rows = math.ceil(len(normalized) / columns)
            canvas = Image.new(
                "RGB",
                (
                    columns * width + GRID_GAP * (columns - 1),
                    rows * height + GRID_GAP * (rows - 1),
                ),
                GRID_BACKGROUND,
            )
            for position, tile in enumerate(normalized):
                column = position % columns
                row = position // columns
                canvas.paste(
                    tile,
                    (
                        column * (width + GRID_GAP),
                        row * (height + GRID_GAP),
                    ),
                )
            if max(canvas.size) > max_side:
                canvas.thumbnail((max_side, max_side))
            target.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(target, format="JPEG", quality=88)
            return True
    except Exception:
        return False


__all__ = ["compose_grid", "frame_count", "has_animation", "GRID_FRAMES"]
