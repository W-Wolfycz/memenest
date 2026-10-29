"""发送编排：回合状态、六道闸、候选打分、延迟与取消（F4 / I-5 / I-2）。

「本回合」= 同一会话从上一条入站消息到它的回复结束。状态只存在于内存，
停机时全部取消（I-2：重载后不会出现幽灵发图）。

发送前会按配置把图压缩成表情包尺寸与体积（`publish/compress.py`），压缩失败回落原图。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from .. import embed as emb
from ..domain import (
    LABEL_KEYS,
    STATE_READY,
    CandidateQuery,
    Memo,
    Reason,
    label_display,
)
from ..store import MemoStore
from .vectors import VectorStore
from . import gate, pick
from .candidates import CandidateCache
from .compress import CACHE_SUBDIR, compress_for_send

MAX_TRACKED_TURNS = 512

# 标签向量的绝对相似度下限。不同 embedding 模型的绝对值不可移植，
# 这个值只用来挡掉「完全不像」，精确度靠后面看相对差。
LABEL_MIN_SIMILARITY = 0.35


@dataclass(slots=True)
class TurnState:
    attempted: bool = False
    used: bool = False
    cancelled: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class FireOutcome:
    code: str
    scheduled: bool = False
    memo: Memo | None = None
    detail: str = ""


@dataclass(slots=True)
class DeliveryStats:
    attempts: int = 0
    sent: int = 0
    cancelled: int = 0
    failed: int = 0
    compressed: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    vector_hits: int = 0
    vector_rejects: int = 0
    vector_degraded: int = 0
    vector_unavailable: int = 0

    def note_reject(self, code: str) -> None:
        name = code.split(" ", 1)[-1]
        self.rejected[name] = self.rejected.get(name, 0) + 1


# 向量取前 K 名后再叠多样性，避免同一语境永远同一张
VECTOR_TOP_K = 20

DeliverFn = Callable[[str, Path], Awaitable[bool]]
PersonaResolver = Callable[[str], str | None]


class Dispatcher:
    def __init__(
        self,
        *,
        store: MemoStore,
        data_dir: Path,
        settings: Callable[[], Settings],
        log: Any,
        deliver: DeliverFn,
        clock: Callable[[], float] = time.time,
        rng: Any = None,
        cache: CandidateCache | None = None,
        persona_resolver: PersonaResolver | None = None,
        embedder: Any = None,
        vectors: VectorStore | None = None,
        label_llm: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir)
        self._settings = settings
        self._log = log
        self._deliver = deliver
        self._clock = clock
        self._rng = rng
        self._cache = cache
        self._persona_resolver = persona_resolver
        self._embedder = embedder
        self._vectors = vectors
        # 可选：`label_extraction=llm` 时用它从话语里萃取情绪标签
        self._label_llm = label_llm
        self._turns: OrderedDict[str, TurnState] = OrderedDict()
        self._last_sent: OrderedDict[str, float] = OrderedDict()
        self.stats = DeliveryStats()
        self._closed = False
        # 21 个标签的向量：一次算好反复用，模型变了才重算
        self._label_vecs: dict[str, tuple[float, ...]] = {}
        self._label_model = ""

    # -- 回合 -------------------------------------------------------------

    def _state(self, umo: str) -> TurnState:
        state = self._turns.get(umo)
        if state is None:
            state = TurnState()
            self._turns[umo] = state
            self._prune(self._turns)
        else:
            self._turns.move_to_end(umo)
        return state

    @staticmethod
    def _prune(store: OrderedDict[str, Any]) -> None:
        while len(store) > MAX_TRACKED_TURNS:
            store.popitem(last=False)

    def note_incoming(self, umo: str | None) -> bool:
        """新消息 = 新回合：只重置回合状态，不打断已经在延迟里的发送。

        延迟是纯粹的「等 N 秒再发」，别人说话不影响它。
        """
        if not umo:
            return False
        return_value = False
        previous = self._turns.get(umo)
        if previous is not None and previous.task is not None and not previous.task.done():
            self._log.debug("memenest: 上一回合仍在延迟中，保留不打断 umo=%s", umo)
            return_value = False
        self._turns[umo] = TurnState()
        self._turns.move_to_end(umo)
        self._prune(self._turns)
        return return_value

    # -- 会话策略 ---------------------------------------------------------

    def _session_allowed(self, umo: str | None) -> bool:
        whitelist = self._settings().send_whitelist
        if not whitelist:
            return True
        key = gate.session_key(umo)
        return key is not None and key in whitelist

    def _session_cooling(self, umo: str, now: float) -> bool:
        cooldown = self._settings().send_cooldown_seconds
        if cooldown <= 0:
            return False
        last = self._last_sent.get(umo)
        return last is not None and (now - last) < cooldown

    def _filters(self, umo: str) -> tuple[str | None, bool]:
        """返回 (当前人格 id, 是否启用人格过滤)。"""
        settings = self._settings()
        if not settings.persona_isolation:
            return None, False
        persona_id: str | None = None
        if self._persona_resolver is not None:
            try:
                persona_id = self._persona_resolver(umo) or None
            except Exception as exc:
                self._log.debug("memenest: 解析人格失败 type=%s", type(exc).__name__)
        return persona_id, True

    # -- 闸与选片 ---------------------------------------------------------

    async def fire(
        self,
        *,
        umo: str | None,
        text: str,
        context_text: str = "",
        reply_has_image: bool,
    ) -> FireOutcome:
        """评估并安排一次发送。同一回合最多走一遍闸。"""
        if not umo:
            return FireOutcome(Reason.SESSION_DENIED, detail="缺少会话标识")
        if self._closed:
            return FireOutcome(Reason.SWITCH_OFF, detail="插件正在停机")
        state = self._state(umo)
        if state.attempted:
            return FireOutcome(Reason.TURN_USED)
        state.attempted = True
        self.stats.attempts += 1

        settings = self._settings()
        now = self._clock()
        decision = gate.evaluate(
            gate.GateInput(
                auto_send_enabled=settings.send_enabled,
                turn_used=state.used,
                turn_cancelled=state.cancelled,
                session_allowed=self._session_allowed(umo),
                session_cooling=self._session_cooling(umo, now),
                text=text,
                ratio=settings.send_ratio,
                roll=self._roll(),
                has_candidate=True,
                command_markers=_command_markers(settings),
                reply_has_image=reply_has_image,
            )
        )
        if not decision.allowed:
            self.stats.note_reject(decision.code)
            self._log.debug("memenest: 闸拒绝 code=%s detail=%s", decision.code, decision.detail)
            return FireOutcome(decision.code, detail=decision.detail)

        persona_id, persona_filter = self._filters(umo)
        memo, low_relevance = await self._choose(
            umo, text, context_text, persona_id, persona_filter
        )
        if memo is None:
            if low_relevance:
                self.stats.note_reject(Reason.LOW_RELEVANCE)
                self._log.debug(
                    "memenest: 候选都不够相关（下限 %.2f），本回合不发 umo=%s",
                    settings.min_keyword_score,
                    umo,
                )
                return FireOutcome(Reason.LOW_RELEVANCE)
            self.stats.note_reject(Reason.NO_CANDIDATE)
            self._log.debug("memenest: 没有可用候选 umo=%s", umo)
            return FireOutcome(Reason.NO_CANDIDATE)

        state.used = True
        delay = max(0, settings.send_delay_seconds)
        state.task = asyncio.get_running_loop().create_task(
            self._deliver_later(umo, memo, delay, state), name="memenest-send"
        )
        return FireOutcome(Reason.OK, scheduled=True, memo=memo)

    def _roll(self) -> float:
        if self._rng is not None:
            return float(self._rng.random())
        return random.random()

    async def _choose(
        self,
        umo: str,
        text: str,
        context_text: str,
        persona_id: str | None,
        persona_filter: bool,
    ) -> tuple[Memo | None, bool]:
        """候选查询走缓存（命中即零查库），打分仍是纯函数（I-8）。"""
        if self._cache is not None:
            pool = await self._cache.get(persona_id=persona_id)
        else:
            pool = await asyncio.to_thread(
                self.store.candidates,
                CandidateQuery(
                    state=STATE_READY,
                    umo=None,
                    ignore_scope=True,
                    limit=400,
                    persona_id=persona_id,
                    persona_filter=persona_filter,
                ),
            )
        if not pool:
            return None, False
        recent = await asyncio.to_thread(self.store.recent_ids, 8)
        picked, decided = await self._pick_by_vector(
            pool, text, context_text, umo, recent, persona_id, persona_filter
        )
        if decided:
            # 向量路已经做出决定：picked 为 None 就是「相似度不够」，不是没候选
            return picked, picked is None

        min_score = float(getattr(self._settings(), "min_keyword_score", 0.0) or 0.0)
        context = pick.PickContext(
            umo=umo,
            keywords=pick.extract_keywords(text),
            context_keywords=pick.extract_keywords(context_text),
            recent_ids=recent,
            rng=self._rng,
            persona_id=persona_id,
            persona_filter=persona_filter,
            min_score=min_score,
        )
        return pick.pick_best(pool, context)

    async def fire_manual(self, *, umo: str | None, memo: Memo) -> FireOutcome:
        """工具入口的显式发送：只过总开关、会话策略、作用域与人格归属，不掷骰、不延迟。"""
        if not umo:
            return FireOutcome(Reason.SESSION_DENIED)
        if self._closed:
            return FireOutcome(Reason.FEATURE_OFF, detail="插件正在停机")
        settings = self._settings()
        if not settings.send_enabled:
            return FireOutcome(Reason.FEATURE_OFF)
        if not self._session_allowed(umo):
            return FireOutcome(Reason.SESSION_DENIED)
        if not pick.scope_allows(memo, umo):
            return FireOutcome(Reason.SCOPE_DENIED)
        persona_id, persona_filter = self._filters(umo)
        if persona_filter and not pick.persona_allows(memo, persona_id):
            return FireOutcome(Reason.PERSONA_DENIED)
        path = await self._prepare_path(memo)
        if path is None:
            return FireOutcome(Reason.FILE_MISSING)
        ok = await self._deliver(umo, path)
        if not ok:
            return FireOutcome(Reason.SEND_FAILED)
        await self._mark_used(umo, memo)
        return FireOutcome(Reason.OK, memo=memo)

    # -- 发送前处理 -------------------------------------------------------

    async def _prepare_path(self, memo: Memo) -> Path | None:
        """原图不存在 → None；压缩失败 → 原图。"""
        source = self.data_dir / memo.rel_path
        if not source.is_file():
            return None
        settings = self._settings()
        if not settings.send_compress:
            return source
        result = await asyncio.to_thread(
            compress_for_send,
            source,
            self.data_dir / CACHE_SUBDIR,
            max_side=settings.send_max_side,
            max_bytes=settings.send_max_bytes,
        )
        if not result.ok:
            self._log.debug("memenest: 压缩失败 reason=%s，回落原图", result.reason)
            return source
        if result.reason == "compressed":
            self.stats.compressed += 1
        return result.path

    async def _mark_used(self, umo: str, memo: Memo) -> None:
        now = self._clock()
        self._last_sent[umo] = now
        self._prune(self._last_sent)
        try:
            await asyncio.to_thread(self.store.bump_usage, memo.id, int(now))
        except Exception as exc:  # 统计失败不影响已发出的消息
            self._log.debug("memenest: 使用计数失败 type=%s", type(exc).__name__)
        state = self._state(umo)
        state.used = True

    # -- 延迟下发 ---------------------------------------------------------

    def _active_embedder(self) -> Any:
        settings = self._settings()
        if not getattr(settings, "embedding_enabled", False):
            return None
        factory = self._embedder
        if factory is None:
            return None
        try:
            return factory()
        except Exception:
            return None

    async def _label_hits(self, embedder: Any, query: tuple[float, ...] | None) -> set[str]:
        """用户这句话最像哪几个情绪标签。

        默认走标签向量（21 个标签各算一次，缓存住），避免每条消息多调一次模型；
        `label_extraction=llm` 时由上层换成模型萃取。
        """
        if query is None:
            return set()
        if self._use_llm_labels():
            return await self._labels_by_llm()

        if self._label_model != embedder.model or not self._label_vecs:
            texts = [label_display(key) for key in LABEL_KEYS]
            vectors = await embedder.embed_many(texts, batch_size=16)
            cache: dict[str, tuple[float, ...]] = {}
            for key, vector in zip(LABEL_KEYS, vectors):
                if vector is not None:
                    cache[key] = vector
            if not cache:
                return set()
            self._label_vecs = cache
            self._label_model = embedder.model

        ranked = sorted(
            ((emb.dot(query, vector), key) for key, vector in self._label_vecs.items()),
            reverse=True,
        )
        # 取前两个明显领先的：绝对阈值在不同 embedding 模型间不可移植，看相对差
        hits: set[str] = set()
        if not ranked:
            return hits
        top_score, top_key = ranked[0]
        if top_score >= LABEL_MIN_SIMILARITY:
            hits.add(top_key)
        for score, key in ranked[1:3]:
            if score >= LABEL_MIN_SIMILARITY and top_score - score <= 0.05:
                hits.add(key)
        return hits

    def _use_llm_labels(self) -> bool:
        if self._label_llm is None:
            return False
        mode = getattr(self._settings(), "label_extraction", "vector")
        return str(mode).lower() == "llm"

    async def _labels_by_llm(self) -> set[str]:
        """让模型直接读这句话的情绪。

        比标签向量准（能理解反讽、上下文），代价是每条消息多一次模型调用；
        默认走向量，只有把 `label_extraction` 改成 llm 才走这条。
        """
        text = getattr(self, "_pending_context", "") or ""
        if not text.strip():
            return set()
        try:
            raw = await self._label_llm(text)
        except Exception as exc:
            self._log.debug("memenest: 标签萃取失败 type=%s", type(exc).__name__)
            return set()
        from ..domain import normalize_labels

        found = normalize_labels(str(raw or "").split(","))
        return set(found[:2])

    def _label_pick(self, memo: Memo, query_labels: set[str]) -> float:
        """标签命中程度：主标签权重 1.0，附加标签 0.5，都没中返回 0。"""
        if not query_labels:
            return 0.0
        if memo.label in query_labels:
            return 1.0
        if any(item in query_labels for item in memo.labels):
            return 0.5
        return 0.0

    async def _pick_by_vector(
        self,
        pool: list[Memo],
        text: str,
        context_text: str,
        umo: str,
        recent: set[int] | list[int],
        persona_id: str | None,
        persona_filter: bool,
    ) -> tuple[Memo | None, bool]:
        """返回 (选中的表情, 是否已由向量路径做出决定)。

        decided=False 表示向量不可用（索引空/查询失败），调用方应回落到关键词打分。
        decided=True 且 picked=None 表示「相似度低于阈值」，本回合不发。
        """
        embedder = self._active_embedder()
        if embedder is None or self._vectors is None:
            return None, False

        try:
            vectors = await self._vectors.get(embedder.model, embedder.dim)
        except Exception:
            self.stats.vector_unavailable += 1
            return None, False
        if not vectors:
            self.stats.vector_unavailable += 1
            return None, False

        # 两路查询向量：回复去比「图上文字/画面」，用户消息去比「使用场景」
        query = await embedder.embed(text)
        if query is None:
            self.stats.vector_degraded += 1
            self._log.debug("memenest: 查询向量失败，回落到关键词")
            return None, False
        context_query = None
        if context_text.strip():
            context_query = await embedder.embed(context_text)
        # LLM 萃取分支要读原始文本，向量分支只用向量
        self._pending_context = context_text or text

        settings = self._settings()
        w_text = float(getattr(settings, "weight_text", 0.4))
        w_caption = float(getattr(settings, "weight_caption", 0.15))
        w_scenes = float(getattr(settings, "weight_scenes", 0.35))
        w_label = float(getattr(settings, "weight_label", 0.10))

        query_labels = (
            await self._label_hits(embedder, context_query or query) if w_label else set()
        )

        scored: list[tuple[float, Memo]] = []
        for memo in pool:
            kinds = vectors.get(memo.id)
            if not kinds:
                continue
            if not pick.scope_allows(memo, umo):
                continue
            if persona_filter and not pick.persona_allows(memo, persona_id):
                continue
            # 分母按「这条实际有哪几路」算：没有话题词的图（立绘、壁纸）不该
            # 因为缺了一路就永久少 0.35，否则同分情况下它永远排在有词图的后面。
            total_weight = 0.0
            if w_text and kinds.get("text"):
                total_weight += w_text
            if w_caption and kinds.get("caption"):
                total_weight += w_caption
            if w_scenes and kinds.get("scene"):
                total_weight += w_scenes
            if w_label:
                total_weight += w_label
            if total_weight <= 0:
                total_weight = 1.0
            value = 0.0
            if w_text:
                value += w_text * emb.dot(query, kinds.get("text", ()))
            if w_caption:
                value += w_caption * emb.dot(query, kinds.get("caption", ()))
            if w_scenes:
                # 话题词没有说话人：机器人自己在聊这个话题、还是用户在聊，都该用这张图。
                # 取两路较高的那个，避免同一句话两边都算成双倍权重。
                # 注意别拿 0.0 兜底——余弦可能为负，垫个 0 等于给这一路偷偷加了个 ReLU，
                # 和 text/caption 两路的口径就不一致了。
                scene_vec = kinds.get("scene", ())
                if scene_vec:
                    sims = [emb.dot(query, scene_vec)]
                    if context_query is not None:
                        sims.append(emb.dot(context_query, scene_vec))
                    value += w_scenes * max(sims)
            if w_label:
                value += w_label * self._label_pick(memo, query_labels)
            scored.append((value / total_weight, memo))
        if not scored:
            self.stats.vector_unavailable += 1
            return None, False

        scored.sort(key=lambda item: -item[0])
        best = scored[0][0]
        threshold = float(getattr(self._settings(), "embedding_threshold", 0.0) or 0.0)
        if best < threshold:
            self.stats.vector_rejects += 1
            self._log.debug("memenest: 向量相似度低于阈值 best=%.3f < %.3f", best, threshold)
            return None, True

        # 相似度主导排序，多样性只在「分差很小」时起作用：
        # 直接拿关键词那套 score_memo 重排会把相似度信号完全冲掉（实测会选错图）。
        chosen: Memo | None = None
        chosen_value = float("-inf")
        chosen_similarity = 0.0
        for similarity, memo in scored[:VECTOR_TOP_K]:
            value = similarity + self._diversity_bonus(memo, recent)
            if value > chosen_value:
                chosen_value, chosen = value, memo
                chosen_similarity = similarity
        if chosen is None:
            return None, False
        self.stats.vector_hits += 1
        self._log.debug(
            "memenest: 向量选中 id=%s 相似度=%.3f 调整后=%.3f 全局最高=%.3f",
            chosen.id,
            chosen_similarity,
            chosen_value,
            best,
        )
        return chosen, True

    def _diversity_bonus(self, memo: Memo, recent: set[int] | list[int]) -> float:
        """最多 ±0.1 的微调，保证相似度差 0.1 以上不会被翻转。"""
        bonus = 0.0
        if memo.id in recent:
            bonus -= 0.08
        if getattr(memo, "is_fav", False):
            bonus += 0.02
        if memo.use_count <= 0:
            bonus += 0.01
        if self._rng is not None:
            bonus += float(self._rng.uniform(0.0, 0.02))
        return bonus

    async def _deliver_later(
        self, umo: str, memo: Memo, delay: int, state: TurnState
    ) -> None:
        """state 必须是安排发送时的那个对象：delay=0 时 dict 里可能已经换了新回合。"""
        try:
            if delay:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._log.debug("memenest: 发送被取消 umo=%s", umo)
            raise
        if state.cancelled or self._closed:
            self.stats.cancelled += 1
            return
        path = await self._prepare_path(memo)
        if path is None:
            self.stats.failed += 1
            self._log.debug("memenest: 发送放弃 reason=%s id=%s", Reason.FILE_MISSING, memo.id)
            return
        ok = await self._deliver(umo, path)
        if not ok:
            self.stats.failed += 1
            self._log.debug("memenest: 发送失败 reason=%s id=%s", Reason.SEND_FAILED, memo.id)
            return
        self.stats.sent += 1
        await self._mark_used(umo, memo)

    # -- 停机 -------------------------------------------------------------

    async def shutdown(self) -> None:
        """取消全部待发任务（I-2）。"""
        self._closed = True
        tasks = [
            state.task
            for state in self._turns.values()
            if state.task is not None and not state.task.done()
        ]
        for state in self._turns.values():
            state.cancelled = True
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._turns.clear()


def _command_markers(settings: Settings) -> tuple[str, ...]:
    """命令类回复一律不补表情：默认标记 + 本插件自己的指令前缀。"""
    return gate.DEFAULT_COMMAND_MARKERS + (f"/{settings.command_prefix}",)


__all__ = ["DeliveryStats", "Dispatcher", "FireOutcome", "TurnState"]
