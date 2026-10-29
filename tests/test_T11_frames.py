"""T-11 / R-7：动图只送一张合成图，且不残留临时文件。

守的静默失效：把整段动图塞给模型（成本与延迟失控），
或者超时/取消路径漏删合成图（临时目录无限增长）。
"""

from __future__ import annotations

import asyncio

import pytest
from PIL import Image

from memenest.ingest.analyze import analyze_detailed


def _frame(index: int, size: int = 64) -> Image.Image:
    image = Image.new("RGB", (size, size), (255, 255, 255))
    box = Image.new("RGB", (size // 3, size // 3), ((index * 12) % 256, 80, 160))
    image.paste(box, ((index * 5) % (size // 2), (index * 3) % (size // 2)))
    return image


def _write_gif(path, count: int = 20) -> None:
    images = [_frame(index) for index in range(count)]
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=80,
        loop=0,
        format="GIF",
    )
@pytest.mark.asyncio
async def test_animated_input_reaches_model_as_exactly_one_image(tmp_path):
    """动图只送一张合成图：整段送上模型会让单次识图成本按帧数翻倍，账单单上才看得出。"""
    source = tmp_path / "anim.gif"
    _write_gif(source, 12)
    seen: list[list[str]] = []

    async def llm(prompt: str, image_paths: list[str]) -> str:
        seen.append(list(image_paths))
        return '{"label": "laugh", "caption": "猫笑翻"}'

    outcome = await analyze_detailed(
        source, prompt="p", llm_call=llm, timeout=5, work_dir=tmp_path
    )
    assert outcome.analysis is not None
    assert len(seen) == 1
    assert len(seen[0]) == 1
    assert seen[0][0].endswith("-grid.jpg")
    # 合成图必须在返回前删除（含成功路径）。
    assert list(tmp_path.glob("*-grid.jpg")) == []


@pytest.mark.asyncio
async def test_timeout_still_cleans_the_grid(tmp_path):
    """超时路径也要删合成图：漏删会让临时目录随动图数量无限增长。"""
    source = tmp_path / "anim.gif"
    _write_gif(source, 9)

    async def slow(prompt: str, image_paths: list[str]) -> str:
        await asyncio.sleep(5)
        return "{}"

    outcome = await analyze_detailed(
        source, prompt="p", llm_call=slow, timeout=0.05, work_dir=tmp_path
    )
    assert outcome.analysis is None
    assert outcome.reason == "model_timeout"
    assert list(tmp_path.glob("*-grid.jpg")) == []
