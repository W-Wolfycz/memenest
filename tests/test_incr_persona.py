"""增量需求：人格归属——一条表情只给某个人格用。

守的静默失效：人格绑定写着生效但发送时没过滤（别的会话照样摸到这张图），
或者解析不到当前人格时放行（越界发送）。
"""

from __future__ import annotations

import logging

import pytest

from memenest.config import Settings
from memenest.domain import (
    STATE_READY,
    Analysis,
    CandidateQuery,
    Draft,
    Reason,
)
from memenest.publish.dispatcher import Dispatcher
from memenest.publish.pick import persona_allows
from memenest.store import MemoStore
from _fixtures import make_memo

LOG = logging.getLogger("memenest-tests")
UMO = "aiocqhttp:GroupMessage:1"


@pytest.fixture()
def store(tmp_path):
    instance = MemoStore(tmp_path / "memo.db")
    instance.initialize()
    yield instance
    instance.close()


def _draft(rel_path: str, persona_ids: tuple[str, ...] = ()) -> Draft:
    return Draft(
        rel_path=rel_path,
        content_hash=rel_path.encode().hex().ljust(64, "a")[:64],
        percept_hash=None,
        analysis=Analysis(label="joy", caption=rel_path),
        scope="any",
        origin_umo=UMO,
        origin_kind="chat",
        created_at=1,
        state=STATE_READY,
        persona_ids=persona_ids,
    )


def test_unknown_persona_fails_closed(store):
    """取不到当前人格时只发通用：fail-open 会让绑了人格的表情漏给所有人格。"""
    store.add(_draft("inbox/generic.png"))
    store.add(_draft("inbox/for_p1.png", ("p1",)))
    only_generic = {
        memo.rel_path for memo in store.candidates(CandidateQuery(persona_id=None))
    }
    assert only_generic == {"inbox/generic.png"}

    # 禁用优先于通用：同一条 SQL 子句，别让「禁用了照样发」漏过去
    blocked = store.add(_draft("inbox/blocked.png", ("p1",)))
    if blocked is not None:
        store.set_persona_blocks(blocked, ("p1",))
    for_p1 = {memo.rel_path for memo in store.candidates(CandidateQuery(persona_id="p1"))}
    assert "inbox/generic.png" in for_p1
    assert "inbox/blocked.png" not in for_p1
def test_persona_allows_matrix():
    """人格归属判定矩阵：全空必须是「谁都不用」，禁用优先于可用。

    前者的退化方式是「悄悄变成通用」，后者是「禁用了照样发」——两种都只表现成
    「这张图又冒出来了」，不比对看不出来。
    """
    generic = make_memo(1)
    bound = make_memo(2, persona_ids=("p1",))
    assert persona_allows(generic, None) is True
    assert persona_allows(generic, "p1") is True
    assert persona_allows(bound, "p1") is True
    assert persona_allows(bound, "p2") is False
    assert persona_allows(bound, None) is False
    # 全空 = 谁都不用，不能退化成通用
    empty = make_memo(3, persona_ids=())
    assert persona_allows(empty, None) is False
    assert persona_allows(empty, "p1") is False
    # 命中禁用就不发：通用也挡得住，绑定了同一人格也挡得住
    blocked = make_memo(4, blocked_persona_ids=("p1",))
    assert persona_allows(blocked, "p1") is False
    assert persona_allows(blocked, "p2") is True
    blocked_bound = make_memo(5, persona_ids=("p1",), blocked_persona_ids=("p1",))
    assert persona_allows(blocked_bound, "p1") is False
@pytest.mark.asyncio
async def test_manual_send_rejects_foreign_persona(tmp_path):
    """手动发送也要过人格校验：绕过它就等于给了工具一条越界通道。"""
    data_dir = tmp_path / "data"
    store = MemoStore(data_dir / "memo.db")
    store.initialize()
    rel_path = "blobs/202601/1_aaaaaaaa.png"
    target = data_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"png")
    memo_id = store.add(_draft(rel_path, "someone_else"))
    delivered: list = []

    async def deliver(umo: str, path) -> bool:
        delivered.append(path)
        return True

    dispatcher = Dispatcher(
        store=store,
        data_dir=data_dir,
        settings=lambda: Settings(send_compress=False, persona_isolation=True),
        log=LOG,
        deliver=deliver,
        persona_resolver=lambda umo: "p1",
    )
    try:
        memo = store.get(memo_id)
        assert memo is not None
        outcome = await dispatcher.fire_manual(umo=UMO, memo=memo)
        assert outcome.code == Reason.PERSONA_DENIED
        assert delivered == []
    finally:
        await dispatcher.shutdown()
        store.close()


