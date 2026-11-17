"""表情窝插件装配层：生命周期、hook 注册、门面方法。

依赖方向：plugin → publish / ingest → store → domain。所有 SQL 走 store，
所有宿主 API 接触点集中在本文件、publish/sender.py 与 panel.py。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools

from . import commands_core, tools_core
from .commands import CommandRunner
from .config import (
    COMMAND_NAME,
    LOG_TAG,
    PERCEPT_THRESHOLD,
    PLUGIN_NAME,
    Settings,
    deprecated_keys,
    load_settings,
)
from .domain import STATE_INBOX, STATE_READY, CandidateQuery, MediaRef, Reason
from .ingest import analyze, dedup
from .ingest.collect import plan_collect, collect_refs
from .ingest.backfill import backfill_percepts
from .ingest.fetch import FetchLimits, fetch_media
from .ingest.pipeline import IngestContext, IngestPipeline
from .ingest.reanalyze import Reanalyzer
from .integrity import scan as integrity_scan
from .ingest.throttle import Throttle
from .publish import gate, pick, punctuation, sender
from . import embed as emb
from . import panel_core
from .embed import Embedder
from .publish.candidates import CandidateCache
from .publish.gate import session_key
from .publish.vectors import VectorStore
from .publish.compress import CACHE_SUBDIR, clear_cache
from .publish.dispatcher import Dispatcher
from .panel import PanelApi
from .paths import resolve_within
from .store import MemoStore

COLLECT_PRIORITY = 10_000
DECORATE_PRIORITY = 100
"""装饰阶段的 priority 越大越早。取正数是为了赶在 t2i、引用前缀这类改链插件
之前读到原始回复文本——晚于它们执行时，chain 里可能已经没有可用文字了。"""
TMP_CLEAN_INTERVAL = 30 * 60
CAPACITY_INTERVAL = 60 * 60
CACHE_MAX_AGE = 24 * 60 * 60
"""压缩缓存产物的保留时长：过期才清，避免每次发送都重新压一遍。"""
VISION_PROBE_INTERVAL = 60.0
"""视觉模型不可用时，间隔多少秒重新探测一次。

