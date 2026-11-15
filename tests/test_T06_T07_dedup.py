"""T-06 / T-07 / R-5：两级指纹去重。

守的静默失效：不同压缩率的同图被重复入库（库容量被同一条表情吃掉），
或者感知阈值形同虚设（比对实现写反、距离算成相等）。
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from _fixtures import FakeMemoSource
from memenest.ingest.dedup import find_duplicate
from memenest.ingest.fingerprints import (
    animated_percept_hash,
    content_hash,
    hamming,
    percept_hash,
)


def _gradient(seed: int, size: int = 128) -> Image.Image:
    image = Image.new("L", (size, size))
    pixels = image.load()
    for x in range(size):
        for y in range(size):
            pixels[x, y] = (x * 2 + y + seed * 40) % 256
    return image.convert("RGB")


def _jpeg_bytes(image: Image.Image, quality: int) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def test_T07_same_image_different_compression_matches():
    """同图不同压缩率必须判重：漏判会让同一张表情以多个副本占掉库容量。"""
    image = _gradient(3)
    high = percept_hash(Image.open(BytesIO(_jpeg_bytes(image, 92))))
    low = percept_hash(Image.open(BytesIO(_jpeg_bytes(image, 25))))
    assert high is not None and low is not None
    assert hamming(high, low) <= 8


def test_different_images_stay_far_apart():
    """不同图不能被判重：误判是静默吞图，用户只会觉得某张图收了却没进来。"""
    left = percept_hash(_gradient(3))
    right = percept_hash(_gradient(200))
    assert left is not None and right is not None
    assert hamming(left, right) > 8


def test_T06_percept_hit_is_duplicate():
    """感知指纹命中要算重复并带回已有 id：漏判同上，重复副本悄悄堆积。"""
    image = _gradient(5)
    digest = percept_hash(image)
    assert digest is not None
    source = FakeMemoSource(rows=[(11, digest)])
    verdict = find_duplicate(source, "b" * 64, digest, threshold=8)
    assert verdict.duplicate is True
    assert verdict.reason == "duplicate_percept"
    assert verdict.existing_id == 11


def test_content_hash_algorithm_is_pinned():
    """换算法会让历史 blocklist 与去重基线全部作废，必须钉死字面量。"""
    assert content_hash(b"memenest") == (
        "b871327ba392533949c247006a1e2bed5cb193fb34da53a5d1ede21f9751357f"
    )

def _pad(image: Image.Image, ratio: float, color: tuple[int, int, int] = (255, 255, 255)):
    width, height = image.size
    pad_x, pad_y = int(width * ratio), int(height * ratio)
    canvas = Image.new("RGB", (width + 2 * pad_x, height + 2 * pad_y), color)
    canvas.paste(image, (pad_x, pad_y))
    return canvas


def test_border_padded_copy_is_duplicate():
    """同一张图加一圈纯色边框必须仍判重。

    加边是二次传播里最常见的形态：不裁掉的话白边会挤占 9x8 网格，
    距离从 0 直接跳到 10 以上，同一张表情就会带着不同边框反复入库。
    """
    image = _gradient(7)
    base = percept_hash(image)
    assert base is not None
    for ratio in (0.05, 0.12, 0.25):
        padded = _pad(image, ratio)
        distance = hamming(base, percept_hash(padded))
        assert distance <= 8, f"边距 {ratio:.0%} 时距离 {distance}，被判成了新图"


def test_solid_image_yields_no_percept_hash():
    """纯色图算不出梯度指纹，必须返回 None。

    否则所有纯色图都得到全 0 指纹，互相之间一律判重——等于把「没特征的图」
    当成「同一张图」。
    """
    assert percept_hash(Image.new("RGB", (200, 200), (120, 40, 200))) is None


def _save_gif(frames: list[Image.Image], path, size: tuple[int, int]) -> None:
    """同一组帧按给定尺寸存一份 GIF：字节不同，帧内容按比例缩放。"""
    resized = [frame.resize(size) for frame in frames]
    resized[0].save(path, save_all=True, append_images=resized[1:], duration=100, loop=0)


def test_same_animation_resized_is_duplicate_but_first_frame_twin_is_not(tmp_path):
    """动图判重的两个方向。

    - 同一动画改尺寸重编码：三帧都在阈值内 → 判重；
    - 首帧相同、后续帧完全不同的两个动画：最差帧把距离拉开 → 不判重。
    后者写坏（退化成只比首帧）时新表情会被静默吞掉，只有比对能拦。
    """

    def stripes(offset: int) -> Image.Image:
        image = Image.new("L", (48, 48))
        pixels = image.load()
        for x in range(48):
            for y in range(48):
                pixels[x, y] = 255 if ((x + offset) // 6) % 2 else 0
        return image.convert("RGB")

    from PIL import Image as PILImage

    same_frames = [stripes(0), stripes(6)]
    a_path, b_path = tmp_path / "a.gif", tmp_path / "b.gif"
    _save_gif(same_frames, a_path, (48, 48))
    _save_gif(same_frames, b_path, (40, 40))  # 同动画、改尺寸 → 字节不同

    twin_frames = [stripes(0), stripes(3)]  # 首帧相同、第二帧相位错开
    c_path = tmp_path / "c.gif"
    _save_gif(twin_frames, c_path, (48, 48))

    hashes = {}
    for key, path in (("a", a_path), ("b", b_path), ("c", c_path)):
        with PILImage.open(path) as image:
            hashes[key] = animated_percept_hash(image)
    assert all(hashes.values()), "前置条件：三份动图都要算得出复合指纹"

    same = FakeMemoSource(rows=[(21, hashes["a"])])
    verdict = find_duplicate(same, "c" * 64, hashes["b"], threshold=8)
    assert verdict.duplicate is True, "同动画改尺寸必须判重"

    twin = FakeMemoSource(rows=[(21, hashes["a"])])
    verdict = find_duplicate(twin, "c" * 64, hashes["c"], threshold=8)
    assert verdict.duplicate is False, "首帧相同的不同动画不能判重"

    # 混存期：复合 vs 老库单帧（首帧指纹）仍能判重
    with PILImage.open(a_path) as image:
        image.seek(0)
        first_frame_only = percept_hash(image.copy())
    assert first_frame_only is not None
    mixed = FakeMemoSource(rows=[(31, first_frame_only)])
    verdict = find_duplicate(mixed, "d" * 64, hashes["a"], threshold=8)
    assert verdict.duplicate is True, "复合指纹要能对上老库的首帧指纹"
