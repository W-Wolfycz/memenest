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
        assert reanalyzer.try_begin() is True
        assert reanalyzer.try_begin() is False, "同一时刻只允许一个任务"
        targets = reanalyzer.targets("empty")
        assert targets == [target]
        result = await reanalyzer.run_and_release(targets)

        assert result["filled"] == 1 and result["failed"] == 0
        memo = store.get(target)
        assert memo is not None
        assert memo.caption == "猫在笑", "结果必须落库，不能只改内存"
        assert memo.label == "laugh"
        assert memo.text_in_img == "哈哈"
        assert memo.scenes == ("笑死",)
        assert reanalyzer.state.running is False
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


@pytest.mark.asyncio
async def test_no_model_short_circuits_with_message(tmp_path):
    data, store, _target = _setup(tmp_path)
    reanalyzer = Reanalyzer(
        store=store, data_dir=data, llm_call=lambda: None, prompt="p", log=_Quiet()
    )
    try:
        result = await reanalyzer.run_and_release(reanalyzer.targets("empty"))
        assert result["ok"] is False
        assert result["reason"] == "no_model"
        assert reanalyzer.state.running is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_write_failure_still_releases_running_flag(tmp_path, monkeypatch):
    """落库失败也不能把 running 卡在 True——面板会永远显示「进行中」。"""
    monkeypatch.setattr(analyze_mod, "VISION_RETRY_BACKOFF", 0.0)
    data, store, target = _setup(tmp_path)

    async def call(prompt: str, images: list[str]) -> str:
        return GOOD_JSON

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "update_analysis", boom)
    reanalyzer = Reanalyzer(
        store=store, data_dir=data, llm_call=lambda: call, prompt="p", log=_Quiet()
    )
    try:
        # 必须真的占过位：否则 running 本来就是 False，测不出「没释放」
        assert reanalyzer.try_begin() is True
        result = await reanalyzer.run_and_release([target])
        assert result["ok"] is False
        assert reanalyzer.state.running is False
        rows, _ = store.page(PageQuery())
        assert rows and rows[0].caption == "", "失败不能留下半截改动"
    finally:
        store.close()


class _Quiet:
    def debug(self, *args, **kwargs) -> None:
        pass

    info = warning = error = debug
