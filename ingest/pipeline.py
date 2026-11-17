"""入库流水线：队列 + worker + 状态机。

不变量守卫：
- I-1：每条媒体只调用一次视觉模型（analyze_detailed 内部不重试）。
- I-3：队列满时丢弃新任务并计数，绝不阻塞消息处理路径。
- I-4：先 tmp → 原子改名 → 写库；写库失败删文件，保证无半行、无孤儿。
- I-6：任何失败路径都不让媒体进入 ready（模型失败直接删临时文件）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import embed as emb
from ..config import QUEUE_CAPACITY, PERCEPT_THRESHOLD, Settings
from ..paths import is_safe_rel_path
from ..domain import (
    FALLBACK_LABEL,
    SCOPE_ANY,
    SCOPE_ORIGIN,
    STATE_INBOX,
    STATE_READY,
    Analysis,
    Draft,
    MediaRef,
)
from ..store import MemoStore
from . import dedup
from .analyze import LLMCall, analyze_with_retry
from .fetch import FetchLimits, fetch_media


@dataclass(slots=True)
class IngestContext:
    """一条媒体在生命周期内的作用域上下文。"""

    umo: str | None
    origin_kind: str = "chat"
    scope: str = SCOPE_ANY
    persona_id: str | None = None


@dataclass(slots=True)
class SubmitResult:
    queued: int = 0
    dropped: int = 0


@dataclass(slots=True)
class IngestCounters:
    queued: int = 0
    stored: int = 0
    duplicate: int = 0
    failed: int = 0
    rejected: int = 0
    dropped_full: int = 0
    no_model: int = 0
    """模型不可用而按裸条目入库的条数：这是「识图其实没生效」的唯一可见痕迹。"""


def _day_of(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def safe_rel_path(rel_path: str) -> bool:
    """入库前的路径守卫（实现见 paths.py，读写两侧共用）。"""
    return is_safe_rel_path(rel_path)


class IngestPipeline:
    def __init__(
        self,
        *,
        store: MemoStore,
        data_dir: Path,
        settings: Callable[[], Settings],
        llm_call: Callable[[], LLMCall | None],
        prompt: str,
        log: Any,
        clock: Callable[[], float] = time.time,
        embedder: Any = None,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir)
        self.tmp_dir = self.data_dir / "tmp"
        self._settings = settings
        self._llm_call = llm_call
        self._prompt = prompt
        self._log = log
        self._embedder = embedder
        self._clock = clock
        self.counters = IngestCounters()
        self._queue: asyncio.Queue[tuple[MediaRef, IngestContext]] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._dedup_lock = asyncio.Lock()
        # 全局并发槽位：队列 worker 和面板导入都从这里拿，`worker_count` 才是
        # 「同时处理几张图」的真实上限。没有它的话两条路径各跑各的，
        # 实际并发是 2×worker_count，识图 API 会被双倍打。
        self._slots: asyncio.Semaphore | None = None
        # 每日识图预算（按本地日期翻页）：管的是「真调视觉模型」的次数，
        # 内存计数即可——重启丢当日进度，换来的是不引入持久化状态。
        self._vision_day = _day_of(self._clock())
        self._vision_calls = 0
        # 「没有视觉模型」的 WARNING 每次启动只喊一次，避免每张图刷屏
        self._warned_no_model = False

    def vision_usage(self) -> tuple[str, int]:
        """(当天日期, 今日识图次数)；跨天时自动翻页归零。"""
        today = _day_of(self._clock())
        if today != self._vision_day:
            self._vision_day = today
            self._vision_calls = 0
        return today, self._vision_calls

    def vision_budget_left(self) -> bool:
        """预算是否还有余量；daily_vision_budget<=0 表示不限。"""
        limit = int(getattr(self._settings(), "daily_vision_budget", 0) or 0)
        if limit <= 0:
            return True
        _day, used = self.vision_usage()
        return used < limit

    def note_vision_call(self) -> None:
        """一次真实的视觉模型调用（含失败——失败同样花了钱）。"""
        self.vision_usage()  # 先翻页再计数
        self._vision_calls += 1

    # -- 生命周期 ---------------------------------------------------------

    def queue_depth(self) -> int:
        queue = self._queue
        return queue.qsize() if queue is not None else 0

    async def wait_idle(self) -> None:
        """等当前队列处理完（停机前的礼貌等待与测试都用它）。"""
        queue = self._queue
        if queue is not None:
            await queue.join()

    def prepare_dirs(self) -> None:
        for name in ("blobs", "inbox", "tmp"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)

    def clear_tmp(self, keep_recent: float = 0.0) -> int:
        """只清 tmp/：启动时清空，绝不碰 blobs/ 与 inbox/。

        `keep_recent` 用于定时清理：跳过最近 N 秒写入的文件，避免删掉 worker 正在处理的临时文件。
        """
        removed = 0
        if not self.tmp_dir.exists():
            return 0
        cutoff = self._clock() - keep_recent
        for entry in self.tmp_dir.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    async def start(self) -> None:
        if self._queue is not None:
            return
        self.prepare_dirs()
        self.clear_tmp()
        self._stop.clear()
        settings = self._settings()
        self._slots = asyncio.Semaphore(max(1, settings.worker_count))
        self._queue = asyncio.Queue(maxsize=max(1, QUEUE_CAPACITY))
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"memenest-worker-{index}")
            for index in range(max(1, settings.worker_count))
        ]

    async def stop(self) -> None:
        """停队列、取消 worker、等它们退出（I-2 的前半段：不再有新任务）。"""
        self._stop.set()
        self._slots = None
        queue, self._queue = self._queue, None
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    # -- 入队 -------------------------------------------------------------

    def submit(self, refs: list[MediaRef], ctx: IngestContext) -> SubmitResult:
        result = SubmitResult()
        queue = self._queue
        if queue is None:
            result.dropped = len(refs)
            self.counters.dropped_full += len(refs)
            return result
        for ref in refs:
            try:
                queue.put_nowait((ref, ctx))
            except asyncio.QueueFull:
                result.dropped += 1
                self.counters.dropped_full += 1
                self._log.debug(
                    "[MemeNest] 队列已满，丢弃新任务 reason=%s platform=%s",
                    ref.reason,
                    ref.platform,
                )
                continue
            result.queued += 1
            self.counters.queued += 1
        return result

    # -- worker -----------------------------------------------------------

    async def _worker(self, index: int) -> None:
        queue = self._queue
        while queue is not None and not self._stop.is_set():
            try:
                ref, ctx = await queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await self._process(ref, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单条失败不得掀翻 worker
                self.counters.failed += 1
                self._log.error(
                    "[MemeNest] worker=%s 处理异常 type=%s", index, type(exc).__name__
                )
            finally:
                queue.task_done()

    async def _process(self, ref: MediaRef, ctx: IngestContext) -> str:
        """处理一条：先抢并发槽位，再干活。

        队列 worker 和面板导入都走这里，所以 `worker_count` 是总并发上限。
        两边平等竞争：用户主动点的导入没有额外配额，点了就尽快跑完，
        消息抓图那边如果正好在处理就等它腾出槽位（队列有容量，不会丢）。

        没 start 过（单测直接调）时槽位为 None，直接放行。
        """
        slots = self._slots
        if slots is None:
            return await self._run_one(ref, ctx)
        async with slots:
            return await self._run_one(ref, ctx)

    async def _run_one(self, ref: MediaRef, ctx: IngestContext) -> str:
        """真正处理一条，返回 "stored" / "duplicate" / "failed" / "rejected"。"""
        settings = self._settings()
        limits = FetchLimits(max_bytes=settings.max_bytes, max_pixels=settings.max_pixels)
        fetched = await fetch_media(ref, limits, self.tmp_dir)
        if not fetched.ok or fetched.media is None:
            self.counters.rejected += 1
            self._log.debug(
                "[MemeNest] 抓取失败 reason=%s platform=%s",
                fetched.reason,
                ref.platform,
            )
            return "rejected"

        media = fetched.media
        try:
            async with self._dedup_lock:
                verdict = await asyncio.to_thread(
                    dedup.find_duplicate,
                    self.store,
                    media.content_hash,
                    media.percept_hash,
                    PERCEPT_THRESHOLD,
                )
            if verdict.duplicate:
                self.counters.duplicate += 1
                self._log.debug(
                    "[MemeNest] 去重命中 reason=%s existing=%s",
                    verdict.reason,
                    verdict.existing_id,
                )
                return "duplicate"

            analysis, used_model = await self._classify(media.path)
            if analysis is None:
                # I-6：分类失败一律不入库，临时文件在 finally 清理。
                self.counters.failed += 1
                return "failed"

            return await self._persist(
                media.path,
                media.ext,
                media.content_hash,
                media.percept_hash,
                analysis,
                ctx,
                settings,
                used_model=used_model,
            )
        finally:
            try:
                media.path.unlink(missing_ok=True)
            except OSError:
                pass

    async def process_one(
        self, ref: MediaRef, ctx: IngestContext
    ) -> dict[str, Any]:
        """同步处理一条，不走队列，返回这一条的结果。

        面板导入用。结局由 `_process` 直接返回，不能拿计数器前后差值反推：
        前端并发发几条时，差值会互相把别人的入库算进自己头上。
        """
        try:
            outcome = await self._process(ref, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 与 worker 一致：单条失败不能把请求打成 500
            self.counters.failed += 1
            self._log.error("[MemeNest] 单条处理异常 type=%s", type(exc).__name__)
            outcome = "failed"
        return {"outcome": outcome}

    async def _classify(self, path: Path) -> tuple[Analysis | None, bool]:
        """返回 (分析结果, 是否真的调用过模型)。"""
        settings = self._settings()
        call = self._llm_call()
        if call is None:
            # F2.5：分类能力不可用时收集照常，但只能进审核池并落在兜底类。
            self.counters.no_model += 1
            if not self._warned_no_model:
                # 升到 WARNING 且只喊一次：这条退化只表现为「所有新图都没有描述」，
                # 曾经只记 DEBUG，结果部署端静静攒了几百条无描述条目没人发现。
                self._warned_no_model = True
                self._log.warning(
                    "[MemeNest] 没有可用的视觉模型，新收条目只按兜底类入库（后续同类不再提示，"
                    "面板「缺模型」计数会继续累加）"
                )
            return Analysis(label=FALLBACK_LABEL), False
        if not self.vision_budget_left():
            # 预算耗尽等同于「模型今天不可用」：收进审核池做裸条目，
            # 明天预算翻页后照常打标（点「补齐」不会补打标，重跑识别即可）。
            self._log.info("[MemeNest] 今日识图预算已用完，按审核池裸条目入库")
            return Analysis(label=FALLBACK_LABEL), False
        try:
            outcome = await analyze_with_retry(
                path,
                prompt=self._prompt,
                llm_call=call,
                work_dir=self.tmp_dir,
                can_attempt=self.vision_budget_left,
                on_attempt=self.note_vision_call,
            )
        except asyncio.CancelledError:
            raise
        if outcome.analysis is None:
            # 一次调用不重试：失败就是这张图没入库，让用户能在日志里看到原因
            self._log.info("[MemeNest] 分类失败 reason=%s（不入库）", outcome.reason)
            return None, True
        return outcome.analysis, True

    async def _persist(
        self,
        tmp_path: Path,
        ext: str,
        content_hash: str,
        percept: str | None,
        analysis: Analysis,
        ctx: IngestContext,
        settings: Settings,
        *,
        used_model: bool,
    ) -> str:
        now = int(self._clock())
        state = STATE_INBOX if (settings.review_enabled or not used_model) else STATE_READY
        target, rel_path = self._target_path(now, content_hash, ext, state)
        if not safe_rel_path(rel_path):
            self._log.error("[MemeNest] 目标路径非法，放弃入库")
            return "rejected"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, target)
        except OSError as exc:
            self._log.error("[MemeNest] 落盘失败 type=%s", type(exc).__name__)
            return "rejected"

        scope = ctx.scope if ctx.scope in (SCOPE_ANY, SCOPE_ORIGIN) else SCOPE_ANY
        draft = Draft(
            rel_path=rel_path,
            content_hash=content_hash,
            percept_hash=percept,
            analysis=analysis,
            scope=scope,
            origin_umo=ctx.umo,
            origin_kind=ctx.origin_kind,
            created_at=now,
            state=state,
            persona_ids=(ctx.persona_id,) if ctx.persona_id else (),
        )
        try:
            async with self._dedup_lock:
                # 落盘在锁外，所以写库前必须在锁内再确认一次，否则跨秒的同内容会双入库。
                existing = await asyncio.to_thread(
                    self.store.match_content_hash, content_hash
                )
                if existing is not None:
                    _unlink(target)
                    self.counters.duplicate += 1
                    return "duplicate"
                memo_id = await asyncio.to_thread(self.store.add, draft)
        except Exception as exc:
            # 任何写库异常都要把已经搬进来的文件撤掉：留下就是孤儿（库里没有引用它）。
            # 记 ERROR 并返回显式 "failed"，不吞：上层会计入失败数、面板也看得到。
            self._log.error("[MemeNest] 写库失败 type=%s", type(exc).__name__)
            _unlink(target)
            return "failed"
        if memo_id is None:
            # 同一路径已存在：等价于重复，删掉刚搬过来的文件即可。
            _unlink(target)
            self.counters.duplicate += 1
            return "duplicate"
        self.counters.stored += 1
        self._log.info(
            "[MemeNest] 入库 id=%s label=%s state=%s", memo_id, analysis.label, state
        )
        await self._index_vector(memo_id, analysis)
        return "stored"

    async def _index_vector(self, memo_id: int, analysis: Analysis) -> None:
        """入库后顺手算向量。失败不影响入库结果，下次重建会补上。"""
        factory = self._embedder
        if factory is None:
            return
        try:
            embedder = factory()
        except Exception as exc:
            # 拿不到 embedder → 这张图入库后永远没有向量，只能等重建：必须留痕
            self._log.warning(
                "[MemeNest] 向量模型初始化失败，id=%s 暂无向量 type=%s",
                memo_id,
                type(exc).__name__,
            )
            return
        if embedder is None:
            return
        parts = emb.vectors_for(
            text_in_image=analysis.text_in_image,
            caption=analysis.caption,
            scenes=analysis.scenes,
        )
        # 三路各建一个向量；空的那几路不建，检索时自然缺席
        want = [(kind, text) for kind, text in parts.items() if text.strip()]
        if not want:
            return
        vectors = await embedder.embed_many([text for _, text in want], batch_size=8)
        rows: list[tuple[int, str, str, int, bytes]] = []
        for (kind, _), vector in zip(want, vectors):
            if vector is None:
                continue
            rows.append((memo_id, kind, embedder.model, len(vector), emb.pack(vector)))
        if not rows:
            return
        try:
            await asyncio.to_thread(
                self.store.save_embeddings, rows, int(self._clock())
            )
        except Exception as exc:  # 索引失败不进错误日志正文
            self._log.warning("[MemeNest] 向量入库失败 type=%s", type(exc).__name__)

    def _target_path(
        self, now: int, content_hash: str, ext: str, state: str
    ) -> tuple[Path, str]:
        stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(now))
        base = f"{stamp}_{content_hash[:8]}{ext}"
        if state == STATE_READY:
            month = time.strftime("%Y%m", time.localtime(now))
            target = self.data_dir / "blobs" / month / base
            return target, f"blobs/{month}/{base}"
        target = self.data_dir / "inbox" / base
        return target, f"inbox/{base}"

    # -- 容量与清理 -------------------------------------------------------

    async def enforce_capacity(self) -> int:
        """超限淘汰：先删库行，再删文件（R-8）。返回淘汰条数。"""
        settings = self._settings()
        before = await asyncio.to_thread(self.store.stats)
        removed = await asyncio.to_thread(self.store.evict, settings.library_cap)
        shortfall = (before.ready - settings.library_cap) - len(removed)
        if shortfall > 0:
            # 收藏与人工导入永不淘汰，库可能停在超限状态。
            self._log.warning(
                "[MemeNest] 容量超限 %s 条无法淘汰（收藏/导入受保护）", shortfall
            )
        for rel_path in removed:
            absolute = self.data_dir / rel_path
            try:
                absolute.unlink(missing_ok=True)
            except OSError:
                self._log.warning("[MemeNest] 淘汰文件删除失败 rel=%s", rel_path)
        if removed:
            self._log.info("[MemeNest] 容量淘汰 count=%s", len(removed))
        return len(removed)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
