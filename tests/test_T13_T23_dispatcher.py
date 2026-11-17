"""T-13 / T-14 / T-15 / T-16 / T-23 / I-2 / I-5：发送编排。

守的静默失效：延迟期间来了新消息仍然补发表情（打断对话）、
同一回合被两处 hook 各发一次（刷屏）、停机后仍有任务落地（重载幽灵发图）。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from memenest.config import Settings
from memenest.domain import STATE_READY, Analysis, Draft, PageQuery, Reason
from memenest.publish.dispatcher import Dispatcher
from memenest.store import MemoStore

LOG = logging.getLogger("memenest-tests")
UMO = "aiocqhttp:GroupMessage:1"
LONG_TEXT = "今天这场雨下得也太大了，出门就湿透了"


class _ZeroRng:
    def random(self) -> float:
        return 0.0


def _setup(tmp_path) -> tuple[MemoStore, object, str]:
    data_dir = tmp_path / "data"
    store = MemoStore(data_dir / "memo.db")
    store.initialize()
    rel_path = "blobs/202601/1_aaaaaaaa.png"
    target = data_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"png")
    store.add(
        Draft(
            rel_path=rel_path,
            content_hash="a" * 64,
            percept_hash=None,
            analysis=Analysis(label="joy", caption="猫在笑"),
            scope="any",
            origin_umo=UMO,
            origin_kind="chat",
            created_at=1,
            state=STATE_READY,
        )
    )
    return store, data_dir, rel_path


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """轮询等待条件成立。

    发送链路里 `_mark_used` 走 asyncio.to_thread，固定 sleep 0.1s 在慢机器上会偶发失败
    （实测 5 次挂 2 次），这里改成等条件本身。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _dispatcher(store, data_dir, delivered: list, *, deliver_fn=None, **overrides) -> Dispatcher:
    base = {
        "send_ratio": 1.0,
        "send_delay_seconds": 0,
        "send_cooldown_seconds": 0,
        "send_enabled": True,
        # 这些用例测的是发送时序（延迟、去重、停机），不是「选哪张最相关」。
        # min_keyword_score 默认 2.0 会挡掉这里构造的无关文本，显式关掉。
        "min_keyword_score": 0.0,
    }
    base.update(overrides)
    settings = Settings(**base)

    async def deliver(umo: str, path) -> bool:
        delivered.append((umo, path))
        return True

    if deliver_fn is not None:
        deliver = deliver_fn

    return Dispatcher(
        store=store,
        data_dir=data_dir,
        settings=lambda: settings,
        log=LOG,
        deliver=deliver,
        rng=_ZeroRng(),
    )


@pytest.mark.asyncio
async def test_send_is_recorded_to_history(tmp_path):
    """守住「发了但没落库」：只在内存里的话，重载插件后排查就没了依据。"""
    store, data_dir, _ = _setup(tmp_path)
    delivered: list = []
    dispatcher = _dispatcher(store, data_dir, delivered)
    try:
        await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)
        assert await _wait_until(lambda: bool(delivered), timeout=2.0) is True
        assert await _wait_until(
            lambda: store.page_send_logs()[1] == 1, timeout=2.0
        ) is True, "发送历史必须落库"
        items, total = store.page_send_logs()
        assert total == 1
        assert items[0]["outcome"] == "sent"
        assert items[0]["via"] in ("vector", "keyword")
        # 标签必须是这张图真正的分类（写成固定的 "other" 就等于没记）
        memo = store.get(items[0]["memo_id"])
        assert memo is not None
        assert items[0]["label"] == memo.label == "joy"
    finally:
        await dispatcher.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_failed_send_records_error_type(tmp_path):
    """历史里的失败必须带原因（异常类型名）：只写「失败」两个字等于没记。"""
    store, data_dir, _ = _setup(tmp_path)

    async def failing_deliver(umo: str, path):
        return False, "TimeoutError"

    dispatcher = _dispatcher(store, data_dir, [], deliver_fn=failing_deliver)
    try:
        await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)
        assert await _wait_until(
            lambda: store.page_send_logs()[1] == 1, timeout=2.0
        ) is True, "失败的发送也要进历史"
        items, _ = store.page_send_logs()
        assert items[0]["outcome"] == "failed"
        assert items[0]["detail"] == "TimeoutError", "失败原因要落库，否则排查只能翻日志"
    finally:
        await dispatcher.shutdown()
        store.close()

