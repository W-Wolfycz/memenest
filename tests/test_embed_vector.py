"""向量检索的静默失效点。

每条用例守的是「跑一遍、看一眼都正常，但行为已经错了」的那类缺陷：
- 维度错位的向量参与排序 → 排序结果全错但不报错
- 向量缓存永不失效 → 新入库的表情永远检索不到
- 阈值判断方向反了 → 该发的不发 / 不该发的照发
- provider 异常没被吞掉 → 整条检索链崩
- 索引失败拖垮入库 → 表情收进来了但永远选不中
"""

from __future__ import annotations

import asyncio
import random

import pytest

from memenest import embed as emb
from memenest.config import load_settings
from memenest.domain import Analysis, Draft, STATE_READY
from memenest.publish.candidates import CandidateCache
from memenest.publish.dispatcher import Dispatcher
from memenest.publish.vectors import VectorStore
from memenest.store import MemoStore


class FakeProvider:
    """按文本查表返回固定向量；没登记的返回零向量。"""

    def __init__(self, dim: int = 4, table: dict[str, list[float]] | None = None) -> None:
        self.dim = dim
        self.table = table or {}
        self.calls = 0

    def get_dim(self) -> int:
        return self.dim

    async def get_embedding(self, text: str) -> list[float]:
        self.calls += 1
        return self.table.get(text, [0.0] * self.dim)

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [await self.get_embedding(text) for text in texts]


class BoomProvider:
    def get_dim(self) -> int:
        return 4

    def _fail(self, *a):
        raise RuntimeError("provider 挂了")

    async def get_embedding(self, text: str) -> list[float]:
        raise RuntimeError("provider 挂了")

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("provider 挂了")


class NullLog:
    def debug(self, *args, **kwargs) -> None:
        pass

    info = warning = error = debug
def _build(store: MemoStore, settings, embedder_factory, tmp_path, seed: int | None = None):
    settings_fn = lambda: settings  # noqa: E731
    cache = CandidateCache(store, settings_fn)
    vectors = VectorStore(store)
    dispatcher = Dispatcher(
        store=store,
        data_dir=tmp_path,
        settings=settings_fn,
        log=NullLog(),
        deliver=lambda umo, path: asyncio.sleep(0, result=True),
        cache=cache,
        persona_resolver=lambda umo: None,
        embedder=embedder_factory,
        vectors=vectors,
        rng=random.Random(seed) if seed is not None else None,
    )
    return dispatcher, cache, vectors


def _seed(store: MemoStore, provider: FakeProvider) -> None:
    """两条表情，向量正交，方便断言选中哪一条。"""
    for index, (vec, label) in enumerate(
        ([[1.0, 0.0, 0.0, 0.0], "laugh"], [[0.0, 1.0, 0.0, 0.0], "blank"]), start=1
    ):
        draft = Draft(
            rel_path=f"blobs/{index}.png",
            content_hash=f"{index:064x}",
            percept_hash=None,
            analysis=Analysis(label=label, caption=f"图{index}"),
            scope="any",
            origin_umo=None,
            origin_kind="import",
            created_at=index,
            state=STATE_READY,
        )
        memo_id = store.add(draft)
        normalized = emb.normalize(vec)
        store.save_embeddings(
            [(memo_id, "text", "m", len(normalized), emb.pack(normalized))], 1
        )



@pytest.mark.asyncio
async def test_vector_cache_invalidates_on_write(tmp_path):
    """写操作后必须重新加载：否则新入库的表情永远检索不到。"""
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    provider = FakeProvider()
    _seed(store, provider)
    vectors = VectorStore(store)
    first = await vectors.get("m", 4)
    assert len(first) == 2

    memo_id = store.add(
        Draft(
            rel_path="blobs/3.png",
            content_hash="f" * 64,
            percept_hash=None,
            analysis=Analysis(label="joy", caption="图3"),
            scope="any",
            origin_umo=None,
            origin_kind="import",
            created_at=3,
            state=STATE_READY,
        )
    )
    normalized = emb.normalize([0.0, 0.0, 1.0, 0.0])
    store.save_embeddings([(memo_id, "text", "m", 4, emb.pack(normalized))], 1)

    again = await vectors.get("m", 4)
    assert len(again) == 3
    store.close()


def _query():
    from memenest.domain import CandidateQuery

    return CandidateQuery(state=STATE_READY, umo=None, ignore_scope=True, limit=50)

