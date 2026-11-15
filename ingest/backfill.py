"""感知指纹回填：只补库里缺指纹的条目。

指纹为空的条目在近似去重里等于不存在（只能靠字节哈希判重）；补一次之后
「同图不同压缩」才重新判得出来。动图补的是三帧复合指纹（抽帧或某帧算不出
梯度时退化为无指纹）。

两个必须守住的点：
1. **游标推进**：批次按 `id > after_id` 往后走。只按 `LIMIT` 从头取的话，
   头部一批补不出来的条目会永远占住窗口，后面的条目永远扫不到。
2. **不能因为「有跳过」就提前结束**：skipped 只是这一条算不出来，跟「后面还有
   没有条目」无关；提前 break 会让 limit 形同虚设。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .fingerprints import animated_percept_hash, percept_hash

logger = logging.getLogger(__name__)

BATCH_SIZE = 40
"""每批取多少条；回填量的上限由调用方的 limit 控制。"""


def percept_for_file(path: Path) -> str | None:
    """读磁盘图片算感知指纹；动图算三帧复合，读不出来、文件缺失都返回 None。"""
    try:
        from PIL import Image

        with Image.open(path) as image:
            if int(getattr(image, "n_frames", 1)) > 1:
                return animated_percept_hash(image)
            return percept_hash(image)
    except Exception as exc:
        logger.debug("回填感知指纹失败 path=%s type=%s", path.name, type(exc).__name__)
        return None


async def backfill_percepts(
    store: Any, data_dir: Path, *, limit: int = 600, batch_size: int = BATCH_SIZE
) -> dict[str, int]:
    """按游标补齐缺失的感知指纹，返回 {filled, skipped}。

    limit 是本次最多扫描多少条：大库不该因为一次「补齐」卡住整条流水线。
    """
    filled = 0
    skipped = 0
    scanned = 0
    cursor = 0
    data_dir = Path(data_dir)
    batch_size = max(1, int(batch_size))
    while scanned < limit:
        rows = await asyncio.to_thread(
            store.memos_missing_percept, batch_size, cursor
        )
        if not rows:
            # 游标之后确实没有缺指纹的条目了；这才是唯一可以结束的条件
            break
        cursor = rows[-1][0]
        dirty: list[tuple[int, str]] = []
        for memo_id, rel_path in rows:
            scanned += 1
            value = await asyncio.to_thread(percept_for_file, data_dir / rel_path)
            if value is None:
                skipped += 1
                continue
            dirty.append((memo_id, value))
        if dirty:
            await asyncio.to_thread(store.set_percept_hashes, dirty)
            filled += len(dirty)
    return {"filled": filled, "skipped": skipped}


__all__ = ["BATCH_SIZE", "backfill_percepts", "percept_for_file"]
