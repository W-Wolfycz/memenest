"""Web 面板：数据面 API（管理页 UI 与脚本共用）。

走宿主原生插件 API（`context.register_web_api` + `astrbot.api.web`）：
路由带插件名前缀，页面 bridge 侧用不带前缀的 endpoint；不自己起服务、不自己拼 token。

返回值统一 `{"ok": bool, ...}`；校验失败统一返回 `{"ok": false, "message": ...}`（宿主对 4xx 只给状态码，原因会丢）。
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import PluginUploadFile, json_response, request

from . import backup
from . import panel_core as core
from .config import PLUGIN_NAME
from .store import SEND_LOG_DAYS, SEND_LOG_MAX
from .domain import (
    GENERIC_PERSONA,
    LABEL_KEYS,
    STATE_DISABLED,
    STATE_INBOX,
    STATE_READY,
    ORIGIN_IMPORT,
    clean_topics,
    MAX_SCENE_CHARS,
    MAX_TEXT_IN_IMAGE,
    MediaRef,
    PageQuery,
)
from .ingest import security
from .ingest.fetch import fetch_json
from . import backup
from .ingest.pipeline import IngestContext
from .paths import resolve_within

ROUTE_PREFIX = f"/{PLUGIN_NAME}"
IMPORTS_SUBDIR = "imports"
MAX_IMPORT_BYTES = 32 * 1024 * 1024
FILTER_SCAN_LIMIT = 1000


class PanelApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._thumbs = core.ThumbCache()
        # 预览图比缩略图大得多，缓存条数少一些；同样按 (rel_path, mtime) 失效。
        # 数据和 MIME 存在同一条里，不会出现两边对不上的状态。
        self._previews = core.PreviewCache()
        # 解码是 CPU 密集的：快速连点十几张时若全部并发，谁也拿不到完整时间片，
        # 表现成「越点越卡」。串成 2 路并发，排队反而更快。
        self._preview_slots = asyncio.Semaphore(2)
        # 备份打包占位：同一时刻只允许一个打包任务（并发写同一个 zip 会出坏包）
        self._exporting = False

    # -- 注册 -------------------------------------------------------------

    def clear_thumbs(self) -> None:
        """写操作后由插件清一次缩略图缓存（文件可能被移动或删除）。"""
        self._thumbs.clear()

    def register(self) -> bool:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            logger.warning("[MemeNest] 宿主不支持 register_web_api，面板 API 未注册")
            return False
        routes = (
            ("stats", self.handle_stats, ["GET"], "MemeNest 统计"),
            ("list", self.handle_list, ["GET"], "MemeNest 列表"),
            ("thumb", self.handle_thumb, ["GET"], "MemeNest 缩略图"),
            ("preview", self.handle_preview, ["GET"], "MemeNest 原图预览"),
            ("delete", self.handle_delete, ["POST"], "MemeNest 删除"),
            ("ban", self.handle_ban, ["POST"], "MemeNest 删除并拉黑"),
            ("fav", self.handle_fav, ["POST"], "MemeNest 收藏"),
            ("scope", self.handle_scope, ["POST"], "MemeNest 作用域"),
            ("bind", self.handle_bind, ["POST"], "MemeNest 人格绑定"),
            ("update", self.handle_update, ["POST"], "MemeNest 编辑描述与标签"),
            ("state", self.handle_state, ["POST"], "MemeNest 切换状态"),
            ("label", self.handle_label, ["POST"], "MemeNest 批量设主分类"),
            ("blocked", self.handle_blocked, ["GET"], "MemeNest 黑名单"),
            (
                "blocked/thumb",
                self.handle_blocked_thumb,
                ["GET"],
                "MemeNest 黑名单缩略图",
            ),
            ("unban", self.handle_unban, ["POST"], "MemeNest 解除拉黑"),
            ("import", self.handle_import, ["POST"], "MemeNest URL 导入"),
            ("import/manifest", self.handle_import_manifest, ["POST"], "MemeNest 清单导入"),
            ("search/preview", self.handle_search_preview, ["POST"], "MemeNest 检索预览"),
            ("export", self.handle_export_create, ["POST"], "MemeNest 生成备份"),
            ("export/list", self.handle_export_list, ["GET"], "MemeNest 备份列表"),
            ("export/delete", self.handle_export_delete, ["POST"], "MemeNest 删除备份"),
            ("overview", self.handle_overview, ["GET"], "MemeNest 大盘概览"),
            ("reanalyze", self.handle_reanalyze, ["POST"], "MemeNest 重新识图"),
            ("reanalyze/state", self.handle_reanalyze_state, ["GET"], "MemeNest 重识进度"),
            ("integrity", self.handle_integrity, ["GET"], "MemeNest 完整性体检"),
            ("integrity/vacuum", self.handle_integrity_vacuum, ["POST"], "MemeNest 整理数据库"),
            ("integrity/orphans", self.handle_integrity_orphans, ["POST"], "MemeNest 清理孤儿文件"),
            ("integrity/missing", self.handle_integrity_missing, ["POST"], "MemeNest 删除缺失记录"),
            ("export/images", self.handle_export_images, ["POST"], "MemeNest 导出选中图片"),
            ("sends/history", self.handle_sends_history, ["GET"], "MemeNest 发送历史"),
            ("reindex", self.handle_reindex, ["POST"], "MemeNest 向量索引"),
            ("import/process", self.handle_import_process, ["POST"], "MemeNest 处理已上传文件"),
            ("import/file", self.handle_import_file, ["POST"], "MemeNest 文件导入"),
        )
        for endpoint, handler, methods, desc in routes:
            register(f"{ROUTE_PREFIX}/{endpoint}", handler, methods, desc)
        logger.info("[MemeNest] 面板 API 已注册 routes=%s", len(routes))
        return True

    # -- 读取 -------------------------------------------------------------

    # -- 大盘 / 体检 / 重新识图 -------------------------------------------

    async def handle_overview(self) -> Any:
        """一次性返回大盘要的所有数据（库况、检索、发送、存储、健康告警）。"""
        store = self.plugin.store
        stats = await asyncio.to_thread(store.stats)
        labels = {key: 0 for key in LABEL_KEYS}
        for key, count in (await asyncio.to_thread(store.labels_in_use)).items():
            labels[key] = count
        origins = await asyncio.to_thread(store.origin_stats, 8)
        undescribed = await asyncio.to_thread(store.count_without_description)
        integrity = await self.plugin.integrity()
        vision = self.plugin.vision_budget_state()
        vector = await asyncio.to_thread(self.plugin.vector_state)
        dispatcher = self.plugin.dispatcher
        recent = dispatcher.recent_sends() if dispatcher is not None else []
        rejected = dispatcher.reject_stats() if dispatcher is not None else {}
        pipeline = self.plugin.pipeline
        counters = self.plugin.counters()
        library_cap = self.plugin.library_cap()
        model_ok = self.plugin.model_available()

        warnings: list[dict[str, str]] = []
        if not model_ok:
            warnings.append(
                {
                    "level": "error",
                    "text": "没有可用的视觉模型：新收的图会以兜底类入库、没有描述",
                }
            )
        budget_full = vision["limit"] > 0 and vision["used"] >= vision["limit"]
        if budget_full:
            warnings.append(
                {
                    "level": "warn",
                    "text": f"今日识图预算已用完（{vision['used']}/{vision['limit']}），"
                    "新收的图暂不打标，次日自动恢复",
                }
            )
        no_model = int(counters.get("缺模型", 0))
        if no_model:
            warnings.append(
                {
                    "level": "warn",
                    "text": f"有 {no_model} 条是在没有视觉模型时入库的，可点「重新识图」补描述",
                }
            )
        if undescribed:
            warnings.append(
                {
                    "level": "warn",
                    "text": f"库里有 {undescribed} 条没有描述（检索不到，也看不出内容）",
                }
            )
        if integrity["missing_count"]:
            warnings.append(
                {
                    "level": "error",
                    "text": f"{integrity['missing_count']} 条记录的文件已不在磁盘上，建议删除这些记录",
                }
            )
        if integrity["orphan_count"]:
            warnings.append(
                {
                    "level": "warn",
                    "text": f"{integrity['orphan_count']} 个文件没有被任何记录引用（只占空间）",
                }
            )
        if library_cap and stats.total >= int(library_cap * 0.9):
            warnings.append(
                {
                    "level": "warn",
                    "text": f"库容量接近上限（{stats.total}/{library_cap}），"
                    "到顶后会按「用得最少」淘汰",
                }
            )
        if vector.get("enabled") and vector.get("expected", 0) > vector.get("covered", 0):
            warnings.append(
                {
                    "level": "info",
                    "text": f"向量索引未补齐（{vector.get('covered', 0)}/{vector.get('expected', 0)}），"
                    "可在表情管理页点「补齐」",
                }
            )

        return json_response(
            {
                "ok": True,
                "library": {
                    "total": stats.total,
                    "ready": stats.ready,
                    "inbox": stats.inbox,
                    "disabled": stats.disabled,
                    "blocked": stats.blocked,
                    "undescribed": undescribed,
                    "cap": library_cap,
                    "labels": labels,
                    "origins": origins,
                },
                "vector": vector,
                "vision": {
                    "available": model_ok,
                    "used": vision["used"],
                    "limit": vision["limit"],
                    "no_model": no_model,
                },
                "send": {
                    "attempts": dispatcher.stats.attempts if dispatcher is not None else 0,
                    "sent": dispatcher.stats.sent if dispatcher is not None else 0,
                    "failed": dispatcher.stats.failed if dispatcher is not None else 0,
                    "cancelled": dispatcher.stats.cancelled if dispatcher is not None else 0,
                    "rejected": rejected,
                    "recent": recent[:5],
                },
                "storage": {
                    "db": integrity["db"]["db"],
                    "wal": integrity["db"]["wal"],
                    "dirs": integrity["dirs"],
                    "total_bytes": integrity["total_bytes"],
                    "missing": integrity["missing_count"],
                    "orphans": integrity["orphan_count"],
                },
                "queue_depth": self.plugin.queue_depth(),
                "reanalyze": self.plugin.reanalyze_state(),
                "counters": counters,
                "warnings": warnings,
            }
        )

    async def handle_integrity(self) -> Any:
        report = await self.plugin.integrity()
        return json_response({"ok": True, **report})

    async def handle_integrity_vacuum(self) -> Any:
        result = await self.plugin.integrity_vacuum()
        logger.info(
            "[MemeNest] 数据库整理完成 saved=%s bytes", result.get("saved")
        )
        return json_response({"ok": True, **result})

    async def handle_integrity_orphans(self) -> Any:
        """POST {paths?} → 删除孤儿文件；不传 paths 就按当前对账结果全删。"""
        body = await request.json(default={})
        raw = body.get("paths")
        paths = [str(item) for item in raw] if isinstance(raw, list) else None
        result = await self.plugin.integrity_drop_orphans(paths)
        logger.info(
            "[MemeNest] 清理孤儿文件 removed=%s failed=%s",
            result.get("removed"),
            result.get("failed"),
        )
        return json_response({"ok": True, **result})

    async def handle_integrity_missing(self) -> Any:
        """POST → 删掉「库里有记录、磁盘没有文件」的条目（不可逆，先重新对账）。"""
        result = await self.plugin.integrity_drop_missing()
        logger.info(
            "[MemeNest] 删除缺失文件记录 removed=%s/%s",
            result.get("removed"),
            result.get("scanned"),
        )
        return json_response({"ok": True, **result})

    async def handle_export_images(self) -> Any:
        """POST {ids} → 把选中的图片打成一个 zip，直接回 base64 让页面下载。

        不走「生成到 exports/ 再让用户去取」那条路：导出选中通常是几十张，
        十几 MB 在面板里下载比让用户去服务器翻目录顺手得多。
        """
        body = await request.json(default={})
        raw = body.get("ids")
        ids = [int(item) for item in raw] if isinstance(raw, list) else []
        if not ids:
            return _fail("没有选中条目")
        ids = ids[: core.MAX_BATCH_IDS]
        paths = await asyncio.to_thread(self.plugin.store.rel_paths_for, ids)
        if not paths:
            return _fail("选中的条目已经不存在了")
        try:
            result = await asyncio.to_thread(
                backup.build_selection_zip, self.plugin.data_dir, paths
            )
        except backup.SelectionTooLarge as exc:
            return _fail(
                f"选中的图片打包后有 {int(exc.args[0]) // (1024 * 1024)} MB，"
                "超过单次导出上限，请分批导出"
            )
        name = backup.selection_name(datetime.now())
        encoded = base64.b64encode(result["data"]).decode("ascii")
        logger.info(
            "[MemeNest] 导出选中图片 name=%s count=%s bytes=%s skipped=%s",
            name,
            result["included"],
            len(result["data"]),
            result["skipped"],
        )
        return json_response(
            {
                "ok": True,
                "name": name,
                "count": result["included"],
                "skipped": result["skipped"],
                "bytes": len(result["data"]),
                "base64": encoded,
            }
        )

    async def handle_reanalyze(self) -> Any:
        body = await request.json(default={})
        scope = str(body.get("scope") or "empty")
        raw_ids = body.get("ids")
        ids = [int(item) for item in raw_ids] if isinstance(raw_ids, list) else None
        if scope not in ("empty", "ids"):
            return _fail("scope 只能是 empty 或 ids")
        if scope == "ids" and not ids:
            return _fail("没有选中条目")
        if not self.plugin.start_reanalyze(scope, ids):
            return _fail("已经在重新识图了")
        return json_response({"ok": True, "started": True, "scope": scope})

    async def handle_reanalyze_state(self) -> Any:
        scope = str(request.query.get("scope") or "empty")
        return json_response({"ok": True, **self.plugin.reanalyze_state(scope)})

    async def handle_stats(self) -> Any:
        store = self.plugin.store
        stats = await asyncio.to_thread(store.stats)
        personas = await asyncio.to_thread(store.personas)
        # 配置里存在但库里还没用过的人格也要列出来，数量记 0
        for persona_id in await self._available_personas():
            personas.setdefault(persona_id, 0)
        # "通用" 在 store 里归一成空键，面板按「全部 / （通用）」两个哨兵自己筛
        personas.pop("", None)
        personas = dict(sorted(personas.items(), key=lambda item: (-item[1], item[0])))
        vector: dict[str, Any] = {}
        getter = getattr(self.plugin, "vector_state", None)
        if getter is not None:
            # vector_state 自己已经处理了「查询失败」的降级，这里不再套一层 try：
            # 真正的异常应该冒到面板上，而不是让向量卡片静默变空。
            vector = await asyncio.to_thread(getter)
        # 标签体检：21 类全量计数（主标签 + 附加标签），零值也要给出来——
        # 哪些分类一直没被用到，比总量更能说明打标质量。
        labels = {key: 0 for key in LABEL_KEYS}
        for key, count in (await asyncio.to_thread(store.labels_in_use)).items():
            labels[key] = count
        # 来源统计 + 识图预算：面板「库况」与预算黄条的数据源
        origins = await asyncio.to_thread(store.origin_stats, 10)
        vision = self.plugin.vision_budget_state()
        return json_response(
            {
                "vector": vector,
                "ok": True,
                "total": stats.total,
                "ready": stats.ready,
                "inbox": stats.inbox,
                "disabled": stats.disabled,
                "blocked": stats.blocked,
                "by_label": stats.by_label,
                "labels": labels,
                "origins": origins,
                "vision_budget": vision,
                "manifest_limit": core.MANIFEST_MAX_ITEMS,
                "personas": personas,
                "counters": self.plugin.counters(),
                "queue_depth": self.plugin.queue_depth(),
                "model_available": self.plugin.model_available(),
                "library_cap": self.plugin.library_cap(),
                "import_concurrency": self.plugin.import_concurrency(),
            }
        )

    async def _available_personas(self) -> list[str]:
        """宿主里配置的人格 id（DB 的 persona_id + v3 的 name）。

        只看库里"已经绑定过"的人格是不够的：新用户在面板里选不到任何选项。
        """
        manager = getattr(self.plugin.context, "persona_manager", None)
        if manager is None:
            return []
        found: list[str] = []
        getter = getattr(manager, "get_all_personas", None)
        if callable(getter):
            try:
                for item in await getter() or []:
                    value = str(getattr(item, "persona_id", "") or "").strip()
                    # 宿主用 * 表示「所有人格」，那是内部通配符，不能当成人格列出来
                    if value and value != GENERIC_PERSONA and value not in found:
                        found.append(value)
            except Exception as exc:  # noqa: BLE001 - 面板不该因为读人格失败而空
                logger.debug("[MemeNest] 读取 DB 人格失败 type=%s", type(exc).__name__)
        for item in getattr(manager, "personas_v3", None) or []:
            value = str((item or {}).get("name") or "").strip()
            if value and value != GENERIC_PERSONA and value not in found:
                found.append(value)
        return found

    async def handle_list(self) -> Any:
        args = core.parse_page_args(request.query.get)
        memos, total = await self._page(args)
        return json_response(
            {
                "ok": True,
                "page": args.page,
                "size": args.size,
                "total": total,
                "items": [core.serialize_memo(memo) for memo in memos],
            }
        )

    async def _page(self, args: core.PageArgs) -> tuple[list[Any], int]:
        store = self.plugin.store
        if not args.keyword and not args.persona:
            memos, total = await asyncio.to_thread(
                store.page,
                PageQuery(state=args.state, label=args.label, page=args.page, size=args.size),
            )
            return memos, total
        # 关键词/人格只能内存过滤：先把条件的池子捞出来，过滤后再切页，保证总数准确。
        pool: list[Any] = []
        page_no = 1
        while len(pool) < FILTER_SCAN_LIMIT:
            batch, _ = await asyncio.to_thread(
                store.page,
                PageQuery(
                    state=args.state,
                    label=args.label,
                    page=page_no,
                    size=core.PAGE_SIZE_MAX,
                ),
                with_total=False,
            )
            pool.extend(batch)
            if len(batch) < core.PAGE_SIZE_MAX:
                break
            page_no += 1
        filtered = core.filter_memos(pool, args.keyword, args.persona)
        start = (args.page - 1) * args.size
        return filtered[start : start + args.size], len(filtered)

    async def handle_thumb(self) -> Any:
        memo_id = core.clamp_ids(request.query.get("id", 0))
        if not memo_id:
            return json_response({"ok": False, "error": "bad_id"})
        memo = await asyncio.to_thread(self.plugin.store.get, memo_id[0])
        if memo is None:
            return json_response({"ok": False, "error": "not_found"})
        source = resolve_within(Path(self.plugin.data_dir), memo.rel_path)
        if source is None:
            return json_response({"ok": False, "error": "bad_path"})
        try:
            mtime = source.stat().st_mtime_ns
        except OSError:
            return json_response({"ok": False, "error": "file_missing"})
        cached = self._thumbs.get(memo.rel_path, mtime)
        if cached is not None:
            return json_response(core.thumb_payload(cached))
        data = await asyncio.to_thread(core.thumb_bytes, source)
        if data is None:
            return json_response({"ok": False, "error": "decode_failed"})
        self._thumbs.put(memo.rel_path, mtime, data)
        return json_response(core.thumb_payload(data))

    async def handle_update(self) -> Any:
        """人工修订：描述、标签、附加分类、主分类；没传的字段不动。"""
        body = await request.json(default={})
        ids = core.clamp_ids(body.get("id"), 1)
        if not ids:
            return _fail("缺少 id")
        memo_id = ids[0]

        caption = core.clamp_text(body.get("caption")) if "caption" in body else None
        text_in_image = (
            core.clamp_text(body.get("text_in_image"), MAX_TEXT_IN_IMAGE)
            if "text_in_image" in body
            else None
        )
        scenes = None
        if "scenes" in body:
            # 一行一个话题词，和打标输出同一套上限
            raw = [
                item.strip()
                for item in str(body.get("scenes") or "").splitlines()
                if item.strip()
            ]
            too_long = [item for item in raw if len(item) > MAX_SCENE_CHARS]
            if too_long:
                # 这里不静默截断：6 字上限下切出来是半截废词，用户也看不出被剪过
                return _fail(f"每个话题词最多 {MAX_SCENE_CHARS} 字：{too_long[0]}")
            scenes = clean_topics(raw)
        labels = None
        if "labels" in body:
            # 附加标签也必须是 21 类之一：不校验就能把任意字符串写进关联表，
            # 显示时兜底成「其他」、筛选里查不到，属于纯静默的数据污染。
            raw = core.split_terms(body.get("labels"), 3) or []
            labels = [item for item in raw if item in LABEL_KEYS]
        label = None
        if "label" in body:
            candidate = str(body.get("label") or "").strip().lower()
            if candidate not in LABEL_KEYS:
                return _fail("分类不合法")
            label = candidate

        store = self.plugin.store
        memo = await asyncio.to_thread(store.get, memo_id)
        if memo is None:
            return _fail("没有这条记录")
        if labels is not None:
            effective = label if label is not None else memo.label
            labels = [item for item in labels if item != effective][:3]
        updated = await asyncio.to_thread(
            store.update_analysis,
            memo_id,
            caption=caption,
            text_in_image=text_in_image,
            scenes=scenes,
            labels=labels,
            label=label,
        )
        if not updated:
            return _fail("写库失败")
        logger.debug("[MemeNest] 编辑条目 id=%s", memo_id)
        return json_response({"ok": True, "id": memo_id})

    async def handle_state(self) -> Any:
        """切换状态：待审核 / 可用 / 禁用（单条与批量同一套逻辑）。

        目录跟着状态走——待审核放 inbox/，其余放 blobs/，移库失败会回滚。
        """
        body = await request.json(default={})
        action = str(body.get("action") or "").strip().lower()
        if action:
            return await self._bulk_by_action(action)

        raw_ids = body.get("ids") if "ids" in body else body.get("id")
        ids = core.clamp_ids(raw_ids)
        if not ids:
            return _fail("缺少 id")
        wanted = str(body.get("state") or "").strip().lower()
        if wanted not in core.ALLOWED_STATES:
            return _fail("状态不合法")

        async def switch(memo_id: int) -> tuple[bool, str]:
            return await self._switch_state(memo_id, wanted)

        return await self._batch(ids, switch)

    async def _bulk_by_action(self, action: str) -> Any:
        """按当前状态整体转换：审核 = 待审核→可用，禁用 = 可用→禁用，启用 = 禁用→可用。

        不看分页、不看勾选——这几个动作的语义就是「把这一类全处理掉」。
        """
        mapping = {
            "review": (STATE_INBOX, STATE_READY),
            "disable": (STATE_READY, STATE_DISABLED),
            "enable": (STATE_DISABLED, STATE_READY),
        }
        if action not in mapping:
            return _fail("动作不合法")
        source, target = mapping[action]
        store = self.plugin.store
        rows = await asyncio.to_thread(store.rows_in_state, source)
        if not rows:
            return _fail("没有需要处理的条目")

        # 目录跟着状态走：三个动作的目标状态都在 blobs/ 下（inbox 只对应「待审核」，
        # 批量动作永远不会把人送回去），所以只需把还躺在 inbox/ 的文件挪出来。
        data_dir = Path(self.plugin.data_dir)
        month = time.strftime("%Y%m")
        moves: dict[int, str] = {}
        failed: list[dict[str, Any]] = []
        moved: list[tuple[Path, Path]] = []
        for memo_id, rel_path in rows:
            source_path = resolve_within(data_dir, rel_path)
            if source_path is None:
                failed.append({"id": memo_id, "reason": "记录路径非法"})
                continue
            if not rel_path.startswith("inbox/"):
                continue  # 目录已经对，只改状态
            new_rel = f"blobs/{month}/{source_path.name}"
            target_path = resolve_within(data_dir, new_rel)
            if target_path is None:
                failed.append({"id": memo_id, "reason": "目标路径非法"})
                continue
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.replace(target_path)
            except OSError:
                failed.append({"id": memo_id, "reason": "移动文件失败"})
                continue
            moves[memo_id] = new_rel
            moved.append((source_path, target_path))

        ok_ids = [memo_id for memo_id, _ in rows if all(
            f["id"] != memo_id for f in failed
        )]
        try:
            await asyncio.to_thread(store.set_state_bulk, ok_ids, target, moves)
        except Exception as exc:
            # 写库失败要把已经挪走的文件挪回去，否则库里路径和磁盘对不上
            for source_path, target_path in moved:
                try:
                    target_path.replace(source_path)
                except OSError:
                    pass
            logger.error(
                "[MemeNest] 批量转换写库失败 type=%s", type(exc).__name__
            )
            return _fail("写库失败")

        logger.debug(
            "[MemeNest] 面板批量转换 ok=%s failed=%s", len(ok_ids), len(failed)
        )
        return json_response({"ok": True, "succeeded": ok_ids, "failed": failed})

    async def _switch_state(self, memo_id: int, wanted: str) -> tuple[bool, str]:
        store = self.plugin.store
        memo = await asyncio.to_thread(store.get, memo_id)
        if memo is None:
            return False, "没有这条记录"

        data_dir = Path(self.plugin.data_dir)
        source = resolve_within(data_dir, memo.rel_path)
        if source is None:
            return False, "记录路径非法"

        want_inbox = wanted == STATE_INBOX
        in_inbox = memo.rel_path.startswith("inbox/")
        if want_inbox == in_inbox:
            # 目录已经对，只改状态
            ok = await asyncio.to_thread(store.set_state, memo_id, wanted, None)
            return (True, "") if ok else (False, "写库失败")

        month = time.strftime("%Y%m")
        name = source.name
        rel_target = f"inbox/{name}" if want_inbox else f"blobs/{month}/{name}"
        target = resolve_within(data_dir, rel_target)
        if target is None:
            return False, "目标路径非法"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
        except OSError:
            return False, "移动文件失败"
        ok = await asyncio.to_thread(store.set_state, memo_id, wanted, rel_target)
        if not ok:
            target.replace(source)
            return False, "写库失败"
        return True, ""

    async def handle_label(self) -> Any:
        """批量把选中条目的主分类改成同一个值。"""
        body = await request.json(default={})
        ids = core.clamp_ids(body.get("ids"))
        if not ids:
            return _fail("缺少 ids")
        label = str(body.get("label") or "").strip().lower()
        if label not in LABEL_KEYS:
            return _fail("分类不合法")
        store = self.plugin.store

        async def action(memo_id: int) -> tuple[bool, str]:
            ok = await asyncio.to_thread(store.update_analysis, memo_id, label=label)
            return (True, "") if ok else (False, "写库失败")

        return await self._batch(ids, action)

    async def handle_blocked(self) -> Any:
        args = core.parse_page_args(request.query.get)
        store = self.plugin.store
        entries = await asyncio.to_thread(store.blocked_page, args.size, args.offset)
        total = await asyncio.to_thread(store.blocked_count)
        return json_response(
            {
                "ok": True,
                "page": args.page,
                "size": args.size,
                "total": total,
                "items": [
                    {"hash": digest, "created_at": created, "has_thumb": has_thumb}
                    for digest, created, has_thumb in entries
                ],
            }
        )

    async def handle_blocked_thumb(self) -> Any:
        """GET ?hash=... → 黑名单条目的缩略图（base64）。老数据没有就返回空。"""
        digest = str(request.query.get("hash") or "").strip()
        if not digest:
            return json_response({"ok": True, "has_thumb": False})
        data = await asyncio.to_thread(
            self.plugin.store.blocked_thumb, digest
        )
        if not data:
            return json_response({"ok": True, "has_thumb": False})
        return json_response(
            {
                "ok": True,
                "has_thumb": True,
                "mime": "image/jpeg",
                "image": base64.b64encode(data).decode("ascii"),
            }
        )

    # -- 写操作 -----------------------------------------------------------

    async def handle_preview(self) -> Any:
        """看大图：静态图给大尺寸 JPEG，动图给原文件。"""
        ids = core.clamp_ids(request.query.get("id", 0), 1)
        if not ids:
            return _fail("缺少 id")
        store = self.plugin.store
        memo = await asyncio.to_thread(store.get, ids[0])
        if memo is None:
            return _fail("没有这条记录")
        source = resolve_within(Path(self.plugin.data_dir), memo.rel_path)
        if source is None:
            return _fail("记录路径非法")
        try:
            mtime = source.stat().st_mtime_ns
        except OSError:
            return _fail("图片读取失败")

        cached = self._previews.get(memo.rel_path, mtime)
        if cached is None:
            async with self._preview_slots:
                # 排队期间别人可能已经算完同一条，进锁后再看一次
                cached = self._previews.get(memo.rel_path, mtime)
                if cached is None:
                    got = await asyncio.to_thread(core.preview_bytes, source)
                    if got is None:
                        return _fail("图片读取失败")
                    data, mime = got
                    self._previews.put(memo.rel_path, mtime, data, mime)
                    cached = (data, mime)
        data, mime = cached
        return json_response(
            {
                "ok": True,
                "mime": mime,
                "image": base64.b64encode(data).decode("ascii"),
            }
        )

    async def handle_delete(self) -> Any:
        body = await request.json(default={})
        ids = core.clamp_ids(body.get("ids"))
        return await self._batch(ids, lambda memo_id: self.plugin.remove_memo(memo_id))

    async def handle_ban(self) -> Any:
        body = await request.json(default={})
        ids = core.clamp_ids(body.get("ids"))
        return await self._batch(
            ids, lambda memo_id: self.plugin.remove_memo(memo_id, block=True)
        )

    async def handle_fav(self) -> Any:
        body = await request.json(default={})
        ids = core.clamp_ids(body.get("ids") or body.get("id"))
        if not ids:
            return _fail("缺少 id")
        wanted = body.get("fav")
        if not isinstance(wanted, bool):
            return _fail("fav 必须是布尔值")

        async def action(memo_id: int) -> tuple[bool, str]:
            ok = await asyncio.to_thread(self.plugin.store.set_fav, memo_id, wanted)
            return ok, "not_found"

        return await self._batch(ids, action)

    async def handle_scope(self) -> Any:
        body = await request.json(default={})
        ids = core.clamp_ids(body.get("ids") or body.get("id"))
        if not ids:
            return _fail("缺少 id")
        scope = str(body.get("scope") or "").strip().lower()
        if scope not in core.ALLOWED_SCOPES:
            return _fail("scope 只能是 any 或 origin")

        async def action(memo_id: int) -> tuple[bool, str]:
            ok = await asyncio.to_thread(self.plugin.store.set_scope, memo_id, scope)
            return ok, "not_found"

        return await self._batch(ids, action)

    async def handle_bind(self) -> Any:
        """POST {ids, persona_ids?, block_persona_ids?} → 改可用名单 / 禁用人格。

        两个字段各自独立：传了才改，都不传等于没操作。
        """
        body = await request.json(default={})
        ids = core.clamp_ids(body.get("ids") or body.get("id"))
        if not ids:
            return _fail("缺少 id")
        allow, block = core.persona_params(body)
        if allow is None and block is None:
            return _fail("没有要改的人格设置")

        async def action(memo_id: int) -> tuple[bool, str]:
            ok = True
            if allow is not None:
                ok = await asyncio.to_thread(
                    self.plugin.store.set_personas, memo_id, allow
                )
            if block is not None:
                ok = (
                    await asyncio.to_thread(
                        self.plugin.store.set_persona_blocks, memo_id, block
                    )
                    and ok
                )
            return ok, "not_found"

        return await self._batch(ids, action)

    async def handle_unban(self) -> Any:
        body = await request.json(default={})
        digest = str(body.get("hash") or body.get("prefix") or "").strip().lower()
        if len(digest) < 8:
            return _fail("指纹前缀至少 8 位")
        removed = await asyncio.to_thread(self.plugin.store.unblock_prefix, digest)
        self._thumbs.clear()
        return json_response({"ok": True, "removed": removed})

    async def _batch(self, ids: list[int], action: Any) -> Any:
        if not ids:
            return _fail("缺少 ids")
        succeeded: list[int] = []
        failed: list[dict[str, Any]] = []
        for memo_id in ids:
            try:
                ok, reason = await action(memo_id)
            except Exception as exc:
                ok, reason = False, type(exc).__name__
            if ok:
                succeeded.append(memo_id)
            else:
                failed.append({"id": memo_id, "reason": reason})
        # 不清缩略图缓存：兜里的 key 是 (rel_path, mtime)，文件一挪路径就变了，
        # 旧条目自然 miss。全清反而让前端把整页缩略图重新解码一遍。
        logger.debug("[MemeNest] 面板批量操作 ok=%s failed=%s", len(succeeded), len(failed))
        return json_response({"ok": True, "succeeded": succeeded, "failed": failed})

    # -- 导入 / 导出 --------------------------------------------------------

    async def handle_export_create(self) -> Any:
        """POST → 生成一份完整备份（db 快照 + blobs/inbox），返回路径与列表。

        打包期间占位：两个页面（或连点两下）同时进来会撞同一个文件名、
        两个线程写同一个 zip，结果是坏包。
        """
        if self._exporting:
            return _fail("正在打包，请稍候")
        self._exporting = True
        try:
            path = await asyncio.to_thread(
                backup.create_backup,
                Path(self.plugin.data_dir),
                self.plugin.store.db_path,
            )
        finally:
            self._exporting = False
        items = await asyncio.to_thread(
            backup.list_exports, Path(self.plugin.data_dir)
        )
        return json_response(
            {
                "ok": True,
                "name": path.name,
                "size": path.stat().st_size,
                "exports": items,
                "directory": str(path.parent),
                "keep": backup.EXPORT_KEEP,
            }
        )

    async def handle_export_list(self) -> Any:
        items = await asyncio.to_thread(
            backup.list_exports, Path(self.plugin.data_dir)
        )
        directory = backup.exports_dir(Path(self.plugin.data_dir))
        return json_response(
            {
                "ok": True,
                "exports": items,
                "directory": str(directory),
                "keep": backup.EXPORT_KEEP,
            }
        )

    async def handle_export_delete(self) -> Any:
        body = await request.json(default={})
        name = str(body.get("name") or "")
        ok = await asyncio.to_thread(
            backup.delete_export, Path(self.plugin.data_dir), name
        )
        if not ok:
            return _fail("没有这个备份文件")
        items = await asyncio.to_thread(
            backup.list_exports, Path(self.plugin.data_dir)
        )
        return json_response(
            {"ok": True, "exports": items, "keep": backup.EXPORT_KEEP}
        )

    async def handle_sends_history(self) -> Any:
        """发送历史（持久化）：分页 + 按检索路筛选 + 搜触发文本。"""
        page = max(1, core._as_int(request.query.get("page"), 1))
        size = max(
            1, min(core.PAGE_SIZE_MAX, core._as_int(request.query.get("size"), 20))
        )
        via = str(request.query.get("via") or "").strip()
        query = str(request.query.get("q") or "").strip()
        items, total = await asyncio.to_thread(
            self.plugin.store.page_send_logs,
            page=page,
            size=size,
            via=via,
            query=query,
        )
        return json_response(
            {
                "ok": True,
                "items": items,
                "total": total,
                "page": page,
                "size": size,
                "days": SEND_LOG_DAYS,
                "keep": SEND_LOG_MAX,
            }
        )

    async def handle_search_preview(self) -> Any:
        """POST {text, context_text?} → 两条检索路各自的 top 候选（dry-run）。"""
        body = await request.json(default={})
        text = str(body.get("text") or "")
        context_text = str(body.get("context_text") or "")
        dispatcher = self.plugin.dispatcher
        if dispatcher is None:
            return _fail("发送器未初始化")
        result = await dispatcher.preview(text, context_text)
        return json_response({"ok": True, **result})

    async def handle_reindex(self) -> Any:
        """POST {full} → 后台补算向量。full=true 先清空再全量重算。"""
        body = await request.json(default={})
        full = body.get("full") is True
        runner = getattr(self.plugin, "run_reindex", None)
        reserve = getattr(self.plugin, "try_begin_reindex", None)
        if runner is None or reserve is None:
            return _fail("当前版本不支持重建")
        # 先同步占位再起任务：否则「检查 running」和「任务真正开始」之间能被连点两下穿过
        if not reserve():
            return _fail("已经在补算了")
        asyncio.create_task(
            runner(full=full, reserved=True), name="memenest-reindex"
        )
        return json_response({"ok": True, "started": True, "full": full})

    async def handle_import(self) -> Any:
        """POST {url, persona_id?, scope?, umo?} → 下载并处理一条 URL。

        单条同步：前端逐条发，拿到结果再发下一条，进度条才是真实进度。
        """
        body = await request.json(default={})
        url = str(body.get("url") or "").strip()
        if not url:
            return _fail("没有提供 url")
        allowed, reason = security.check_url(url)
        if not allowed:
            return json_response({"ok": True, "outcome": "rejected", "reason": reason})

        try:
            scope, umo, persona_id = core.import_params(body)
        except ValueError as exc:
            return _fail(str(exc))

        ref = MediaRef(
            source=url,
            declared_ext="",
            origin_kind=ORIGIN_IMPORT,
            platform="panel",
            reason="import_url",
        )
        ctx = IngestContext(
            umo=umo,
            origin_kind=ORIGIN_IMPORT,
            scope=scope,
            persona_id=persona_id,
        )
        result = await self.plugin.pipeline.process_one(ref, ctx)
        logger.info(
            "[MemeNest] 面板 URL 导入 outcome=%s persona=%s",
            result.get("outcome"),
            bool(persona_id),
        )
        return json_response({"ok": True, **result})

    async def handle_import_manifest(self) -> Any:
        """POST {url, persona_id?, scope?, umo?} → 拉 JSON 清单并整批入队。

        清单是图片 URL 数组，或 `{"items": [...]}`（项可为字符串或 `{"url": ...}`）。
        只入队不等结果：进度看「队列/统计」，单条失败按失败计。
        """
        body = await request.json(default={})
        url = str(body.get("url") or "").strip()
        if not url:
            return _fail("没有提供清单 url")
        allowed, reason = security.check_url(url)
        if not allowed:
            return json_response({"ok": True, "outcome": "rejected", "reason": reason})
        try:
            scope, umo, persona_id = core.import_params(body)
        except ValueError as exc:
            return _fail(str(exc))

        data, error = await fetch_json(url)
        if error:
            return _fail(f"清单读取失败（{error}）")
        urls = core.manifest_urls(data)
        if not urls:
            return _fail("清单里没有可用的图片 URL")

        refs = [
            MediaRef(
                source=item,
                declared_ext="",
                origin_kind=ORIGIN_IMPORT,
                platform="panel",
                reason="import_manifest",
            )
            for item in urls
        ]
        ctx = IngestContext(
            umo=umo,
            origin_kind=ORIGIN_IMPORT,
            scope=scope,
            persona_id=persona_id,
        )
        result = self.plugin.pipeline.submit(refs, ctx)
        logger.info(
            "[MemeNest] 清单导入 queued=%s dropped=%s total=%s",
            result.queued,
            result.dropped,
            len(urls),
        )
        return json_response(
            {
                "ok": True,
                "queued": result.queued,
                "dropped": result.dropped,
                "total": len(urls),
            }
        )

    async def handle_import_file(self) -> Any:
        """POST multipart（字段名 file）→ 只接收文件，识别交给 import/process。"""
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return _fail("缺少文件")
        _, ext = core.normalize_upload_name(upload.filename)
        if ext is None:
            return _fail("只支持 png/jpg/gif/webp/bmp")
        if upload.content_length and upload.content_length > MAX_IMPORT_BYTES:
            return _fail("文件超过 32 MiB")

        imports_dir = Path(self.plugin.data_dir) / IMPORTS_SUBDIR
        imports_dir.mkdir(parents=True, exist_ok=True)
        target = imports_dir / f"{uuid.uuid4().hex}{ext}"
        try:
            # 交给宿主边写边卡上限：content_length 缺失时（分块上传）也能在超限的
            # 那一刻停下并删掉半截文件，不会先把磁盘写满再回头检查。
            await upload.save(target, max_bytes=MAX_IMPORT_BYTES)
        except Exception as exc:
            target.unlink(missing_ok=True)
            return _fail(f"保存失败：{type(exc).__name__}")
        try:
            if target.stat().st_size > MAX_IMPORT_BYTES:
                target.unlink(missing_ok=True)
                return _fail("文件超过 32 MiB")
        except OSError:
            return _fail("保存失败")

        logger.info("[MemeNest] 面板上传收文件 ext=%s", ext)
        # 只负责收文件，识别交给下一步：前端先显示「上传中」，再显示「处理中」
        return json_response({"ok": True, "job": target.stem, "ext": ext})

    async def handle_import_process(self) -> Any:
        """POST {job, ext, persona_id?, scope?, umo?} → 识别并入库那一张。

        与上传分开，前端才能把「上传中」和「处理中」分成两个阶段显示。
        """
        body = await request.json(default={})
        job = str(body.get("job") or "").strip()
        ext = str(body.get("ext") or "").strip().lower()
        if not core.is_upload_job(job) or ext not in core.ALLOWED_UPLOAD_EXTS:
            return _fail("文件标识不合法")

        # 必须自己拼正斜杠相对路径：Path.relative_to() 在 Windows 返回反斜杠，
        # 而 resolve_within 拒绝反斜杠，绕这一圈会让所有上传都「找不到文件」。
        resolved = resolve_within(
            Path(self.plugin.data_dir), f"{IMPORTS_SUBDIR}/{job}{ext}"
        )
        if resolved is None or not resolved.is_file():
            return _fail("找不到上传的文件")

        try:
            scope, umo, persona_id = core.import_params(body)
        except ValueError as exc:
            resolved.unlink(missing_ok=True)
            return _fail(str(exc))

        ref = MediaRef(
            source=str(resolved),
            declared_ext=ext,
            origin_kind=ORIGIN_IMPORT,
            platform="panel",
            reason="import_upload",
        )
        ctx = IngestContext(
            umo=umo,
            origin_kind=ORIGIN_IMPORT,
            scope=scope,
            persona_id=persona_id,
        )
        try:
            result = await self.plugin.pipeline.process_one(ref, ctx)
        finally:
            resolved.unlink(missing_ok=True)
        logger.info(
            "[MemeNest] 面板上传处理 outcome=%s bound=%s",
            result.get("outcome"),
            bool(persona_id),
        )
        return json_response({"ok": True, **result})

def _fail(message: str) -> Any:
    """校验失败用 200 + ok=false 返回：宿主对 4xx 只给状态码，具体原因会丢。"""
    return json_response({"ok": False, "message": message})


__all__ = ["IMPORTS_SUBDIR", "PanelApi", "ROUTE_PREFIX"]