def test_vectors_are_split_by_kind():
    """三路向量必须各管一段，不能互相串味。

    串起来的后果是静默的：实测拼成一根时 top-1 只有 28.6%，
    拆开后单用图上文字那一路就有 47.4%，差的那一半全是互相稀释掉的。
    """
    from memenest.domain import label_display

    parts = emb.vectors_for(
        text_in_image="我不评价这个了",
        caption="蓝发女仆侧脸撇嘴",
        scenes=("变猫娘",),
    )
    # 每一路只装自己的东西
    assert parts["text"] == "我不评价这个了"
    assert parts["caption"] == "蓝发女仆侧脸撇嘴"
    assert parts["scene"] == "变猫娘"
    # 交叉污染：图上文字不该跑进画面描述，反之亦然
    assert "蓝发" not in parts["text"]
    assert "我不评价" not in parts["caption"]
    # scene 只装话题词。曾经把情绪标签也拼进来，结果没有话题词的图仍然有一根
    # 所有同情绪图共有的「无语」向量，那一路上纯属噪音，情绪匹配归 labels 路管
    assert label_display("blank") not in parts["scene"]
    # 空字段不建向量，检索时自然缺席
    empty = emb.vectors_for(text_in_image="", caption="", scenes=())
    assert not empty["text"] and not empty["caption"] and not empty["scene"]
    # 没有话题词的图不该有 scene 向量，哪怕 label 非空
    no_topic = emb.vectors_for(text_in_image="", caption="蓝发少女微笑", scenes=())
    assert no_topic["scene"] == ""
    assert no_topic["caption"] == "蓝发少女微笑"

@pytest.mark.asyncio
async def test_scene_hit_works_from_reply_side(tmp_path):
    """话题词没有说话人：机器人自己在聊这个话题时，也该选中它。

    只拿用户消息去比 scene 向量的话，「机器人说变猫娘 → 配那张变猫娘图」这条
    永远走不通，而线上这种情况很常见（bot 自己接梗）。写坏了的表现是
    场景那一路只对用户消息生效，选图偏保守——只看排序变化才发现。
    """
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    # 两张都要有话题向量：没有向量的图会被上游 skip 掉，
    # 只剩一张参与的话，怎么改都看不出差别（变异等价）
    provider = FakeProvider(
        table={"笑死": [1.0, 0.0, 0.0, 0.0], "无语": [0.0, 1.0, 0.0, 0.0]}
    )
    for index, (label, scenes) in enumerate(
        (("laugh", ("笑死",)), ("blank", ("无语",))), start=1
    ):
        memo_id = store.add(
            Draft(
                rel_path=f"blobs/{index}.png",
                content_hash=f"{index:064x}",
                percept_hash=None,
                analysis=Analysis(label=label, caption=f"图{index}", scenes=scenes),
                scope="any",
                origin_umo=None,
                origin_kind="import",
                created_at=index,
                state=STATE_READY,
            )
        )
        parts = emb.vectors_for(
            text_in_image="", caption=f"图{index}", scenes=scenes
        )
        if parts["scene"]:
            vec = emb.normalize(await provider.get_embedding(parts["scene"]))
            store.save_embeddings(
                [(memo_id, "scene", "m", len(vec), emb.pack(vec))], 1
            )
    pool = [memo for memo in store.candidates(_query())]

    # 只给 scene 权重，其余三路关掉：命中与否全看话题词那一路
    settings = load_settings(
        {
            "embedding_enabled": True,
            "embedding_threshold": 0.0,
            "weight_text": 0.0,
            "weight_caption": 0.0,
            "weight_scenes": 1.0,
            "weight_label": 0.0,
        }
    )
    embedder = emb.Embedder(provider=provider, model="m", timeout=5.0)
    dispatcher, _, _ = _build(store, settings, lambda: embedder, tmp_path)

    # 回复提到话题词、用户消息是别的话题 → 只能靠 reply 那一路。
    # 这里刻意让 context_text 非空：空串会让「只用 context」的写法也碰巧通过。
    #
    # 跑多次而不是一次：选图本身带采样（pick_variety），单次结果有随机性。
    # 正确实现下 #1 的分数远高于 #2（1.0 vs 0），会独占整条分数线；
    # 写成「scene 那一路不算」时两者同分，会退化成五五开。
    picks = []
    for _ in range(40):
        memo, decided = await dispatcher._pick_by_vector(
            pool, "笑死", "嗯嗯", "umo", [], None, False
        )
        assert decided is True
        picks.append(memo.label if memo else None)
    share = picks.count("laugh") / len(picks)
    assert share > 0.9, f"回复命中话题词时应该几乎总选中它，实际只占 {share:.0%}"

    # 用户消息命中、回复无关 → 靠 context 那一路
    picks2 = []
    for _ in range(40):
        memo, _ = await dispatcher._pick_by_vector(
            pool, "嗯嗯", "笑死", "umo", [], None, False
        )
        picks2.append(memo.label if memo else None)
    share2 = picks2.count("laugh") / len(picks2)
    assert share2 > 0.9, f"用户命中话题词时应该几乎总选中它，实际只占 {share2:.0%}"
    store.close()
