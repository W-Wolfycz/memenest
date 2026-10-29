"""增量需求：发送压缩——缩尺寸、压体积、动图降帧、缓存复用、失败回落。

守的静默失效：配置写着压缩但发出去的还是原图（超大图把平台撑爆）、
动图被压成静态图（表情包动效丢失）、压缩失败后整条发不出去。
"""

from __future__ import annotations

from PIL import Image

from memenest.publish.compress import clear_cache, compress_for_send


def _big_static(path, size: int = 1600) -> None:
    image = Image.new("RGB", (size, size), (30, 120, 200))
    for offset in range(0, size, 40):
        for column in range(0, size, 40):
            color = ((offset * 7) % 256, (column * 3) % 256, 90)
            image.paste(color, (offset, column, offset + 30, column + 30))
    image.save(path, format="PNG")


def _animated_gif(path, frames: int = 20) -> None:
    images = []
    spacing = 120
    for index in range(frames):
        frame = Image.new("RGB", (400, 400), (255, 255, 255))
        box = Image.new("RGB", (spacing, spacing), (index * 10 % 256, 80, 160))
        frame.paste(box, (index * 8 % 200, spacing))
        images.append(frame)
    images[0].save(
        path, save_all=True, append_images=images[1:], duration=80, loop=0, format="GIF"
    )
def test_clear_cache_removes_products(tmp_path):
    """清缓存要真的清空产物：残留会随发送次数线性增长，只有磁盘发现得了。"""
    source = tmp_path / "big.png"
    _big_static(source, 600)
    cache = tmp_path / "cache"
    assert compress_for_send(source, cache, max_side=128, max_bytes=256 * 1024).ok
    assert clear_cache(cache) >= 1
    assert list(cache.iterdir()) == []