宿主在插件启动阶段可能还没把 provider 装好（冷启动顺序、provider 热重载）；
只在 initialize() 里解析一次的话，拿到空值就永久为空，所有新图都静默落成「其他」。"""
COMMAND_MARK = "_memenest_command_turn"
"""本回合是指令交互：不补发表情，避免打断管理操作。"""
# `label_extraction=llm` 时用：只要情绪 key，不要解释，省 token 也省解析
LABEL_PROMPT = (
    "从下面这套情绪里选出这段话最接近的 1-2 个，只返回 key，用逗号分隔，"
    "不要写任何解释：\n"
    "joy | laugh | smug | kudos | cheer | love | shy | sad | cry | upset "
    "| disgust | blank | awkward | puzzle | wow | alarm | drowsy | slack "
    "| refuse | expect | other\n\n"
    "这段话：{text}"
)

FALLBACK_PROMPT = (
    "请用 JSON 描述这张聊天表情："
    '{"label": "21 类之一", "labels": [], "scenes": [], '
    '"text_in_image": "", "caption": "一句话"}'
)


def _load_prompt() -> str:
    path = Path(__file__).resolve().parent / "prompts" / "classify.txt"
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as exc:
        # 回落兜底提示词会让打标质量断崖下跌，必须在启动日志里看得见
        logger.warning("读取 classify.txt 失败，改用兜底提示词 type=%s", type(exc).__name__)
        return FALLBACK_PROMPT
    return analyze.build_prompt(template)


class Main(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = MemoStore(self.data_dir / "memo.db")
        self.throttle = Throttle()
        self._pipeline: IngestPipeline | None = None
        self._dispatcher: Dispatcher | None = None
        self._cache: CandidateCache | None = None
        self._panel = PanelApi(self)
        self._runner = CommandRunner(self)
        self._handles = tools_core.HandleRegistry()
        self._collect_override: bool | None = None
        self._collect_restore_task: asyncio.Task[None] | None = None
        self._model_provider_id = ""
        # 上次探测视觉模型的时间：模型没就绪时按需重试，但不必每张图都探一次
        self._vision_probe_at = 0.0
        self._prompt = _load_prompt()
        self._maintenance: list[asyncio.Task[None]] = []
        self._reanalyzer: Reanalyzer | None = None
        self._reanalyze_task: asyncio.Task[Any] | None = None

    # -- 生命周期 ---------------------------------------------------------

    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.store.initialize)
        self._model_provider_id = self._resolve_vision_provider()
        if not self._model_provider_id:
            logger.warning("%s 没有可用的视觉模型，收集仍工作但新条目只进待审核池", self._tag())
        self._cache = CandidateCache(self.store, self._settings, log=logger)
        self._vectors = VectorStore(self.store, log=logger)
        self._reindex: dict[str, Any] = {"running": False, "done": 0, "total": 0}
        self._pipeline = IngestPipeline(
            store=self.store,
            data_dir=self.data_dir,
            settings=self._settings,
            llm_call=self._llm_call,
            prompt=self._prompt,
            log=logger,
            embedder=self._resolve_embedder,
        )
        await self._pipeline.start()
        self._dispatcher = Dispatcher(
            store=self.store,
            data_dir=self.data_dir,
            settings=self._settings,
            log=logger,
            deliver=lambda umo, path: sender.deliver(self.context, umo, path),
            cache=self._cache,
            persona_resolver=self._resolve_persona_id,
            embedder=self._resolve_embedder,
            vectors=self._vectors,
            label_llm=self._label_llm,
        )
        self._reanalyzer = Reanalyzer(
            store=self.store,
            data_dir=self.data_dir,
            llm_call=self._llm_call,
            prompt=self._prompt,
            log=logger,
            can_attempt=self._vision_budget_left,
            on_attempt=self._count_vision_call,
        )
        # 工具用 @filter.llm_tool 声明式注册（宿主据此在插件详情页展示）；
        # 这里只负责按开关把它们从「可调用列表」里摘掉或放回去。
        for tool_name in ("memenest_search", "memenest_send"):
            try:
                if self._settings().send_tools_enabled:
                    self.context.activate_llm_tool(tool_name)
                else:
                    self.context.deactivate_llm_tool(tool_name)
            except Exception as exc:
                # 宿主 API 差异不该拖垮启动，但也不能静默：配置没生效要能在日志里看到
                logger.warning(
                    "%s 工具开关未生效 tool=%s type=%s",
                    self._tag(),
                    tool_name,
                    type(exc).__name__,
                )
        self._panel.register()
        if self._settings().embedding_enabled:
            self._reindex_task = asyncio.create_task(
                self._auto_backfill(), name="memenest-vector-backfill"
            )
        self._maintenance = [
            asyncio.create_task(
                self._maintenance_loop(TMP_CLEAN_INTERVAL, self._clean_caches),
                name="memenest-cache-clean",
            ),
            asyncio.create_task(
                self._maintenance_loop(CAPACITY_INTERVAL, self.enforce_capacity),
                name="memenest-capacity",
            ),
        ]
        logger.info(
            "%s 已启动 data=%s model=%s persona_filter=%s",
            self._tag(),
            self.data_dir.name,
            "on" if self._model_provider_id else "off",
            "on" if self._settings().persona_isolation else "off",
        )
        stale = deprecated_keys(self.config)
        if stale:
            # 只在启动时提示一次：旧配置项不会生效，免得每读一次配置就刷一条。
            logger.warning(
                "%s 以下配置项已废弃，将被忽略：%s", self._tag(), ", ".join(stale)
            )

    async def terminate(self) -> None:
        # 停机顺序：取消待发 → 关队列等 worker → 关库 → 取消定时器（I-2）。
        if self._dispatcher is not None:
            await self._dispatcher.shutdown()
        if self._pipeline is not None:
            await self._pipeline.stop()
        reindex_task = getattr(self, "_reindex_task", None)
        if reindex_task is not None and not reindex_task.done():
            reindex_task.cancel()
            await asyncio.gather(reindex_task, return_exceptions=True)
        for task in self._maintenance:
            task.cancel()
        if self._maintenance:
            await asyncio.gather(*self._maintenance, return_exceptions=True)
        self._maintenance = []
        reanalyze_task = getattr(self, "_reanalyze_task", None)
        if reanalyze_task is not None and not reanalyze_task.done():
            reanalyze_task.cancel()
            await asyncio.gather(reanalyze_task, return_exceptions=True)
        await asyncio.to_thread(self.store.close)
        self._handles.clear()
        if self._cache is not None:
            self._cache.invalidate()
        self._pipeline = None
        self._dispatcher = None
        self._cache = None
        logger.info("%s 已停止", self._tag())

    async def _maintenance_loop(self, interval: int, action: Any) -> None:
        while True:
            # 首轮先等待一个周期，不在启动瞬间就动磁盘。
            await asyncio.sleep(interval)
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s 定时任务失败 type=%s", self._tag(), type(exc).__name__)

    # -- 消息 hook --------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=COLLECT_PRIORITY)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        if self._dispatcher is not None:
            self._dispatcher.note_incoming(umo)
        self._handles.invalidate(umo)

        parsed = commands_core.parse_command(event.get_message_str(), self.command_name())
        if parsed is not None:
            event.set_extra(COMMAND_MARK, True)
            # 带唤醒前缀 / 被 @ 的消息交给下面注册的 command_group 执行——只有那样
            # 指令才会出现在 WebUI 指令列表与 /help 里。这里只处理 command filter
            # 覆盖不到的写法：不带前缀的「memenest xxx」，以及只发一个「/memenest」。
            if parsed.default or not getattr(event, "is_at_or_wake_command", False):
                text = await self._runner.run(event, parsed)
                if text:
                    event.set_result(MessageEventResult().message(text))
                # True = 禁止本回合的默认 LLM 请求（接口名反直觉，语义见宿主 docstring）
                event.should_call_llm(True)
            return
        await self._collect(event)

    # -- 指令组 -----------------------------------------------------------
    # 必须用宿主装饰器注册：只有注册过的指令才会出现在 WebUI 指令列表与 /help 里，
    # 也才会被宿主的指令管理页识别。on_any_message 里的手写解析只兜它覆盖不到的写法。

    # 权限：**全部指令都是 ADMIN**。这些指令全都在动全局状态（往库里写、切收集开关、
    # 改素材在某个人格下的可用），连 status 都会暴露库容与会话归属，所以不给所有人用。
    # 声明用装饰器而不是在函数体里判 is_admin()：Dashboard 指令管理页显示的默认值
    # 只认装饰器，函数体里判会与实际行为不一致（运维会误判）。
    @filter.command_group("memenest")
    def memenest_group(self) -> None:
        """表情窝：收集、审核、发图与人格绑定。"""

    async def _run_command(
        self, event: AstrMessageEvent, sub: str, args: tuple[str, ...] = ()
    ) -> str:
        """执行一条子命令，并禁止本回合的默认 LLM 请求。

        `should_call_llm(True)` 的语义是「禁止默认 LLM 请求」（接口名反直觉），
        不这样设的话指令回复发出去之后还会再来一轮 LLM 生成。
        """
        event.should_call_llm(True)
        event.set_extra(COMMAND_MARK, True)
        return await self._runner.run(event, commands_core.ParsedCommand(sub, args))

    @memenest_group.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memenest_status(self, event: AstrMessageEvent):
        """查看库存、队列与向量覆盖率。"""
        text = await self._run_command(event, "status")
        if text:
            yield event.plain_result(text)

    @memenest_group.command("help")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memenest_help(self, event: AstrMessageEvent):
        """列出可用子命令。"""
        yield event.plain_result(commands_core.render_help(self.command_name()))

    @memenest_group.command("grab")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memenest_grab(self, event: AstrMessageEvent):
        """引用一条带图的消息，把那张图收进库。"""
        text = await self._run_command(event, "grab")
        if text:
            yield event.plain_result(text)

    @memenest_group.command("on")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memenest_on(self, event: AstrMessageEvent, minutes: str = ""):
        """临时开启收集，可带分钟数。"""
        args = (minutes,) if str(minutes).strip() else ()
        text = await self._run_command(event, "on", args)
        if text:
            yield event.plain_result(text)

    @memenest_group.command("off")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memenest_off(self, event: AstrMessageEvent, minutes: str = ""):
        """临时关闭收集，可带分钟数。"""
        args = (minutes,) if str(minutes).strip() else ()
        text = await self._run_command(event, "off", args)
        if text:
            yield event.plain_result(text)

    @memenest_group.command("mute")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memenest_mute(self, event: AstrMessageEvent):
        """把表情和指令一起发（或引用一张），对当前人格禁用它。"""
        text = await self._run_command(event, "mute")
        if text:
            yield event.plain_result(text)

    @memenest_group.command("unmute")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memenest_unmute(self, event: AstrMessageEvent):
        """恢复这张表情在当前人格下的可用。"""
        text = await self._run_command(event, "unmute")
        if text:
            yield event.plain_result(text)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        # 两种回合在 LLM 响应阶段就能排除，不必等装饰阶段：
        # - 带工具调用的回合通常紧接着 memenest_send，再自动补一张就是双发；
        # - 响应本身已经带图，没必要再加一张。
        if getattr(response, "tools_call_args", None):
            return
        response_chain = getattr(response, "result_chain", None)
        chain_list = getattr(response_chain, "chain", response_chain)
        if gate.reply_contains_image(chain_list):
            return
        await self._schedule_send(event, str(getattr(response, "completion_text", "") or ""))

    @filter.on_decorating_result(priority=DECORATE_PRIORITY)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        # 兜底：没有 agent 完成事件的路径（命令回复、别的插件产生的回复）也覆盖一次。
        await self._schedule_send(event, "")

    async def _schedule_send(self, event: AstrMessageEvent, fallback_text: str) -> None:
        dispatcher = self._dispatcher
        if dispatcher is None:
            return
        if event.get_extra(COMMAND_MARK):
            # 指令回合整轮不发表情（回复文本本身不是命令，靠标记而不是文本判断）。
            return
        result = event.get_result()
        if not gate.result_is_model_reply(result):
            # 命令回复与其他插件主动发的文本不配表情：用宿主给的结果类型判定，
            # 不再靠文本前缀猜（前缀只作为闸内兜底）。
            return
        chain = getattr(result, "chain", None) if result is not None else None
        text = gate.reply_text(chain) or fallback_text
        if not text:
            return
        outcome = await dispatcher.fire(
            umo=event.unified_msg_origin,
            text=text,
            context_text=event.get_message_str() or "",
            reply_has_image=gate.reply_contains_image(chain),
        )
        if outcome.scheduled:
            logger.debug("%s 已安排发送", self._tag(event))

    # -- 收集 -------------------------------------------------------------

    async def _collect(self, event: AstrMessageEvent) -> None:
        settings = self._settings()
        umo = event.unified_msg_origin
        outcome = collect_refs(event, accept_all=settings.collect_all_images)
        self._sync_throttle(settings)
        blacklist = settings.collect_blacklist
        if blacklist:
            key = session_key(umo)
            if key and key in blacklist:
                logger.debug(
                    "%s 命中收集黑名单 key=%s", self._tag(event), key
                )
                return
        pipeline = self._pipeline
        inbox_full = False
        if settings.inbox_cap > 0 and outcome.refs:
            inbox_full = (
                await asyncio.to_thread(self.store.count_state, STATE_INBOX)
                >= settings.inbox_cap
            )
        budget_exhausted = False
        if outcome.refs and pipeline is not None:
            budget_exhausted = not pipeline.vision_budget_left()
        plan = plan_collect(
            enabled=settings.collect_enabled,
            has_refs=bool(outcome.refs),
            throttle=self.throttle,
            now=time.monotonic(),
            inbox_full=inbox_full,
            budget_exhausted=budget_exhausted,
        )
        if not plan.should_collect:
            logger.debug(
                "%s 未收录 action=%s reason=%s platform=%s",
                self._tag(event),
                plan.action,
                outcome.reason if not outcome.refs else plan.reason,
                event.get_platform_name(),
            )
            return
        if pipeline is None:
            return
        # 普通收集一律进通用池；「引用收录」走 grab_quoted，那边才绑定人格。
        ctx = IngestContext(umo=umo, origin_kind="chat", scope="any", persona_id=None)
        result = pipeline.submit(outcome.refs, ctx)
        logger.info(
            "%s 收集 queued=%s dropped=%s source=%s",
            self._tag(event),
            result.queued,
            result.dropped,
            outcome.reason,
        )

    def _sync_throttle(self, settings: Settings) -> None:
        self.throttle.mode = settings.throttle_mode
        self.throttle.ratio = settings.collect_ratio
        self.throttle.cooldown_seconds = settings.cooldown_seconds

    # -- LLM 工具 ---------------------------------------------------------

    @filter.llm_tool(name="memenest_search")
    async def memenest_search(
        self, event: AstrMessageEvent, query: str = "", label: str = ""
    ):
        """在表情库里检索候选表情，返回带编号的候选摘要。

        只在本次回复里使用返回的编号；编号会随下一条消息失效。
        用户想发表情、但没说具体是哪一张时，先用它看看库里有什么。

        Args:
            query(string): 可选。想要的情绪或画面关键词，例如「笑死」「无语」「猫」。
            label(string): 可选。只在这个分类里找，取值如 joy/laugh/sad/upset/puzzle/blank。
        """
        return await self.tool_search(
            event, query.strip(), label.strip() or None
        )

    @filter.llm_tool(name="memenest_send")
    async def memenest_send(self, event: AstrMessageEvent, choice: str):
        """发送上一次 memenest_search 返回的某个编号对应的表情。

        只能使用上一轮检索给出的编号；编号过期会失败，需要重新检索。

        Args:
            choice(string): 候选编号，来自上一轮检索结果，例如 "1"。
        """
        return await self.tool_send(event, choice.strip())

    async def tool_search(self, event: AstrMessageEvent, query: str, label: str | None) -> str:
        umo = event.unified_msg_origin
        if not self._settings().send_enabled:
            return tools_core.render_failure(Reason.FEATURE_OFF, "自动发送已关闭")
        persona_id, persona_filter = self._session_filters(umo)
        pool = await self._candidate_pool(persona_id, persona_filter)
        if not pool:
            return tools_core.render_failure(Reason.NO_CANDIDATE, "库里没有可用表情")
        if persona_filter:
            candidates = pick.filter_for_session(pool, umo, persona_id)
        else:
            candidates = pick.filter_scope(pool, umo)
        if label:
            candidates = [memo for memo in candidates if label in memo.all_labels]
        if not candidates:
            return tools_core.render_failure(Reason.NO_CANDIDATE, "没有符合条件的表情")
        dispatcher = self._dispatcher
        if dispatcher is not None:
            # 与自动发送共用同一套打分：工具搜到的和自动发的应该是同一批候选
            ranked, _via = await dispatcher.rank_pool(candidates, query)
            ordered = [memo for _score, memo in ranked]
        else:
            context = pick.PickContext(
                umo=umo,
                keywords=pick.extract_keywords(query) if query else (),
                recent_ids=await asyncio.to_thread(self.store.recent_ids, 8),
                persona_id=persona_id,
                persona_filter=persona_filter,
            )
            ordered = sorted(
                candidates,
                key=lambda memo: pick.score_memo(memo, context),
                reverse=True,
            )
        handles = self._handles.issue(umo, ordered[:5])
        return tools_core.render_candidates(handles)

    async def tool_send(self, event: AstrMessageEvent, token: str) -> str:
        umo = event.unified_msg_origin
        if not token:
            return tools_core.render_failure(Reason.BAD_HANDLE, "缺少编号")
        if not self._handles.has_session(umo):
            return tools_core.render_failure(Reason.STALE_HANDLE, "候选已过期，请重新检索")
        memo = self._handles.resolve(umo, token)
        if memo is None:
            return tools_core.render_failure(Reason.BAD_HANDLE, f"没有编号 {token}")
        dispatcher = self._dispatcher
        if dispatcher is None:
            return tools_core.render_failure(Reason.FEATURE_OFF, "插件正在停机")
        outcome = await dispatcher.fire_manual(umo=umo, memo=memo)
        if outcome.code != Reason.OK:
            return tools_core.render_failure(outcome.code, outcome.detail)
        return tools_core.render_sent(memo)

    # -- 门面（给 commands / panel 用） -----------------------------------

    @property
    def pipeline(self) -> IngestPipeline | None:
        return self._pipeline

    @property
    def dispatcher(self):
        return self._dispatcher

    def remove_rel_file(self, rel_path: str) -> None:
        target = resolve_within(self.data_dir, rel_path)
        if target is None:
            logger.warning("[MemeNest] 拒绝删除越界路径 rel=%s", rel_path)
            return
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass

    async def remove_memo(self, memo_id: int, *, block: bool = False) -> tuple[bool, str]:
        """删行 + 删文件；block=True 时先拉黑内容指纹。"""
        memo = await asyncio.to_thread(self.store.get, memo_id)
        if memo is None:
            return False, f"没有编号 {memo_id}"
        if block:
            # 拉黑会删原图，所以先把缩略图取出来：黑名单列表要靠它才能认出是哪张
            thumb = await asyncio.to_thread(
                self._block_thumb, resolve_within(self.data_dir, memo.rel_path)
            )
            await asyncio.to_thread(
                self.store.block, memo.content_hash, int(time.time()), thumb
            )
        rel_path = await asyncio.to_thread(self.store.delete, memo_id)
        if rel_path is None:
            return False, f"没有编号 {memo_id}"
        await asyncio.to_thread(self.remove_rel_file, rel_path)
        self._panel.clear_thumbs()
        if block:
            return True, f"编号 {memo_id} 已删除并拉黑该指纹"
        return True, f"编号 {memo_id} 已删除"

    def _block_thumb(self, path: Any) -> bytes | None:
        """给即将被删的图留一张 200px 小图；失败就当没有，不影响拉黑。"""
        if path is None:
            return None
        try:
            return panel_core.thumb_bytes(path)
        except Exception as exc:
            logger.debug("拉黑前留缩略图失败 type=%s", type(exc).__name__)
            return None

    def command_name(self) -> str:
        return COMMAND_NAME

    def _settings(self) -> Settings:
        """读一次配置并叠加运行期开关（`/memenest on|off` 的临时状态）。

        配置分组只是界面结构，load_settings 已经展平；这里只覆盖会被运行时改动的那一项，
        其余直接返回快照，避免每条消息都重建一次。
        """
        base = load_settings(self.config)
        collect_enabled = (
            base.collect_enabled if self._collect_override is None else self._collect_override
        )
        if collect_enabled is base.collect_enabled:
            return base
        return replace(base, collect_enabled=collect_enabled)

    def collect_enabled(self) -> bool:
        return self._settings().collect_enabled

    def auto_send_enabled(self) -> bool:
        return self._settings().send_enabled

    def library_cap(self) -> int:
        return self._settings().library_cap

    def import_concurrency(self) -> int:
        """面板导入的并发上限：复用 worker_count（「同时处理几张图」）。"""
        return max(1, int(self._settings().worker_count))

    def vision_budget_state(self) -> dict[str, int]:
        """面板要的识图预算状态：今日已用 / 上限（0 = 不限）。"""
        pipeline = self._pipeline
        used = 0
        if pipeline is not None:
            _day, used = pipeline.vision_usage()
        return {"used": used, "limit": int(self._settings().daily_vision_budget)}

    # -- 重新识图（把识图失效期间落下的无描述条目救回来） ------------------

    def _vision_budget_left(self) -> bool:
        pipeline = self._pipeline
        return pipeline.vision_budget_left() if pipeline is not None else False

    def _count_vision_call(self) -> None:
        pipeline = self._pipeline
        if pipeline is not None:
            pipeline.note_vision_call()

    def reanalyze_state(self, scope: str = "empty", ids: list[int] | None = None) -> dict[str, Any]:
        """面板用：当前进度 + 这次会处理多少条。"""
        reanalyzer = self._reanalyzer
        if reanalyzer is None:
            return {"running": False, "total": 0, "done": 0, "filled": 0, "failed": 0,
                    "message": "", "pending": 0}
        state = reanalyzer.state.snapshot()
        try:
            state["pending"] = len(reanalyzer.targets(scope, ids))
        except Exception as exc:
            logger.debug("统计重新识图目标失败 type=%s", type(exc).__name__)
            state["pending"] = 0
        return state

    async def run_reanalyze(self, scope: str, ids: list[int] | None, *, reserved: bool) -> Any:
        """执行重新识图；reserved=True 表示调用方已经用 try_begin 占过位。"""
        reanalyzer = self._reanalyzer
        if reanalyzer is None:
            return {"ok": False, "message": "重新识图不可用"}
        if not reserved and not reanalyzer.try_begin():
            return {"ok": False, "message": "已经在重新识图了"}
        targets = await asyncio.to_thread(reanalyzer.targets, scope, ids)
        if not targets:
            reanalyzer.state.running = False
            reanalyzer.state.message = "没有需要重新识图的条目"
            return {"ok": True, "total": 0, "filled": 0, "failed": 0}
        return await reanalyzer.run_and_release(targets)

    def try_begin_reanalyze(self) -> bool:
        reanalyzer = self._reanalyzer
        return bool(reanalyzer is not None and reanalyzer.try_begin())

    def start_reanalyze(self, scope: str, ids: list[int] | None) -> bool:
        """占位并起后台任务；返回是否真的启动了。"""
        if not self.try_begin_reanalyze():
            return False
        self._reanalyze_task = asyncio.create_task(
            self.run_reanalyze(scope, ids, reserved=True),
            name="memenest-reanalyze",
        )
        return True

    # -- 完整性体检 -------------------------------------------------------

    async def integrity(self) -> dict[str, Any]:
        """库与磁盘对账（只读）。"""
        return await asyncio.to_thread(integrity_scan, self.data_dir, self.store)

    async def integrity_vacuum(self) -> dict[str, int]:
        from .integrity import vacuum as _vacuum

        return await asyncio.to_thread(_vacuum, self.store)

    async def integrity_drop_orphans(self, paths: list[str] | None = None) -> dict[str, int]:
        from .integrity import remove_orphans

        return await asyncio.to_thread(remove_orphans, self.data_dir, self.store, paths)

    async def integrity_drop_missing(self) -> dict[str, int]:
        from .integrity import remove_missing

        return await asyncio.to_thread(remove_missing, self.data_dir, self.store)

    def model_available(self) -> bool:
        # 用带重试的解析：面板上的「有没有视觉模型」必须反映此刻的真实状态
        return bool(self._vision_provider())

    def queue_depth(self) -> int:
        pipeline = self._pipeline
        return pipeline.queue_depth() if pipeline is not None else 0

    def counters(self) -> dict[str, int]:
        pipeline = self._pipeline
        if pipeline is None:
            return {}
        ing = pipeline.counters
        dispatcher = self._dispatcher
        data = {
            "入队": ing.queued,
            "入库": ing.stored,
            "重复": ing.duplicate,
            "失败": ing.failed,
            "未收录": ing.rejected,
            "队列丢弃": ing.dropped_full,
            # 只在这批里出现过才有意义：非 0 就说明识图没生效，收进来的图没描述
            "缺模型": ing.no_model,
        }
        if dispatcher is not None:
            data.update(
                {
                    "发送尝试": dispatcher.stats.attempts,
                    "已发送": dispatcher.stats.sent,
                    "发送取消": dispatcher.stats.cancelled,
                    "发送失败": dispatcher.stats.failed,
                    "压缩发出": dispatcher.stats.compressed,
                }
            )
        if self._cache is not None:
            data.update(self._cache.stats())
        return data

    def set_collect(self, value: bool, minutes: int | None = None) -> bool:
        """临时改收集开关；返回是否真的变了（本来就是这个状态时返回 False）。

        minutes 为 None 表示本次运行有效，给了分钟数就定时回到配置值。
        """
        current = self._settings().collect_enabled
        if current == value:
            return False
        self._collect_override = value
        self._schedule_collect_restore(minutes)
        return True

    def _schedule_collect_restore(self, minutes: int | None) -> None:
        if self._collect_restore_task is not None:
            self._collect_restore_task.cancel()
            self._collect_restore_task = None
        if not minutes:
            return
        self._collect_restore_task = asyncio.create_task(
            self._restore_collect_after(minutes * 60), name="memenest-collect-restore"
        )

    async def _restore_collect_after(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            raise
        self._collect_override = None
        logger.info("%s 临时收集状态到期，恢复配置值", self._tag())

    async def grab_quoted(self, sources: list[str], umo: str) -> tuple[int, int]:
        """把引用消息里的图片直接收进来：跳过判定与节流，并绑定当前人格。"""
        pipeline = self._pipeline
        if pipeline is None or not sources:
            return 0, 0
        refs = [
            MediaRef(
                source=source,
                declared_ext="",
                origin_kind="force",
                platform="panel",
                reason="grab_quote",
            )
            for source in sources[:4]
        ]
        ctx = IngestContext(
            umo=umo,
            origin_kind="force",
            scope="any",
            persona_id=self._resolve_persona_id(umo),
        )
        result = pipeline.submit(refs, ctx)
        logger.info("%s 引用收录 queued=%s dropped=%s", self._tag(), result.queued, result.dropped)
        return result.queued, result.dropped

    async def mute_for_persona(
        self, sources: list[str], umo: str, *, enabled: bool = True
    ) -> dict[str, Any]:
        """按图片定位库里的表情，对**当前人格**加/取消禁用。

        定位先认字节哈希，再认感知指纹：指令多半是引用 BOT 刚发出去的那张，
        而那是压缩产物，字节与原图不同，只能靠指纹认（实测距离 0–3，阈值 8）。
        """
        if not sources:
            return {"ok": False, "code": "no_image"}
        persona_id = self._resolve_persona_id(umo)
        if not persona_id:
            return {"ok": False, "code": "no_persona"}
        memo_id, code = await self._locate_memo_by_sources(sources)
        if memo_id is None:
            return {"ok": False, "code": code}
        if enabled:
            done = await asyncio.to_thread(
                self.store.add_persona_block, memo_id, persona_id
            )
        else:
            done = await asyncio.to_thread(
                self.store.remove_persona_block, memo_id, persona_id
            )
        if not done:
            return {"ok": False, "code": "write_failed"}
        logger.info(
            "%s 人格禁用变更 memo=%s enabled=%s", self._tag(), memo_id, enabled
        )
        logger.debug("%s 人格禁用变更 persona=%s", self._tag(), persona_id)
        return {"ok": True, "code": "ok", "memo_id": memo_id}

    async def _locate_memo_by_sources(self, sources: list[str]) -> tuple[int | None, str]:
        settings = self._settings()
        limits = FetchLimits(
            max_bytes=settings.max_bytes, max_pixels=settings.max_pixels
        )
        last_reason = ""
        for source in sources[:4]:
            ref = MediaRef(
                source=source,
                declared_ext="",
                origin_kind="force",
                platform="panel",
                reason="mute_lookup",
            )
            fetched = await fetch_media(ref, limits, self.data_dir / "tmp")
            if not fetched.ok or fetched.media is None:
                # 记下来：下载失败和「库里没有」是两回事，不能都回成同一句文案
                last_reason = fetched.reason
                continue
            media = fetched.media
            try:
                memo_id = await asyncio.to_thread(
                    self.store.match_content_hash, media.content_hash
                )
                if memo_id is None:
                    verdict = await asyncio.to_thread(
                        dedup.find_duplicate,
                        self.store,
                        media.content_hash,
                        media.percept_hash,
                        PERCEPT_THRESHOLD,
                    )
                    memo_id = verdict.existing_id
            finally:
                try:
                    media.path.unlink(missing_ok=True)
                except OSError:
                    pass
            if memo_id is not None:
                return memo_id, "ok"
        if last_reason:
            logger.debug("%s 表情定位失败 reason=%s", self._tag(), last_reason)
            return None, "fetch_failed"
        return None, "not_found"

    async def enforce_capacity(self) -> int:
        pipeline = self._pipeline
        return await pipeline.enforce_capacity() if pipeline is not None else 0

    async def _clean_caches(self) -> int:
        removed = 0
        pipeline = self._pipeline
        if pipeline is not None:
            removed += await asyncio.to_thread(pipeline.clear_tmp, 300.0)
        removed += await asyncio.to_thread(
            clear_cache, self.data_dir / CACHE_SUBDIR, CACHE_MAX_AGE
        )
        removed += await asyncio.to_thread(self._purge_imports)
        return removed

    def _purge_imports(self, ttl: int = 3600) -> int:
        """上传导入的源文件在入库后就没用了，按 TTL 清掉（tmp/ 之外的临时区）。"""
        root = self.data_dir / "imports"
        if not root.exists():
            return 0
        cutoff = time.time() - ttl
        removed = 0
        for entry in root.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def log_error(self, message: str, *args: Any) -> None:
        logger.error(message, *args)

    def _tag(self, event: AstrMessageEvent | None = None) -> str:
        settings = self._settings()
        platform_id = ""
        if event is not None and settings.log_with_bot_id:
            try:
                platform_id = event.get_platform_id()
            except Exception:
                platform_id = ""
        return f"[{LOG_TAG}][platform:{platform_id}]" if platform_id else f"[{LOG_TAG}]"

    # -- 候选与人格 -------------------------------------------------------

    def _session_filters(self, umo: str) -> tuple[str | None, bool]:
        settings = self._settings()
        if not settings.persona_isolation:
            return None, False
        return self._resolve_persona_id(umo), True

    async def _candidate_pool(
        self, persona_id: str | None, persona_filter: bool
    ) -> list[Any]:
        if self._cache is not None:
            return await self._cache.get(persona_id=persona_id if persona_filter else None)
        return await asyncio.to_thread(
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

    # -- 向量回填 ---------------------------------------------------------

    async def _auto_backfill(self) -> None:
        """启动后补一次缺失向量。

        半路才配 embedding 时，老图没有向量会被静默跳过（卡片还在但永远不发），
        这一步就是为了不让它们变成死数据。
        """
        try:
            await asyncio.sleep(5)  # 让启动流程先跑完
            await self.run_reindex(full=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s 补齐失败 type=%s", self._tag(), type(exc).__name__)

    async def _backfill_percepts(self, limit: int = 600) -> dict[str, int]:
        """给没有感知指纹的条目补算，返回 {filled, skipped}。

        实现放在 ingest/backfill，这里只负责选目录与记账（便于单测）。
        """
        return await backfill_percepts(self.store, Path(self.data_dir), limit=limit)

    def try_begin_reindex(self) -> bool:
        """同步占位，避免「检查 running」与「真正开始」之间的 await 间隙。

        HTTP 路由和自动回填都先过这一步；占位失败说明已经有一个在跑。
        """
        if self._reindex.get("running"):
            return False
        self._reindex = {"running": True, "done": 0, "total": 0}
        return True

    async def run_reindex(
        self, *, full: bool = False, reserved: bool = False
    ) -> dict[str, Any]:
        """增量补缺或全量重算；同一时刻只允许一个任务。

        reserved=True 表示调用方已经用 try_begin_reindex 占过位，这里不再重复占。

        占位之后**所有**路径都必须走 finally 释放：这里任何一步抛异常（库写失败、
        provider 报错），只要 running 停在 True，「补齐」就会永久返回「已经在补算了」，
        而且面板上看不出任何异常——只能重载插件才好。
        """
        if not reserved and not self.try_begin_reindex():
            return {"ok": False, "message": "已经在补算了"}
        try:
            return await self._run_reindex_locked(full=full)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "%s 补齐失败 type=%s", self._tag(), type(exc).__name__
            )
            return {"ok": False, "message": f"补齐失败（{type(exc).__name__}）"}
        finally:
            self._reindex = {"running": False, **self._reindex_done()}

    def _reindex_done(self) -> dict[str, int]:
        done = int(self._reindex.get("done", 0) or 0)
        total = int(self._reindex.get("total", 0) or 0)
        return {"done": done, "total": total}

    async def _run_reindex_locked(self, *, full: bool) -> dict[str, Any]:
        """占位已就绪后的实际流程；异常由 run_reindex 统一兜住并释放占位。"""
        # 感知指纹与向量互不依赖：没有向量模型时也要补，否则近似去重一直是瞎的
        percept = await self._backfill_percepts()
        if percept["filled"] or percept["skipped"]:
            logger.info(
                "%s 感知指纹回填 filled=%s skipped=%s",
                self._tag(),
                percept["filled"],
                percept["skipped"],
            )
        embedder = self._resolve_embedder()
        if embedder is None:
            return {
                "ok": True,
                "written": 0,
                "failed": 0,
                "percept_filled": percept["filled"],
                "percept_skipped": percept["skipped"],
                "message": "没有可用的向量模型，只补了感知指纹",
            }

        store = self.store
        model = embedder.model
        if full:
            # 连别的模型的记录一起清：换过模型后那些向量既不会用到、也没法对比
            removed = await asyncio.to_thread(store.clear_embeddings)
            logger.info("%s 清空全部向量 count=%s 当前模型=%s", self._tag(), removed, model)

        dim = embedder.dim
        covered, expected = await asyncio.to_thread(store.embedding_coverage, model, dim)
        self._reindex = {"running": True, "done": covered, "total": expected}
        written = failed = 0
        # 进度按「条目」算，和 expected 同量纲：written 是向量行数（一条最多 3 行），
        # 拿它直接加会出现 120/100 这种超量显示。
        filled: set[int] = set()
        # 算不出来的条目会被 missing_embedding_memos 反复返回（向量没写进库），
        # 必须自己记账跳过，否则就是一个永远打 provider 的死循环。
        skipped: set[int] = set()
        try:
            while True:
                batch = await asyncio.to_thread(
                    store.missing_embedding_memos, model, dim, 40
                )
                batch = [memo for memo in batch if memo.id not in skipped]
                if not batch:
                    break
                pairs: list[tuple[int, str, str]] = []
                for memo in batch:
                    parts = emb.vectors_for(
                        text_in_image=memo.text_in_img,
                        caption=memo.caption,
                        scenes=memo.scenes,
                    )
                    for kind, text in parts.items():
                        if text.strip():
                            pairs.append((memo.id, kind, text))
                vectors = await embedder.embed_many(
                    [text for _, _, text in pairs], batch_size=8
                )
                rows: list[tuple[int, str, str, int, bytes]] = []
                got: dict[int, int] = {}
                for (memo_id, kind, _), vector in zip(pairs, vectors):
                    if vector is None:
                        continue
                    if len(vector) != dim:
                        # provider 报的维度不准：以实际向量为准，下一轮按新维度查缺
                        dim = len(vector)
                    rows.append((memo_id, kind, model, dim, emb.pack(vector)))
                    got[memo_id] = got.get(memo_id, 0) + 1
                # 一条都没成功的记下来，免得被反复返回
                for memo in batch:
                    if not got.get(memo.id):
                        failed += 1
                        skipped.add(memo.id)
                if rows:
                    await asyncio.to_thread(store.save_embeddings, rows, int(time.time()))
                    written += len(rows)
                    filled.update(got)
                self._vectors.invalidate()
                self._reindex["done"] = covered + len(filled)
                await asyncio.sleep(0)  # 让出控制权，别卡消息处理
        finally:
            # 进度归位：running 由外层 run_reindex 的 finally 统一释放，
            # 这里只保住最终计数，避免两处都写 running 造成状态打架。
            self._reindex["done"] = covered + len(filled)
            self._reindex["total"] = expected
        logger.info(
            "%s 向量回填完成 written=%s failed=%s total=%s",
            self._tag(),
            written,
            failed,
            expected,
        )
        return {
            "ok": True,
            "written": written,
            "failed": failed,
            "percept_filled": percept["filled"],
            "percept_skipped": percept["skipped"],
        }

    def vector_state(self) -> dict[str, Any]:
        """面板要的向量状态：模型、覆盖率、是否在重建。"""
        embedder = self._resolve_embedder()
        model = embedder.model if embedder is not None else ""
        covered = expected = 0
        if model:
            try:
                dim = getattr(embedder, "dim", 0)
                covered, expected = self.store.embedding_coverage(model, dim)
            except Exception as exc:
                # 面板显示 0/0 时得能看出是「查询失败」而不是「一条都没算」
                logger.debug(
                    "%s 向量覆盖率查询失败 type=%s", self._tag(), type(exc).__name__
                )
                covered = expected = 0
        state = dict(self._reindex)
        state.update(
            {
                "model": model,
                "covered": covered,
                "expected": expected,
                "enabled": self._settings().embedding_enabled,
            }
        )
        return state

    def _resolve_embedder(self) -> Embedder | None:
        """拿宿主的 embedding provider；拿不到返回 None，检索自动回落到关键词。"""
        settings = self._settings()
        if not settings.embedding_enabled:
            return None
        provider: Any = None
        wanted = (settings.embedding_provider_id or "").strip()
        if wanted:
            try:
                provider = self.context.get_provider_by_id(wanted)
            except Exception as exc:
                logger.debug("读取指定的向量模型失败 type=%s", type(exc).__name__)
                provider = None
        if provider is None:
            try:
                providers = self.context.get_all_embedding_providers() or []
            except Exception as exc:
                logger.debug("枚举向量模型失败 type=%s", type(exc).__name__)
                providers = []
            provider = providers[0] if providers else None
        if provider is None or not hasattr(provider, "get_embedding"):
            return None
        model = (
            str(getattr(provider, "model", "") or "")
            or str(getattr(provider, "model_name", "") or "")
            or wanted
            or "default"
        )
        return Embedder(provider=provider, model=model, timeout=15.0)

    def _resolve_persona_id(self, umo: str | None) -> str | None:
        """读该会话命中 conf 的**配置级**人格 id。

        只覆盖宿主 `PersonaManager.resolve_selected_persona` 的最后一步：
        `runner_type=local` 时取 `agent_runner.config.persona.persona_id`；
        dify/coze/dashscope/deerflow 这些第三方 runner 没有独立人格字段，
        宿主会退到 `SYSTEM_DEFAULT_PERSONA_ID`，这里返回 None。

        不处理另外两级优先级——会话强制（`session_service_config.persona_id`）
        和当前对话（`conversation.persona_id`）。那两级要拿 `event` 和
        `conversation_manager` 才能查，而这里只有 umo。

        读配置失败直接抛给调用方：人格解析不出来会让人格过滤静默退化成
        「只发通用」，那是不报错的错误行为，宁可让它显式失败。
        """
        if not umo:
            return None
        config = self.context.get_config(umo)
        if not hasattr(config, "get"):
            return None
        agent = config.get("agent_runner") or {}
        runner_config = agent.get("config") or {}
        if agent.get("runner_type") == "local":
            persona = runner_config.get("persona") or {}
            persona_id = str(persona.get("persona_id") or "").strip()
        else:
            # 第三方 runner 的配置里没有 persona 段落，人格由远端决定
            persona_id = ""
        return persona_id or None

    # -- 视觉模型 ---------------------------------------------------------

    def _resolve_vision_provider(self) -> str:
        configured = self._settings().vision_provider_id
        if configured and self._provider_exists(configured):
            return configured
        if configured:
            logger.warning("%s 配置的视觉模型不存在，回落到全局图片描述模型", self._tag())
        try:
            host_config = self.context.get_config()
        except Exception as exc:
            logger.debug("读取宿主配置失败 type=%s", type(exc).__name__)
            host_config = {}
        fallback = ""
        if hasattr(host_config, "get"):
            fallback = str(
                (host_config.get("provider_settings") or {}).get(
                    "default_image_caption_provider_id"
                )
                or ""
            )
        if fallback and self._provider_exists(fallback):
            return fallback
        return ""

    def _provider_exists(self, provider_id: str) -> bool:
        getter = getattr(self.context, "get_provider_by_id", None)
        if not callable(getter):
            # 老版本没有这个方法：无法探测，按配置信任。
            return True
        try:
            return getter(provider_id) is not None
        except Exception as exc:
            # 探测本身出错时按「不可用」处理：_vision_provider 会退避重试，
            # 这里留 DEBUG 说明是探测失败而不是 provider 真不存在。
            logger.debug("探测 provider 失败 type=%s", type(exc).__name__)
            return False

    def _vision_provider(self) -> str:
        """当前可用的视觉模型 id；取不到就按需重试解析（有探测间隔）。

        为什么不能只在 initialize() 里解析一次：宿主冷启动时插件可能先于 provider
        加载完成，`get_provider_by_id` 这时查不到 → 解析结果为空 → 一直为空，
        于是每一张新图都按「模型不可用」落成兜底类 `other`（无描述），
        而这条退化只有 DEBUG 日志。实测部署端就是这样连续攒了 284 条无描述条目。
        """
        current = self._model_provider_id
        if current and self._provider_exists(current):
            return current
        now = time.monotonic()
        if (
            not current
            and self._vision_probe_at
            and now - self._vision_probe_at < VISION_PROBE_INTERVAL
        ):
            # 空值且刚探测过：别每张图都去查一遍 provider 表
            return ""
        self._vision_probe_at = now
        resolved = self._resolve_vision_provider()
        if resolved != current:
            if resolved:
                logger.info(
                    "%s 视觉模型已就绪 provider=%s（识图恢复）", self._tag(), resolved
                )
            elif current:
                logger.warning("%s 视觉模型已失效 provider=%s", self._tag(), current)
            self._model_provider_id = resolved
        return resolved

    def _llm_call(self):
        provider_id = self._vision_provider()
        if not provider_id:
            return None

        async def call(prompt: str, image_paths: list[str]) -> str:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=image_paths,
            )
            return str(getattr(response, "completion_text", "") or "")

        return call

    async def _label_llm(self, text: str) -> str:
        """让模型读一句话的情绪，返回逗号分隔的 key（`label_extraction=llm` 用）。

        只在用户显式把配置切到 llm 时才会被调用：每条消息多一次模型请求，
        换来的是反讽、上下文这类向量读不准的东西能读对。
        """
        provider_id = self._vision_provider()
        if not provider_id or not text.strip():
            return ""
        prompt = LABEL_PROMPT.format(text=text.strip()[:200])
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as exc:
            logger.debug("%s 标签萃取调用失败 type=%s", self._tag(), type(exc).__name__)
            return ""
        return str(getattr(response, "completion_text", "") or "").strip()
