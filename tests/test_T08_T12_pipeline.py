"""T-08 / T-12 / T-05 / I-3 / I-4 / I-6：流水线的失败与回滚。

守的静默失效：模型失败却把条目留在库里（I-6 失守，审核池被垃圾污染）、
写库失败后文件残留成孤儿（I-4 失守，磁盘慢慢涨）、队列满却阻塞消息处理。
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from PIL import Image

from memenest.config import Settings
from memenest.domain import MediaRef
from memenest.ingest.pipeline import IngestContext, IngestPipeline, safe_rel_path
from memenest.store import MemoStore, StoreError

LOG = logging.getLogger("memenest-tests")
UMO = "aiocqhttp:GroupMessage:1"


def _write_png(path, color=(40, 120, 200)) -> None:
    Image.new("RGB", (48, 48), color).save(path, format="PNG")


def _ref(path) -> MediaRef:
    return MediaRef(
        source=str(path),
        declared_ext=".png",
        origin_kind="chat",
        platform="aiocqhttp",
        reason="sticker_subtype",
    )


def _settings(**overrides) -> Settings:
    base = {"send_delay_seconds": 0}
    base.update(overrides)
    return Settings(**base)


def _build(tmp_path, settings: Settings, llm_call):
    data_dir = tmp_path / "data"
    store = MemoStore(data_dir / "memo.db")
    store.initialize()
    pipeline = IngestPipeline(
        store=store,
        data_dir=data_dir,
        settings=lambda: settings,
        llm_call=(lambda: llm_call) if llm_call is not None else (lambda: None),
        prompt="p",
        log=LOG,
    )
    return pipeline, store, data_dir


def _files_under(root) -> list[str]:
    if not root.exists():
        return []
    return [path.name for path in root.rglob("*") if path.is_file()]
@pytest.mark.asyncio
async def test_T08_model_failure_leaves_no_row_and_no_file(tmp_path):
    """模型失败（这里用抛异常代表）不留行、不留文件。

    超时现在交给 provider 自己的 timeout，插件不再单独设，所以这里改用异常路径。
    """
    settings = _settings()
    source = tmp_path / "two.png"
    _write_png(source, (200, 10, 10))

    async def slow(prompt: str, images: list[str]) -> str:
        raise RuntimeError("model down")

    pipeline, store, data_dir = _build(tmp_path, settings, slow)
    await pipeline.start()
    try:
        pipeline.submit([_ref(source)], IngestContext(umo=UMO))
        await asyncio.wait_for(pipeline.wait_idle(), timeout=15)
        assert store.stats().total == 0
        assert _files_under(data_dir / "tmp") == []
        assert _files_under(data_dir / "inbox") == []
        assert _files_under(data_dir / "blobs") == []
    finally:
        await pipeline.stop()
        store.close()


@pytest.mark.asyncio
async def test_T12_store_failure_removes_the_moved_file(tmp_path, monkeypatch):
    """写库失败要删掉已经移动的文件：否则每失败一次就留下一个没人引用的孤儿文件。"""
    settings = _settings()
    source = tmp_path / "three.png"
    _write_png(source, (10, 200, 10))

    async def llm(prompt: str, images: list[str]) -> str:
        return '{"label": "laugh", "caption": "笑"}'

    pipeline, store, data_dir = _build(tmp_path, settings, llm)
    await pipeline.start()

    def boom(draft):
        raise StoreError("simulated disk failure")

    monkeypatch.setattr(store, "add", boom)
    try:
        pipeline.submit([_ref(source)], IngestContext(umo=UMO))
        await asyncio.wait_for(pipeline.wait_idle(), timeout=10)
        assert store.stats().total == 0
        # 条目既没有半行记录，也没有留下孤儿文件。
        assert _files_under(data_dir / "inbox") == []
        assert _files_under(data_dir / "tmp") == []
    finally:
        await pipeline.stop()
        store.close()
def test_rel_path_guard_rejects_escapes():
    """入库路径守卫：库里的每一条 rel_path 都必须落在数据目录内。"""
    assert safe_rel_path("inbox/20260101_abababab.png") is True
    assert safe_rel_path("blobs/202601/x.jpg") is True
    assert safe_rel_path("../etc/passwd") is False
    assert safe_rel_path("inbox/../../x.png") is False
    assert safe_rel_path("/abs.png") is False
    assert safe_rel_path("C:/x.png") is False
    assert safe_rel_path("inbox\\x.png") is False
    assert safe_rel_path("") is False
@pytest.mark.asyncio
async def test_capacity_eviction_deletes_the_files_too(tmp_path):
    """只删行不删文件 = 孤儿文件无限增长，只有磁盘会告诉你。"""
    settings = _settings(library_cap=10)
    pipeline, store, data_dir = _build(tmp_path, settings, None)
    blobs = data_dir / "blobs" / "202601"
    blobs.mkdir(parents=True, exist_ok=True)
    from memenest.domain import STATE_READY, Analysis, Draft

    for index in (1, 2):
        rel = f"blobs/202601/{index}_aaaaaaaa.png"
        target = data_dir / rel
        target.write_bytes(b"png")
        store.add(
            Draft(
                rel_path=rel,
                content_hash=f"{index:064x}",
                percept_hash=None,
                analysis=Analysis(label="joy"),
                scope="any",
                origin_umo=None,
                origin_kind="chat",
                created_at=index,
                state=STATE_READY,
            )
        )
    # 把上限压到 1，触发一次淘汰
    tight = _settings(library_cap=1)
    pipeline._settings = lambda: tight
    removed = await pipeline.enforce_capacity()
    assert removed == 1
    remaining = [p for p in blobs.iterdir() if p.is_file()]
    assert len(remaining) == 1
    assert store.stats().total == 1
    store.close()
