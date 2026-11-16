"""发送历史：持久化、筛选、淘汰。

守的静默失效：发过的记录没落库（面板「最近发送」重启就空，排查无从下手）；
淘汰规则写反把最新记录删掉；按天数与按条数两条约束只有一条生效。
"""

from __future__ import annotations

from memenest.store import SEND_LOG_DAYS, SEND_LOG_MAX, MemoStore


def _store(tmp_path) -> MemoStore:
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    return store


def _add(store: MemoStore, *, at: int, via: str = "vector", text: str = "", memo_id=1):
    store.add_send_log(
        created_at=at,
        umo="group_demo",
        memo_id=memo_id,
        via=via,
        label="joy",
        text=text,
        outcome="sent",
    )


def test_history_is_newest_first_and_filterable(tmp_path):
    store = _store(tmp_path)
    try:
        _add(store, at=100, via="vector", text="第一条")
        _add(store, at=200, via="keyword", text="第二条")
        _add(store, at=300, via="tool", text="第三条")

        items, total = store.page_send_logs()
        assert total == 3
        assert [item["created_at"] for item in items] == [300, 200, 100], "新的在前"

        only_tool, tool_total = store.page_send_logs(via="tool")
        assert tool_total == 1 and only_tool[0]["via"] == "tool"

        hits, hit_total = store.page_send_logs(query="第二")
        assert hit_total == 1 and hits[0]["text"] == "第二条"
    finally:
        store.close()


def test_expired_rows_are_pruned_but_recent_kept(tmp_path):
    store = _store(tmp_path)
    now = 1_700_000_000
    try:
        _add(store, at=now - (SEND_LOG_DAYS + 1) * 86400, text="太老")
        _add(store, at=now - 3600, text="刚发")
        removed = store.prune_send_logs(now)
        assert removed == 1, "只删超期那条"
        items, total = store.page_send_logs()
        assert total == 1 and items[0]["text"] == "刚发"
    finally:
        store.close()


def test_overflow_prunes_oldest_keeps_newest(tmp_path):
    store = _store(tmp_path)
    try:
        # 写两条，再把上限临时压到 1：留下来的必须是最新那条
        _add(store, at=100, text="旧")
        _add(store, at=200, text="新")
        removed = store.prune_send_logs(200, days=SEND_LOG_DAYS, keep=1)
        assert removed == 1
        items, total = store.page_send_logs()
        assert total == 1 and items[0]["text"] == "新"
        assert SEND_LOG_MAX > 1000, "条数上限要足够覆盖日常量"
    finally:
        store.close()
