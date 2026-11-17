"""重新识图：把没有描述的条目重跑一遍识别。

守的静默失效：识图失效期间入库的条目会永久停在「无描述 + 兜底类」——面板看不出内容、
检索也匹配不到；重新识图如果只更新了内存没落库、或者异常后一直显示「进行中」，
用户会以为「点了没反应」或「一直在跑」。
"""

from __future__ import annotations

import pytest
from PIL import Image

from memenest.domain import STATE_INBOX, Analysis, Draft, PageQuery
from memenest.ingest import analyze as analyze_mod
from memenest.ingest.reanalyze import Reanalyzer
from memenest.store import MemoStore

GOOD_JSON = '{"label": "laugh", "caption": "猫在笑", "text_in_image": "哈哈", "scenes": ["笑死"]}'


def _draft(rel_path: str, *, caption: str = "") -> Draft:
    return Draft(
        rel_path=rel_path,
        content_hash=rel_path.encode().hex().ljust(64, "a")[:64],
        percept_hash=None,
        analysis=Analysis(label="other" if not caption else "joy", caption=caption),
        scope="any",
        origin_umo=None,
        origin_kind="chat",
        created_at=1,
        state=STATE_INBOX,
    )


def _setup(tmp_path, *, with_file: bool = True, with_missing: bool = False):
    data = tmp_path / "data"
    (data / "blobs").mkdir(parents=True)
    if with_file:
        Image.new("RGB", (24, 24), (30, 120, 200)).save(
            data / "blobs" / "blank.png", format="PNG"
        )
    store = MemoStore(data / "memo.db")
    store.initialize()
    target = store.add(_draft("blobs/blank.png"))
    if with_missing:
        store.add(_draft("blobs/gone.png"))
    return data, store, target


@pytest.mark.asyncio
async def test_reanalyze_fills_description_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze_mod, "VISION_RETRY_BACKOFF", 0.0)
    data, store, target = _setup(tmp_path)

    async def call(prompt: str, images: list[str]) -> str:
        return GOOD_JSON

    reanalyzer = Reanalyzer(
        store=store, data_dir=data, llm_call=lambda: call, prompt="p", log=_Quiet()
    )
    try:
        reanalyzer.try_begin()
        result = await reanalyzer.run_and_release(reanalyzer.targets("empty"))
        assert result["filled"] == 1 and result["failed"] == 0
        memo = store.get(target)
        assert memo is not None
        assert memo.caption == "猫在笑", "结果必须落库，不能只改内存"
        assert memo.label == "laugh"
        assert memo.text_in_img == "哈哈"
        assert memo.scenes == ("笑死",)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_missing_file_counts_as_failed_not_retried_forever(tmp_path):
    data, store, _target = _setup(tmp_path, with_missing=True)
    calls: list[int] = []

    async def call(prompt: str, images: list[str]) -> str:
        calls.append(1)
        return GOOD_JSON

    reanalyzer = Reanalyzer(
        store=store, data_dir=data, llm_call=lambda: call, prompt="p", log=_Quiet()
    )
    try:
        targets = reanalyzer.targets("empty")
        assert len(targets) == 2
        await reanalyzer.run_and_release(targets)
        assert reanalyzer.state.filled == 1
        assert reanalyzer.state.failed == 1, "文件缺失要计入失败，且不再调模型"
        assert len(calls) == 1, "只有文件存在的那条才该调模型"
    finally:
        store.close()


class _Quiet:
    def debug(self, *args, **kwargs) -> None:
        pass

    info = warning = error = debug