@pytest.mark.asyncio
async def test_T23_shutdown_cancels_pending_and_refuses_new_work(tmp_path):
    """停机要取消待发并拒绝新任务：漏取消会在进程退出时留下悬空发送。"""
    store, data_dir, _ = _setup(tmp_path)
    delivered: list = []
    dispatcher = _dispatcher(store, data_dir, delivered, send_delay_seconds=1)
    try:
        assert (await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)).scheduled
        await dispatcher.shutdown()
        # 必须等过延迟期：只等 0.1s 的话，「没取消」也会显示成没发，断言等于没测
        assert await _wait_until(lambda: bool(delivered), timeout=1.5) is False, (
            "停机之后不该再发出任何东西"
        )
        after = await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)
        assert after.scheduled is False
        assert after.code == Reason.SWITCH_OFF
    finally:
        store.close()


@pytest.mark.asyncio
async def test_whitelist_blocks_sessions_outside_it(tmp_path):
    """白名单外的会话一律不发：判定漏了会把表情发到用户没授权的群里。"""
    store, data_dir, _ = _setup(tmp_path)
    delivered: list = []
    dispatcher = _dispatcher(store, data_dir, delivered, send_whitelist=("G:999",))
    try:
        outcome = await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)
        assert outcome.code == Reason.SESSION_DENIED
        assert delivered == []
    finally:
        await dispatcher.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_delayed_landing_marks_its_own_turn_not_the_new_one(tmp_path):
    """延迟落地只标记「发起它的那个回合」，且不因新消息而取消。

    落地时若去取「当前回合」，延迟窗口内进来的那条消息的回合会被提前判成已用过，
    它自己的回复就静默少一张图——不报错，日志里也看不出来。

    这里直接驱动 `_deliver_later` 而不是靠真实 sleep 让两条消息交错：靠时序的写法
    在慢机器上会偶发通过（机器一堵，落地与第二条消息的先后就变了），
    变异验证也因此时好时坏。
    """
    store, data_dir, _ = _setup(tmp_path)
    delivered: list = []
    dispatcher = _dispatcher(store, data_dir, delivered)
    try:
        memo = store.page(PageQuery())[0][0]
        first_turn = dispatcher._state(UMO)
        # 第一回合已经排了一张（真实流程里 state.used 在这时被置上）
        first_turn.used = True
        # 延迟窗口里用户又发了一条：新回合顶上来，且必须是另一个对象
        dispatcher.note_incoming(UMO)
        new_turn = dispatcher._state(UMO)
        assert new_turn is not first_turn, "前置条件：新消息要换一份回合状态"

        await dispatcher._deliver_later(UMO, memo, 0.0, first_turn)

        assert len(delivered) == 1, "新消息不该打断已排期的发送"
        assert new_turn.used is False, "新回合被提前判成已用过，它自己的配图就没了"
        assert store.page(PageQuery())[0][0].use_count == 1, "用量要记在落地的那张上"
    finally:
        await dispatcher.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_preview_ranks_without_touching_delivery_state(tmp_path):
    """「试一试」只是看一眼：排序可用，但不能占回合、不能记用量。

    预览要是顺手把 state.used 置上，用户点一下「试一试」就把这条消息的配图机会
    用掉了——下一个回复真该发图时反而一张都发不出来。
    """
    store, data_dir, _ = _setup(tmp_path)
    delivered: list = []
    dispatcher = _dispatcher(store, data_dir, delivered)
    try:
        result = await dispatcher.preview(LONG_TEXT)
        assert [row["id"] for row in result["keyword"]], "关键词路应该给出候选"
        # 没配 embedding：向量路明确回 None（面板据此显示「未启用」而不是「没有候选」）
        assert result["vector"] is None
        assert delivered == [], "预览不能真的发出去"
        assert dispatcher.stats.sent == 0
        # 预览之后自动发送照常可用（回合没有被提前用掉）
        outcome = await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)
        assert outcome.code == Reason.OK
        assert await _wait_until(lambda: len(delivered) == 1, timeout=2.0)
    finally:
        await dispatcher.shutdown()
        store.close()
