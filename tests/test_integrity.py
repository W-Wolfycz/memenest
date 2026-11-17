"""完整性体检：库与磁盘对账。

守的静默失效：库里有记录但文件没了（面板点开是空白、发送时报「文件缺失」），
磁盘上有文件但库里没引用（只占空间，谁也看不见）；两种都要能在面板上一眼看出来。
"""

from __future__ import annotations

from memenest.domain import STATE_READY, Analysis, Draft, PageQuery
from memenest.integrity import remove_missing, remove_orphans, scan, vacuum
from memenest.store import MemoStore


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


def _setup(tmp_path):
    data = tmp_path / "data"
    (data / "blobs").mkdir(parents=True)
    (data / "blobs" / "kept.png").write_bytes(b"x")
    (data / "blobs" / "orphan.png").write_bytes(b"yy")
    store = MemoStore(data / "memo.db")
    store.initialize()
    kept = store.add(_draft("blobs/kept.png"))
    missing = store.add(_draft("blobs/missing.png"))
    return data, store, kept, missing


def test_scan_reports_missing_and_orphans(tmp_path):
    data, store, _kept, _missing = _setup(tmp_path)
    try:
        report = scan(data, store)
        assert report["rows"] == 2
        assert report["missing_count"] == 1
        assert report["missing_sample"][0]["rel_path"] == "blobs/missing.png"
        assert report["orphan_count"] == 1
        assert report["orphan_sample"] == ["blobs/orphan.png"]
        assert report["dirs"]["blobs"]["files"] == 2
        assert report["total_bytes"] > 0
    finally:
        store.close()


def test_remove_orphans_keeps_referenced_files(tmp_path):
    """只删没人引用的：把有记录的图删掉等于无声丢数据。"""
    data, store, kept, _missing = _setup(tmp_path)
    try:
        result = remove_orphans(data, store)
        assert result == {"scanned": 1, "removed": 1, "failed": 0}
        assert (data / "blobs" / "kept.png").is_file()
        assert not (data / "blobs" / "orphan.png").exists()
        # 库里的行一条都不能少
        rows, total = store.page(PageQuery())
        assert total == 2 and len(rows) == 2
        assert store.get(kept) is not None
    finally:
        store.close()


def test_remove_missing_deletes_only_broken_rows(tmp_path):
    """只删「记录在、文件没了」的：删多了等于无声丢图。"""
    data, store, kept, missing = _setup(tmp_path)
    try:
        result = remove_missing(data, store)
        assert result == {"scanned": 1, "removed": 1}
        assert store.get(missing) is None
        assert store.get(kept) is not None, "文件还在的条目必须留着"
        rows, total = store.page(PageQuery())
        assert total == 1 and len(rows) == 1
    finally:
        store.close()


