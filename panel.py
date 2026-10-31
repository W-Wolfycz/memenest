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
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import PluginUploadFile, json_response, request

from . import panel_core as core
from .config import PLUGIN_NAME
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
            ("reindex", self.handle_reindex, ["POST"], "MemeNest 向量索引"),
            ("import/process", self.handle_import_process, ["POST"], "MemeNest 处理已上传文件"),
            ("import/file", self.handle_import_file, ["POST"], "MemeNest 文件导入"),
        )
        for endpoint, handler, methods, desc in routes:
            register(f"{ROUTE_PREFIX}/{endpoint}", handler, methods, desc)
        logger.info("[MemeNest] 面板 API 已注册 routes=%s", len(routes))
        return True

    # -- 读取 -------------------------------------------------------------

    async def handle_stats(self) -> Any:
        store = self.plugin.store
        stats = await asyncio.to_thread(store.stats)
        personas = await asyncio.to_thread(store.personas)
        # 配置里存在但库里还没用过的人格也要列出来，数量记 0
        for persona_id in await self._available_personas():
            personas.setdefault(persona_id, 0)
        personas.pop(GENERIC_PERSONA, None)
        personas = dict(sorted(personas.items(), key=lambda item: (-item[1], item[0])))
        vector: dict[str, Any] = {}
        getter = getattr(self.plugin, "vector_state", None)
        if getter is not None:
            try:
                vector = await asyncio.to_thread(getter)
            except Exception:
                vector = {}
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
        ids = await asyncio.to_thread(store.ids_in_state, source)
        if not ids:
            return _fail("没有需要处理的条目")

        async def act(memo_id: int) -> tuple[bool, str]:
            return await self._switch_state(memo_id, target)

        return await self._batch(ids, act)

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
        payload = await asyncio.to_thread(core.preview_payload, source)
        if payload is None:
            return _fail("图片读取失败")
        return json_response({"ok": True, **payload})

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
        body = await request.json(default={})
        ids = core.clamp_ids(body.get("ids") or body.get("id"))
        if not ids:
            return _fail("缺少 id")
        raw = body.get("persona_ids")
        if raw is None:
            raw = body.get("persona_id")
        if isinstance(raw, (list, tuple)):
            persona_ids = [str(item).strip() for item in raw if str(item).strip()]
        else:
            text = str(raw or "").strip()
            persona_ids = (
                [] if text.lower() in ("none", "clear", "", "-")
                else [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
            )

        async def action(memo_id: int) -> tuple[bool, str]:
            ok = await asyncio.to_thread(
                self.plugin.store.set_personas, memo_id, persona_ids
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
        if succeeded:
            self._thumbs.clear()
        logger.debug("[MemeNest] 面板批量操作 ok=%s failed=%s", len(succeeded), len(failed))
        return json_response({"ok": True, "succeeded": succeeded, "failed": failed})

    # -- 导入 -------------------------------------------------------------

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
            await upload.save(target)
        except Exception as exc:
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
