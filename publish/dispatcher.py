"""发送编排：回合状态、六道闸、候选打分、延迟与取消（F4 / I-5 / I-2）。

「本回合」= 同一会话从上一条入站消息到它的回复结束。状态只存在于内存，
停机时全部取消（I-2：重载后不会出现幽灵发图）。

发送前会按配置把图压缩成表情包尺寸与体积（`publish/compress.py`），压缩失败回落原图。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import (
    COMMAND_NAME,
    SEND_MAX_SIDE,
    VARIETY_DELTA,
    WEIGHT_CAPTION,
    WEIGHT_LABEL,
    WEIGHT_SCENES,
    WEIGHT_TEXT,
    Settings,
)
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
from . import gate, pick, punctuation
from .pick import score_memo
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

# 发送来源标记（面板「发送记录」的 via 字段）：前端按这三个值显示「向量 / 关键词 / 工具」。
VIA_VECTOR = "vector"
VIA_KEYWORD = "keyword"
VIA_TOOL = "tool"

# 向量路里两项微调的量级（比 delta 小一个数量级，只动同分附近的排序）
VECTOR_CATEGORY_PENALTY = 0.02
VECTOR_PRIOR_BONUS = 0.02

DeliverFn = Callable[[str, Path], Awaitable[bool]]
PersonaResolver = Callable[[str], str | None]


def score_vector_entry(
    memo: Memo,
    kinds: dict[str, Any],
    query: tuple[float, ...],
    context_query: tuple[float, ...] | None,
    *,
    label_bonus: float = 0.0,
    recent_by_label: dict[str, tuple[int, ...]] | None = None,
    expected_labels: set[str] | frozenset[str] | None = None,
) -> float | None:
    """一条候选的向量分（三路加权余弦 + 标签 + 同类避让 + 标点先验）。

    两条路共用同一份口径：真实挑图和面板「试一试」的预览。分开写两份的话，
    调权重时预览和实际行为会悄悄不一致，那这个预览工具就白做了。
    """
    total_weight = 0.0
    if WEIGHT_TEXT and kinds.get("text"):
        total_weight += WEIGHT_TEXT
    if WEIGHT_CAPTION and kinds.get("caption"):
        total_weight += WEIGHT_CAPTION
    if WEIGHT_SCENES and kinds.get("scene"):
        total_weight += WEIGHT_SCENES
    if WEIGHT_LABEL:
        total_weight += WEIGHT_LABEL
    if total_weight <= 0:
        total_weight = 1.0

    value = 0.0
    if WEIGHT_TEXT:
        value += WEIGHT_TEXT * emb.dot(query, kinds.get("text", ()))
    if WEIGHT_CAPTION:
        value += WEIGHT_CAPTION * emb.dot(query, kinds.get("caption", ()))
    if WEIGHT_SCENES:
        # 话题词没有说话人：机器人自己在聊这个话题、还是用户在聊，都该用这张图。
        # 取两路较高的那个，避免同一句话两边都算成双倍权重。
        # 注意别拿 0.0 兜底——余弦可能为负，垫个 0 等于给这一路偷偷加了个 ReLU，
        # 和 text/caption 两路的口径就不一致了。
        scene_vec = kinds.get("scene", ())
        if scene_vec:
            sims = [emb.dot(query, scene_vec)]
            if context_query is not None:
                sims.append(emb.dot(context_query, scene_vec))
            value += WEIGHT_SCENES * max(sims)
    if WEIGHT_LABEL:
        value += WEIGHT_LABEL * label_bonus
    # 同类最近使用：扣一点点（0.02 量级），只影响同分附近的排序，
    # 不会把明显更相关的图压下去。
    if recent_by_label:
        value -= pick.category_recent_penalty(
            recent_by_label.get(memo.label, ()), memo.id, VECTOR_CATEGORY_PENALTY
        )
    # 标点先验：关键词/语义都没给出情绪时的一点方向
    if expected_labels and memo.label in expected_labels:
        value += VECTOR_PRIOR_BONUS
    return value / total_weight


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
        # 已排期但还没落地的发送任务。回合表会在下一条消息到来时被重置，
        # 任务本身不能跟着丢——停机时要逐个取消，否则就是幽灵发图。
        self._pending: set[asyncio.Task[None]] = set()
        self.stats = DeliveryStats()
        self._closed = False
        # 最近成功发送的观测样本（面板「发送记录」）：内存环形队列即可，
        # 重启清零可接受——它服务于调参，不是审计日志。
        self._recent_sends: deque[dict] = deque(maxlen=20)
        # 21 个标签的向量：一次算好反复用，模型变了才重算
        self._label_vecs: dict[str, tuple[float, ...]] = {}
        self._label_model = ""
        # `label_extraction=llm` 时从这句话里萃取情绪；每回合由 _pick_by_vector 覆盖
        self._pending_context = ""

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

    def note_incoming(self, umo: str | None) -> None:
        """新消息 = 新回合：重置回合状态，但**不打断**已经在延迟里的发送。

        延迟只是「等 N 秒再发」，别人说话不该让上一张取消；反过来，已经在跑的
        任务也不该继续占着回合标记，所以这里无条件换一份新状态。
        （任务本身记在 `_pending` 里，不依赖回合表还引用着它。）
        """
        if not umo:
            return
        self._turns[umo] = TurnState()
        self._turns.move_to_end(umo)
        self._prune(self._turns)

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
                # 人格解析失败会让过滤退化成「只发通用」，属于不报错的错误行为：
                # 用 WARNING 留下痕迹，别让它在 DEBUG 里消失。
                self._log.warning(
                    "[MemeNest] 解析人格失败，本回合按无人格处理 type=%s",
                    type(exc).__name__,
                )
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
            self._log.debug("[MemeNest] 闸拒绝 code=%s detail=%s", decision.code, decision.detail)
            return FireOutcome(decision.code, detail=decision.detail)

        persona_id, persona_filter = self._filters(umo)
        memo, low_relevance, via = await self._choose(
            umo, text, context_text, persona_id, persona_filter
        )
        if memo is None:
            if low_relevance:
                self.stats.note_reject(Reason.LOW_RELEVANCE)
                self._log.debug(
                    "[MemeNest] 候选都不够相关（下限 %.2f），本回合不发 umo=%s",
                    settings.min_keyword_score,
                    umo,
                )
                return FireOutcome(Reason.LOW_RELEVANCE)
            self.stats.note_reject(Reason.NO_CANDIDATE)
            self._log.debug("[MemeNest] 没有可用候选 umo=%s", umo)
            return FireOutcome(Reason.NO_CANDIDATE)

        state.used = True
        delay = gate.humanized_delay(settings.send_delay_seconds, text, self._roll())
        task = asyncio.get_running_loop().create_task(
            self._deliver_later(
                umo, memo, delay, state, trigger_text=text, via=via
            ),
            name="memenest-send",
        )
        state.task = task
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
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
    ) -> tuple[Memo | None, bool, str]:
        """返回 (候选, 是否「不够相关」, 走了哪条路：vector / keyword / "")。"""
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
            return None, False, ""
        recent = await asyncio.to_thread(self.store.recent_ids, 8)
        recent_by_label = await asyncio.to_thread(self.store.recent_ids_by_label, 4)
        expected = punctuation.expected_labels(text)
        picked, decided = await self._pick_by_vector(
            pool,
            text,
            context_text,
            umo,
            recent,
            persona_id,
            persona_filter,
            recent_by_label=recent_by_label,
            expected_labels=expected,
        )
        if decided:
            # 向量路已经做出决定：picked 为 None 就是「相似度不够」，不是没候选
            return picked, picked is None, VIA_VECTOR

        min_score = float(getattr(self._settings(), "min_keyword_score", 0.0) or 0.0)
        context = pick.PickContext(
            umo=umo,
            keywords=pick.extract_keywords(text),
            context_keywords=pick.extract_keywords(context_text),
            recent_ids=recent,
            recent_by_label=recent_by_label,
            expected_labels=frozenset(expected),
            rng=self._rng,
            persona_id=persona_id,
            persona_filter=persona_filter,
            min_score=min_score,
        )
        memo, below = pick.pick_best(pool, context)
        return memo, below, VIA_KEYWORD

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
            self._note_send(umo, memo, "", VIA_TOOL, outcome="missing", detail="file_missing")
            return FireOutcome(Reason.FILE_MISSING)
        ok, detail = await self._deliver_with_reason(umo, path)
        if not ok:
            self._note_send(umo, memo, "", VIA_TOOL, outcome="failed", detail=detail)
            return FireOutcome(Reason.SEND_FAILED)
        # 工具发送也要进发送记录：面板问的是「最近发过什么」，漏掉工具那一路
        # 会让人以为只有自动发送在用图。
        self.stats.sent += 1
        self._note_send(umo, memo, "", VIA_TOOL)
        await self._mark_used(umo, memo)
        return FireOutcome(Reason.OK, memo=memo)

    # -- 发送前处理 -------------------------------------------------------

    async def _deliver_with_reason(self, umo: str, path: Path) -> tuple[bool, str]:
        """调用注入的投递回调，返回 (是否成功, 失败原因)。

        回调允许返回 `(ok, detail)` 或旧式 `bool`：前者能带上异常类型名
        （发送历史要显示「为什么没发出去」），后者兼容测试与第三方注入。
        """
        result = await self._deliver(umo, path)
        if isinstance(result, tuple):
            return bool(result[0]), str(result[1] or "")
        return bool(result), ""

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
            max_side=SEND_MAX_SIDE,
            max_bytes=settings.send_max_bytes,
        )
        if not result.ok:
            self._log.debug("[MemeNest] 压缩失败 reason=%s，回落原图", result.reason)
            return source
        if result.reason == "compressed":
            self.stats.compressed += 1
        return result.path

    async def _mark_used(
        self, umo: str, memo: Memo, state: TurnState | None = None
    ) -> None:
        """记一次成功发送：更新冷却起点与使用计数，并标记**发起它的那个回合**。

        `state` 必须由调用方传入：延迟落地时当前回合可能已经换成下一条消息的回合，
        再用 `_state(umo)` 去取就会把下一回合提前判成「已用过」，吞掉它的一次配图。
        """
        now = self._clock()
        self._last_sent[umo] = now
        self._prune(self._last_sent)
        try:
            await asyncio.to_thread(self.store.bump_usage, memo.id, int(now))
        except Exception as exc:
            # 用量没记上，淘汰算法就会优先删掉「刚发过」的图：降级但不能静默
            self._log.warning(
                "[MemeNest] 使用计数失败，淘汰会偏向这张 id=%s type=%s",
                memo.id,
                type(exc).__name__,
            )
        (state if state is not None else self._state(umo)).used = True

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
        except Exception as exc:
            # 工厂就是 _resolve_embedder，异常意味着语义检索整条被关掉
            self._log.warning("[MemeNest] 向量模型不可用 type=%s", type(exc).__name__)
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
        text = self._pending_context.strip()
        if not text:
            return set()
        try:
            raw = await self._label_llm(text)
        except Exception as exc:
            # 情绪那一路静默得 0 分，本回合会退化成只按画面/话题打分的普通检索
            self._log.warning(
                "[MemeNest] 标签萃取失败，本回合情绪路按空处理 type=%s",
                type(exc).__name__,
            )
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
        *,
        recent_by_label: dict[str, tuple[int, ...]] | None = None,
        expected_labels: set[str] | None = None,
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
        except Exception as exc:
            self.stats.vector_unavailable += 1
            self._log.warning(
                "[MemeNest] 向量索引读取失败，本回合回落关键词 type=%s",
                type(exc).__name__,
            )
            return None, False
        if not vectors:
            self.stats.vector_unavailable += 1
            return None, False

        # 两路查询向量：回复去比「图上文字/画面」，用户消息去比「使用场景」
        query = await embedder.embed(text)
        if query is None:
            self.stats.vector_degraded += 1
            self._log.debug("[MemeNest] 查询向量失败，回落到关键词")
            return None, False
        context_query = None
        if context_text.strip():
            context_query = await embedder.embed(context_text)
        # LLM 萃取分支要读原始文本，向量分支只用向量
        self._pending_context = context_text or text

        settings = self._settings()
        query_labels = (
            await self._label_hits(embedder, context_query or query) if WEIGHT_LABEL else set()
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
            value = score_vector_entry(
                memo,
                kinds,
                query,
                context_query,
                label_bonus=self._label_pick(memo, query_labels),
                recent_by_label=recent_by_label,
                expected_labels=expected_labels,
            )
            if value is not None:
                scored.append((value, memo))
        if not scored:
            self.stats.vector_unavailable += 1
            return None, False

        scored.sort(key=lambda item: -item[0])
        best = scored[0][0]
        # 语义下限：全场最高分都不到线就这回合不发。默认 0 = 不设限——
        # 实测「该发」与「不该发」的相似度区间有重叠，默认值开得太严只会误杀。
        threshold = float(getattr(settings, "embedding_min_similarity", 0.0) or 0.0)
        if threshold > 0 and best < threshold:
            self.stats.vector_rejects += 1
            self._log.debug(
                "[MemeNest] 相似度低于下限 最高=%.3f 下限=%.3f，本回合不发",
                best,
                threshold,
            )
            return None, True
        # 取前 K 名，划一条「最高分 - delta」的线，线内按离线的距离加权抽一张。
        # 直接取第一名会让同一句话永远发同一张：库里 500 张实测只用到 365 张，
        # 剩下 135 张永远排不到第一。delta 越大进线的越多、越活泼。
        top = scored[:VECTOR_TOP_K]
        variety = self._settings().pick_variety
        delta = VARIETY_DELTA.get(variety, VARIETY_DELTA["balanced"])
        floor = best - delta
        pool = [(sim, memo) for sim, memo in top if sim >= floor]
        if not pool:
            pool = top[:1]

        # 权重 = 分数离线的距离；收藏给一点点加成，其他一律不加。
        # 以前这里还有 -0.08 的「最近发过」惩罚，比 delta 本身还大，
        # 会把线内精心挑出来的候选重新排一遍，现在由 delta 天然承担。
        weights = [
            max(sim - floor, 0.0) + (0.01 if getattr(memo, "is_fav", False) else 0.0)
            for sim, memo in pool
        ]
        if sum(weights) <= 0:
            # 收藏加成可能让全员为 0（分数都压在线上），这时退化成均匀采样
            weights = [1.0] * len(pool)
        if self._rng is not None:
            chosen = self._rng.choices(pool, weights=weights, k=1)[0][1]
        else:
            total = sum(weights)
            cut = random.random() * total
            running = 0.0
            chosen = pool[-1][1]
            for (sim, memo), weight in zip(pool, weights):
                running += weight
                if cut <= running:
                    chosen = memo
                    break
        chosen_similarity = next(
            sim for sim, memo in pool if memo.id == chosen.id
        )
        self.stats.vector_hits += 1
        self._log.debug(
            "[MemeNest] 向量选中 id=%s 相似度=%.3f 线内 %d 张 delta=%.2f 全局最高=%.3f",
            chosen.id,
            chosen_similarity,
            len(pool),
            delta,
            best,
        )
        return chosen, True


    async def _deliver_later(
        self,
        umo: str,
        memo: Memo,
        delay: float,
        state: TurnState,
        *,
        trigger_text: str = "",
        via: str = "",
    ) -> None:
        """state 必须是安排发送时的那个对象：delay=0 时 dict 里可能已经换了新回合。"""
        try:
            if delay:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._log.debug("[MemeNest] 发送被取消 umo=%s", umo)
            raise
        if state.cancelled or self._closed:
            self.stats.cancelled += 1
            self._note_send(umo, memo, trigger_text, via, outcome="cancelled", detail="cancelled")
            return
        path = await self._prepare_path(memo)
        if path is None:
            self.stats.failed += 1
            self._log.debug("[MemeNest] 发送放弃 reason=%s id=%s", Reason.FILE_MISSING, memo.id)
            self._note_send(umo, memo, trigger_text, via, outcome="missing", detail="file_missing")
            return
        ok, detail = await self._deliver_with_reason(umo, path)
        if not ok:
            self.stats.failed += 1
            self._log.debug("[MemeNest] 发送失败 reason=%s id=%s", Reason.SEND_FAILED, memo.id)
            self._note_send(umo, memo, trigger_text, via, outcome="failed", detail=detail)
            return
        self.stats.sent += 1
        self._note_send(umo, memo, trigger_text, via)
        await self._mark_used(umo, memo, state)

    # -- 检索排序（工具路径复用同一套口径） --------------------------------

    async def rank_pool(
        self,
        pool: list[Memo],
        text: str,
        context_text: str = "",
    ) -> tuple[list[tuple[float, Memo]], str]:
        """给候选排序，返回 ([(分数, memo)] 由高到低, 走了哪条路）。

        工具检索与自动发送共用这份打分，避免「同一句话，模型搜到的和自动发的不是一张」。
        向量可用时走向量（含标签命中、同类避让、标点先验），否则回落关键词。
        """
        if not pool:
            return [], ""
        recent = await asyncio.to_thread(self.store.recent_ids, 8)
        recent_by_label = await asyncio.to_thread(self.store.recent_ids_by_label, 4)
        expected = punctuation.expected_labels(text)

        embedder = self._active_embedder()
        if embedder is not None and self._vectors is not None:
            try:
                vectors = await self._vectors.get(embedder.model, embedder.dim)
                query = await embedder.embed(text)
            except Exception as exc:
                self._log.warning(
                    "[MemeNest] 工具检索的向量路失败，回落关键词 type=%s",
                    type(exc).__name__,
                )
                vectors, query = None, None
            if vectors and query is not None:
                context_query = (
                    await embedder.embed(context_text) if context_text.strip() else None
                )
                query_labels: set[str] = set()
                if WEIGHT_LABEL and not self._use_llm_labels():
                    try:
                        query_labels = await self._label_hits(
                            embedder, context_query or query
                        )
                    except Exception as exc:
                        self._log.debug(
                            "[MemeNest] 工具检索的标签命中失败 type=%s",
                            type(exc).__name__,
                        )
                scored: list[tuple[float, Memo]] = []
                for memo in pool:
                    kinds = vectors.get(memo.id)
                    if not kinds:
                        continue
                    value = score_vector_entry(
                        memo,
                        kinds,
                        query,
                        context_query,
                        label_bonus=self._label_pick(memo, query_labels),
                        recent_by_label=recent_by_label,
                        expected_labels=expected,
                    )
                    if value is not None:
                        scored.append((value, memo))
                if scored:
                    scored.sort(key=lambda item: -item[0])
                    return scored, VIA_VECTOR

        context = pick.PickContext(
            umo=None,
            keywords=pick.extract_keywords(text),
            context_keywords=pick.extract_keywords(context_text),
            recent_ids=recent,
            recent_by_label=recent_by_label,
            expected_labels=frozenset(expected),
            rng=self._rng,
        )
        ranked = sorted(
            ((score_memo(memo, context), memo) for memo in pool),
            key=lambda item: -item[0],
        )
        return ranked, VIA_KEYWORD

    # -- 发送观测（面板「发送记录」弹层的数据源） --------------------------

    def _note_send(
        self,
        umo: str,
        memo: Memo,
        trigger_text: str,
        via: str,
        *,
        outcome: str = "sent",
        detail: str = "",
    ) -> None:
        """记一次发送：成功进内存环形（面板即时看），全部落库（发送历史）。

        写历史失败只记 WARNING：发图已经成功了，不能因为记账失败把它退回去。
        """
        now = int(self._clock())
        if outcome == "sent":
            self._recent_sends.append(
                {
                    "time": now,
                    "umo": umo,
                    "memo_id": memo.id,
                    "label": memo.label,
                    "caption": memo.caption[:40],
                    "text": (trigger_text or "")[:60],
                    "via": via,
                }
            )
        try:
            self.store.add_send_log(
                created_at=now,
                umo=umo,
                memo_id=memo.id,
                via=via,
                label=memo.label,
                text=trigger_text or "",
                outcome=outcome,
                detail=detail,
            )
        except Exception as exc:
            self._log.warning(
                "[MemeNest] 写发送历史失败 outcome=%s type=%s",
                outcome,
                type(exc).__name__,
            )

    def recent_sends(self) -> list[dict]:
        """最近的发送记录（新→旧，面板直接按顺序渲染）。"""
        return list(reversed(self._recent_sends))

    def reject_stats(self) -> dict[str, int]:
        """拒发原因分布（含向量路自身的不够相关计数）。"""
        data = dict(self.stats.rejected)
        if self.stats.vector_rejects:
            data["vector_below_threshold"] = self.stats.vector_rejects
        return data

    # -- 检索预览（面板「试一试」：两条路各自的 top 候选与原始分） --------

    async def preview(self, text: str, context_text: str = "", *, limit: int = 5) -> dict:
        """Dry-run 检索：不过闸、不记用量、不做多样性重排，只看原始分。

        这是调参工具：换提示词、调权重、改阈值前后各跑一遍同一句话，
        对比 top 候选的变化。作用域/人格过滤也不做——目的是看检索本身。
        """
        text = (text or "").strip()
        if not text:
            return {"keyword": [], "vector": None, "model": ""}
        pool = await asyncio.to_thread(
            self.store.candidates,
            CandidateQuery(
                state=STATE_READY,
                umo=None,
                ignore_scope=True,
                limit=400,
                persona_id=None,
                persona_filter=False,
            ),
        )
        if not pool:
            return {"keyword": [], "vector": None, "model": ""}

        def _entry(memo: Memo, score: float) -> dict:
            return {
                "id": memo.id,
                "label": memo.label,
                "caption": memo.caption[:40],
                "text_in_image": (memo.text_in_img or "")[:30],
                "score": round(float(score), 4),
            }

        # 关键词路：纯函数打分排序
        recent = await asyncio.to_thread(self.store.recent_ids, 8)
        recent_by_label = await asyncio.to_thread(self.store.recent_ids_by_label, 4)
        expected = punctuation.expected_labels(text)
        context = pick.PickContext(
            umo=None,
            keywords=pick.extract_keywords(text),
            context_keywords=pick.extract_keywords(context_text),
            recent_ids=recent,
            recent_by_label=recent_by_label,
            expected_labels=frozenset(expected),
            rng=self._rng,
        )
        keyword_ranked = sorted(
            ((score_memo(memo, context), memo) for memo in pool),
            key=lambda item: -item[0],
        )
        keyword_top = [_entry(memo, score) for score, memo in keyword_ranked[:limit]]

        # 向量路：与 _pick_by_vector 共用 score_vector_entry，且带上同样的先验与惩罚，
        # 唯一差别是不做多样性重排（预览要看的是原始排序）。
        vector_top: list[dict] | None = None
        model = ""
        embedder = self._active_embedder()
        if embedder is not None and self._vectors is not None:
            model = embedder.model
            try:
                vectors = await self._vectors.get(embedder.model, embedder.dim)
                query = await embedder.embed(text)
            except Exception as exc:
                self._log.warning("[MemeNest] 预览向量路失败 type=%s", type(exc).__name__)
                vectors, query = None, None
            if vectors and query is not None:
                context_query = (
                    await embedder.embed(context_text) if context_text.strip() else None
                )
                # 标签命中：vector 模式走标签向量（算一次就缓存），llm 模式跳过——
                # 预览不该为了一次调参额外花一次模型调用。
                query_labels: set[str] = set()
                if WEIGHT_LABEL and not self._use_llm_labels():
                    try:
                        query_labels = await self._label_hits(
                            embedder, context_query or query
                        )
                    except Exception as exc:
                        self._log.debug(
                            "[MemeNest] 预览标签命中失败 type=%s", type(exc).__name__
                        )
                scored: list[tuple[float, Memo]] = []
                for memo in pool:
                    kinds = vectors.get(memo.id)
                    if not kinds:
                        continue
                    value = score_vector_entry(
                        memo,
                        kinds,
                        query,
                        context_query,
                        label_bonus=self._label_pick(memo, query_labels),
                        recent_by_label=recent_by_label,
                        expected_labels=expected,
                    )
                    if value is not None:
                        scored.append((value, memo))
                scored.sort(key=lambda item: -item[0])
                vector_top = [_entry(memo, score) for score, memo in scored[:limit]]
        return {"keyword": keyword_top, "vector": vector_top, "model": model}

    # -- 停机 -------------------------------------------------------------

    async def shutdown(self) -> None:
        """取消全部待发任务（I-2），不论它们还挂在哪一回合上。"""
        self._closed = True
        tasks = set(self._pending)
        for state in self._turns.values():
            if state.task is not None and not state.task.done():
                tasks.add(state.task)
            state.cancelled = True
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._turns.clear()


def _command_markers(settings: Settings) -> tuple[str, ...]:
    """命令类回复一律不补表情：默认标记 + 本插件自己的指令前缀。"""
    return gate.DEFAULT_COMMAND_MARKERS + (f"/{COMMAND_NAME}",)


__all__ = ["DeliveryStats", "Dispatcher", "FireOutcome", "TurnState"]
