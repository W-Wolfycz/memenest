"""两级指纹（R-5）。

content_hash：字节级 SHA-256，任何像素级改写都会变。
percept_hash：8x8 灰度梯度指纹（64 bit），用来识别不同压缩率/尺寸的同图。

算指纹前先裁掉纯色边框：同一张图加一圈白边是最常见的二次传播形态，
不裁的话白边会挤占 9x8 网格，距离从 0 直接跳到 10 以上，漏判成新图。
"""

from __future__ import annotations

import hashlib

PERCEPT_SIDE_X = 9
PERCEPT_SIDE_Y = 8
PERCEPT_BITS = (PERCEPT_SIDE_X - 1) * PERCEPT_SIDE_Y
PERCEPT_HEX_WIDTH = PERCEPT_BITS // 4

# 边框判定：与左上角的通道差在这个范围内算同色
BORDER_TOLERANCE = 16
# 裁完比这还小，说明判定失败（或者图本身就是纯色），放弃裁剪
BORDER_MIN_KEEP = 0.35


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def trim_border(image: object) -> object:
    """裁掉四边与左上角同色的纯色边框；不是边框就原样返回。

    只在真的检测到边框时才裁——无边框的图裁完会丢掉有效边缘，
    所以留了「裁完太小就放弃」的兜底。
    """
    try:
        rgb = image.convert("RGB")  # type: ignore[attr-defined]
        width, height = rgb.size
        if width < 16 or height < 16:
            return image
        pixels = rgb.load()
        base = pixels[0, 0]
    except Exception:
        return image

    def same(color: object) -> bool:
        return (
            abs(color[0] - base[0]) <= BORDER_TOLERANCE  # type: ignore[index]
            and abs(color[1] - base[1]) <= BORDER_TOLERANCE  # type: ignore[index]
            and abs(color[2] - base[2]) <= BORDER_TOLERANCE  # type: ignore[index]
        )

    step_x = max(1, width // 64)
    step_y = max(1, height // 64)

    try:
        top = 0
        while top < height and all(same(pixels[x, top]) for x in range(0, width, step_x)):
            top += 1
        bottom = height - 1
        while bottom > top and all(
            same(pixels[x, bottom]) for x in range(0, width, step_x)
        ):
            bottom -= 1
        left = 0
        while left < width and all(
            same(pixels[left, y]) for y in range(top, bottom + 1, step_y)
        ):
            left += 1
        right = width - 1
        while right > left and all(
            same(pixels[right, y]) for y in range(top, bottom + 1, step_y)
        ):
            right -= 1
    except Exception:
        return image

    kept_w = right - left + 1
    kept_h = bottom - top + 1
    if kept_w < width * BORDER_MIN_KEEP or kept_h < height * BORDER_MIN_KEEP:
        return image
    if kept_w < 8 or kept_h < 8:
        return image
    if (kept_w, kept_h) == (width, height):
        return image
    try:
        return rgb.crop((left, top, right + 1, bottom + 1))
    except Exception:
        return image


def percept_hash(image: object) -> str | None:
    """对 PIL 图像算梯度指纹；任何异常都返回 None（退化为只按 content_hash 去重）。"""
    try:
        gray = trim_border(image).convert("L").resize(  # type: ignore[attr-defined]
            (PERCEPT_SIDE_X, PERCEPT_SIDE_Y)
        )
        # L 模式每个像素一字节，tobytes 比 getdata 更快且不触发弃用警告。
        pixels = gray.tobytes()
    except Exception:
        return None
    bits = 0
    index = 0
    for row in range(PERCEPT_SIDE_Y):
        offset = row * PERCEPT_SIDE_X
        for col in range(PERCEPT_SIDE_X - 1):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            if left > right:
                bits |= 1 << index
            index += 1
    if bits == 0:
        # 全无梯度（纯色块之类）：所有这种图会算出同一个值，拿去比对只会互相误判
        return None
    return f"{bits:0{PERCEPT_HEX_WIDTH}x}"


def hamming(left: str, right: str) -> int:
    """两个感知指纹的汉明距离；非法输入按「完全不同」处理。"""
    try:
        value = int(left, 16) ^ int(right, 16)
    except (TypeError, ValueError):
        return PERCEPT_BITS
    return int.bit_count(value)


__all__ = [
    "PERCEPT_BITS",
    "content_hash",
    "hamming",
    "percept_hash",
    "trim_border",
]
