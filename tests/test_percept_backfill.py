"""感知指纹回填。

守的静默失效：
- 补算成功但没写库：下次「补齐」照样把它当缺指纹，看起来一直在跑、实际没生效；
- 动图被算进指纹：首帧相同的两个动画会互相判重，新图被丢；
- 补不出来的条目（文件缺失/动图）没有本地记账，会被反复重扫。
"""

from __future__ import annotations

import pytest
from PIL import Image

from memenest.domain import STATE_READY, Analysis, Draft
from memenest.ingest.backfill import backfill_percepts
from memenest.store import MemoStore


def _stripes(seed: int = 0) -> Image.Image:
    image = Image.new("L", (32, 32))
    pixels = image.load()
    for x in range(32):
        for y in range(32):
            pixels[x, y] = 255 if ((x + seed) // 4) % 2 else 0
    return image.convert("RGB")


def _draft(rel_path: str) -> Draft:
    return Draft(
        rel_path=rel_path,
        content_hash=rel_path.encode().hex().ljust(64, "a")[:64],
        percept_hash=None,
        analysis=Analysis(label="joy", caption=rel_path),
        scope="any",
        origin_umo=None,
        origin_kind="import",
        created_at=1,
        state=STATE_READY,
    )


@pytest.mark.asyncio
async def test_backfill_writes_fingerprints_and_skips_unreadable(tmp_path):
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    data = tmp_path / "data"
    (data / "blobs").mkdir(parents=True)

    _stripes(0).save(data / "blobs" / "a.png")
    normal = store.add(_draft("blobs/a.png"))
    missing = store.add(_draft("blobs/missing.png"))
    _stripes(0).save(
        data / "blobs" / "c.gif", save_all=True, append_images=[_stripes(4)], duration=100
    )
    animated = store.add(_draft("blobs/c.gif"))

    try:
        result = await backfill_percepts(store, data)
        assert result == {"filled": 2, "skipped": 1}

        filled = store.get(normal)
        assert filled is not None and filled.percept_hash, "补出来的指纹必须真的落库"
        anim = store.get(animated)
        assert anim is not None and ":" in anim.percept_hash, "动图要补三帧复合指纹"
        lost = store.get(missing)
        assert lost is not None and not lost.percept_hash

        # 再跑一次：已补的不会再出现，补不出来的也不会死循环
        again = await backfill_percepts(store, data)
        assert again == {"filled": 0, "skipped": 1}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_backfill_gets_past_a_block_of_uncomputable_rows(tmp_path):
    """头部一批永久算不出的条目不能挡住后面的条目。

    批次按 `LIMIT` 从头取时，这 40 条（文件缺失）会永远占住窗口：补一次和补一百次
    都是「扫了 40 条、跳过 40 条」，后面的正常条目永远没有指纹，而面板只会显示
    「跳过 40」——看起来像正常跳过。
    """
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    data = tmp_path / "data"
    (data / "blobs").mkdir(parents=True)

    for index in range(45):  # 超过一批 40 条，确保会跨批
        store.add(_draft(f"blobs/lost{index}.png"))
    good: list[int] = []
    for index in range(5):
        _stripes(index).save(data / "blobs" / f"ok{index}.png")
        good.append(store.add(_draft(f"blobs/ok{index}.png")))

    try:
        result = await backfill_percepts(store, data, limit=600)
        assert result["skipped"] == 45
        assert result["filled"] == 5, "游标没推进时这 5 条永远补不到"
        for memo_id in good:
            memo = store.get(memo_id)
            assert memo is not None and memo.percept_hash
    finally:
        store.close()


@pytest.mark.asyncio
async def test_backfill_keeps_going_after_a_skip(tmp_path):
    """一批里有一条跳过，不能就此收工：limit 是「最多扫多少条」，不是「一批」。

    提前 break 的表现是「点了补齐、只补了 39 张」，而且再点一次还是 39 张。
    """
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    data = tmp_path / "data"
    (data / "blobs").mkdir(parents=True)

    store.add(_draft("blobs/gone.png"))  # 排在这一批里，注定跳过
    for index in range(80):
        _stripes(index).save(data / "blobs" / f"p{index}.png")
        store.add(_draft(f"blobs/p{index}.png"))

    try:
        result = await backfill_percepts(store, data, limit=600)
        assert result["skipped"] == 1
        assert result["filled"] == 80, f"只补了 {result['filled']} 张，说明遇到 skip 就停了"
    finally:
        store.close()
