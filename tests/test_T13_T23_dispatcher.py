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


def _dispatcher(store, data_dir, delivered: list, **overrides) -> Dispatcher:
    base = {
        "send_ratio": 1.0,
        "send_delay_seconds": 0,
        "send_cooldown_seconds": 0,
        "send_enabled": True,
    }
    base.update(overrides)
    settings = Settings(**base)

    async def deliver(umo: str, path) -> bool:
        delivered.append((umo, path))
        return True

    return Dispatcher(
        store=store,
        data_dir=data_dir,
        settings=lambda: settings,
        log=LOG,
        deliver=deliver,
        rng=_ZeroRng(),
    )


@pytest.mark.asyncio
async def test_delayed_send_happens_once_then_marks_usage(tmp_path):
    """延迟发送只发一次并记用量：不记 use_count 会让淘汰算法一直挑「没用过」的图来删。"""
    store, data_dir, _ = _setup(tmp_path)
    delivered: list = []
    dispatcher = _dispatcher(store, data_dir, delivered)
    try:
        outcome = await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)
        assert outcome.scheduled is True
        assert await _wait_until(lambda: len(delivered) == 1)
        assert await _wait_until(lambda: store.page(PageQuery())[0][0].use_count == 1)
        assert store.stats().total == 1
    finally:
        await dispatcher.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_T13_new_message_does_not_cancel_the_pending_send(
    tmp_path,
):
    """延迟是纯粹的等待：别人再说话也不打断已排期的发送。"""
    store, data_dir, _ = _setup(tmp_path)
    delivered: list = []
    dispatcher = _dispatcher(store, data_dir, delivered, send_delay_seconds=1)
    try:
        result = await dispatcher.fire(
            umo=UMO, text=LONG_TEXT, reply_has_image=False
        )
        assert result.scheduled is True

        # 同一会话紧接着又来消息：只重置回合状态，不打断已排期的发送
        dispatcher.note_incoming(UMO)

        # 等条件成立而不是睡固定时长——固定 sleep 在慢机器上会偶发挂
        assert await _wait_until(lambda: len(delivered) == 1, timeout=3.0), (
            "排期的发送应该照常落地"
        )
    finally:
        await dispatcher.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_T14_T16_two_hook_calls_fire_only_once(tmp_path):
    """两个 hook 都触发时每回合只发一张：重复投递在群里看起来像手滑，很难归因。"""
    store, data_dir, _ = _setup(tmp_path)
    delivered: list = []
    dispatcher = _dispatcher(store, data_dir, delivered)
    try:
        first = await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)
        second = await dispatcher.fire(umo=UMO, text=LONG_TEXT, reply_has_image=False)
        assert first.scheduled is True
        assert second.code == Reason.TURN_USED
        assert second.scheduled is False
        await asyncio.sleep(0.1)
        assert len(delivered) == 1
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
