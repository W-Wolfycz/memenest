"""变异验证：逐条改坏被测逻辑，确认对应用例真的失败。

准入条件而非可选项——「删完/补完做变异验证」，不失败的用例等于没写。

用法（在插件父目录 `D:\\AI\\dsh\\plugin` 下）：

    & $py memenest\\scripts\\verify_mutations.py

脚本会先把插件复制到临时目录，在副本上做变异（不改动真源码，中途中断也安全），
然后对每条变异：应用 → 跑指定用例 → 判断是否真的失败 → 恢复。

两个坑：① 变异要真的改变行为（等价改写会让用例假通过）；
② 判「被拦住」必须确认用例真的跑起来了（收集错误会让退出码非 0）。
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable
PKG = ROOT.name

# 多行锚点抽成常量：直接内联会把行宽顶过 100，也让下面的变异表难读。
_BACKFILL_ANIMATED = (
    '            if int(getattr(image, "n_frames", 1)) > 1:\n'
    "                return animated_percept_hash(image)"
)
_PERCEPT_WITHIN_ALL = (
    "        return all(\n"
    "            hamming(a, b) <= threshold for a, b in zip(left_parts, right_parts)\n"
    "        )"
)
_RECENT_BY_LABEL_SQL = (
    '"SELECT id, label FROM memo WHERE used_at > 0 "\n'
    '                "ORDER BY used_at DESC LIMIT ?"'
)

MUTATIONS: list[tuple[str, str, str | list[str], str | list[str], str]] = [
    (
        "标签只认整串（模型多写一个词就丢成 other）",
        "domain.py",
        "    for token in _LABEL_TOKEN_SPLIT.split(text):",
        "    for token in []:",
        "tests/test_T09_parse.py::test_normalize_label_tolerates_chatty_output",
    ),
    (
        "纯色图也发指纹（所有纯色图互相判重）",
        "ingest/fingerprints.py",
        "    if bits == 0:",
        "    if False:",
        "tests/test_T06_T07_dedup.py::test_solid_image_yields_no_percept_hash",
    ),
    (
        "列表元素又被标点切碎（一条 scene 整条消失）",
        "ingest/analyze.py",
        "        raw_items = list(value)",
        "        raw_items = [part for item in value for part in _LIST_SPLIT.split(str(item))]",
        "tests/test_T09_parse.py::test_clean_list_keeps_list_items_intact",
    ),
    (
        "中文别名指向不存在的 key（非法标签入库）",
        "domain.py",
        '    "嫌弃": "disgust",',
        '    "嫌弃": "disgusted",',
        "tests/test_T09_parse.py::test_all_aliases_point_to_real_labels",
    ),
    (
        "指纹不裁边框（加白边的同图重复入库）",
        "ingest/fingerprints.py",
        "        gray = trim_border(image).convert(\"L\").resize(",
        "        gray = image.convert(\"L\").resize(",
        "tests/test_T06_T07_dedup.py::test_border_padded_copy_is_duplicate",
    ),
    (
        "回合守卫整组失效（同一回合发两张）",
        "publish/dispatcher.py",
        [
            "        if state.attempted:\n            return FireOutcome(Reason.TURN_USED)\n",
            "                turn_used=state.used,\n",
        ],
        [
            "        if False:\n            return FireOutcome(Reason.TURN_USED)\n",
            "                turn_used=False,\n",
        ],
        "tests/test_T13_T23_dispatcher.py::test_T14_T16_two_hook_calls_fire_only_once",
    ),
    (
        "发送白名单失效（发到没授权的会话）",
        "publish/dispatcher.py",
        "        return key is not None and key in whitelist",
        "        return True",
        "tests/test_T13_T23_dispatcher.py::test_whitelist_blocks_sessions_outside_it",
    ),
    (
        "停机整组失效（待发照旧落地）",
        "publish/dispatcher.py",
        [
            "        for task in tasks:\n            task.cancel()\n",
            "            state.cancelled = True\n",
            "        if state.cancelled or self._closed:\n",
        ],
        [
            "        for task in tasks:\n            pass\n",
            "            pass\n",
            "        if False:\n",
        ],
        "tests/test_T13_T23_dispatcher.py::test_T23_shutdown_cancels_pending_and_refuses_new_work",
    ),
    (
        "新消息不重置回合（下一回合被上一张吃掉）",
        "publish/dispatcher.py",
        "        if not umo:\n            return\n        self._turns[umo] = TurnState()\n",
        "        if not umo:\n            return\n        return  # 变异：新消息不重置回合\n",
        "tests/test_T13_T23_dispatcher.py::test_delayed_landing_marks_its_own_turn_not_the_new_one",
    ),
        (
        "向量三路串味（图上文字里混进画面描述，命中率悄悄下降）",
        "embed.py",
        '        KIND_TEXT: (text_in_image or "").strip(),',
        '        KIND_TEXT: (text_in_image or "").strip() + (caption or ""),',
        "tests/test_embed_vector.py::test_vectors_are_split_by_kind",
    ),
        (
            "外键级联失效（删行后标签残留）",
            "store/store.py",
            'conn.execute("PRAGMA foreign_keys = ON")',
            'conn.execute("PRAGMA foreign_keys = OFF")',
            "tests/test_store_schema.py::test_delete_cascades_to_labels",
        ),
    # ---- 向量检索 ----
    (
        "向量缓存永不失效（新入库的表情永远检索不到）",
        "publish/vectors.py",
        "            and self._epoch == epoch",
        "            and True",
        "tests/test_embed_vector.py::test_vector_cache_invalidates_on_write",
    ),
    (
        "分数线划得太低（不相关的图也进候选）",
        "publish/dispatcher.py",
        "        floor = best - delta",
        "        floor = best - 10.0",
        "tests/test_pick_variety.py"
        "::test_pick_variety_changes_spread_and_respects_floor",
    ),
    # ---- 人格与作用域 ----
    (
        "人格全空被当成通用（谁都能用）",
        "publish/pick.py",
        "    if not memo.persona_ids:\n        return False",
        "    if not memo.persona_ids:\n        return True",
        "tests/test_incr_persona.py::test_persona_allows_matrix",
    ),
    (
        "人格隔离失效（候选池不按人格收敛）",
        "publish/candidates.py",
        "        persona_key = persona_id if settings.persona_isolation else None",
        "        persona_key = None",
        "tests/test_incr_cache.py::test_cache_is_keyed_by_persona",
    ),
    (
        "未知人格放行（fail-closed 变成串数据）",
        "publish/pick.py",
        "    return bool(persona_id) and persona_id in memo.persona_ids",
        "    return True",
        "tests/test_incr_persona.py::test_manual_send_rejects_foreign_persona",
    ),
    # ---- 去重 ----
    (
        "感知指纹阈值失效（重复图照样入库）",
        "ingest/dedup.py",
        "        if percept_within(percept_hash, candidate, threshold):",
        "        if percept_within(percept_hash, candidate, -1):",
        "tests/test_T06_T07_dedup.py::test_T06_percept_hit_is_duplicate",
    ),
    (
        "内容哈希算法被换（去重全失效）",
        "ingest/fingerprints.py",
        "    return hashlib.sha256(data).hexdigest()",
        "    return hashlib.md5(data).hexdigest()",
        "tests/test_T06_T07_dedup.py::test_content_hash_algorithm_is_pinned",
    ),
    # ---- 存储与迁移 ----
    (
        "候选池被截断（库大后旧表情发不出）",
        "store/store.py",
        "        limit = max(1, min(MAX_CANDIDATES, query.limit))",
        "        limit = max(1, min(400, query.limit))",
        "tests/test_incr_cache.py::test_cache_loads_every_ready_row",
    ),
    # ---- 面板边界 ----
    (
        "上传扩展名白名单失效",
        "panel_core.py",
        "    if ext not in ALLOWED_UPLOAD_EXTS:",
        "    if False:",
        "tests/test_panel_core.py::test_normalize_upload_name_strips_paths_and_checks_extensions",
    ),
    # ---- 抓取安全 ----
    (
        "私网数字主机被放行（SSRF）",
        "ingest/security.py",
        "        return bool(_NUMERIC_HOST.fullmatch(name))",
        "        return False",
        "tests/test_T21_T24_store.py::test_T24_url_checks",
    ),
    (
        "URL 扩展名替代真实内容（伪装成图片）",
        "ingest/fetch.py",
        "    ext = ext_map.get(image_format, \"\")",
        "    ext = \".gif\"",
        "tests/test_T24_fetch.py::test_extension_follows_real_content_not_url",
    ),
    # ---- 闸与发送 ----
    # ---- 命令与工具 ----
    (
        "管理员门失效（任何成员能关掉收集）",
        "commands.py",
        "        if parsed.is_write and not _is_admin(event):",
        "        if False:",
        "tests/test_T29_commands.py::test_member_cannot_toggle_collect",
    ),
    # ---- 采集 ----
    # ---- 解析 ----
    (
        "分类失败仍然入库（垃圾条目占库）",
        "ingest/pipeline.py",
        "            if analysis is None:",
        "            if False:",
        "tests/test_T08_T12_pipeline.py::test_T08_model_failure_leaves_no_row_and_no_file",
    ),
    (
        "话题双路重复加分（同一句话算两份权重）",
        "publish/pick.py",
        "        elif reply_hits and any(_hit(reply_hits, scene) for scene in topics):",
        "        if reply_hits and any(_hit(reply_hits, scene) for scene in topics):",
        "tests/test_pick_score.py::test_topic_hit_scores_once_not_twice",
    ),
    (
        "下限没开也拒负分（小库用完一轮就再也不发）",
        "publish/pick.py",
        "    if best is not None and context.min_score > 0 and best_score < context.min_score:",
        "    if best is not None and best_score < context.min_score:",
        "tests/test_pick_score.py::test_min_score_zero_never_rejects_negative_scores",
    ),
    (
        "作用域滤光被报成不够相关（面板统计口径错）",
        "publish/pick.py",
        "    if not pool:\n        return None, False",
        "    if not pool:\n        return None, True",
        "tests/test_pick_score.py::test_scope_filtered_out_is_not_low_relevance",
    ),
    (
        "语义下限不生效（相似度再低也照发）",
        "publish/dispatcher.py",
        "        if threshold > 0 and best < threshold:",
        "        if False:",
        "tests/test_embed_vector.py::test_embedding_min_similarity_blocks_weak_matches",
    ),
    (
        "延迟落地标记了当前回合（吞掉下一条回复的配图）",
        "publish/dispatcher.py",
        "        await self._mark_used(umo, memo, state)",
        "        await self._mark_used(umo, memo)",
        "tests/test_T13_T23_dispatcher.py::test_delayed_landing_marks_its_own_turn_not_the_new_one",
    ),
    # ---- 人格禁用（mute） ----
    (
        "禁用被通用名单盖掉（禁用了照样发）",
        "publish/pick.py",
        "    if persona_id and persona_id in memo.blocked_persona_ids:\n        return False\n"
        "    if GENERIC_PERSONA in memo.persona_ids:\n        return True\n",
        "    if GENERIC_PERSONA in memo.persona_ids:\n        return True\n"
        "    if persona_id and persona_id in memo.blocked_persona_ids:\n        return False\n",
        "tests/test_incr_persona.py::test_persona_allows_matrix",
    ),
    (
        "禁用名单写入不生效（面板保存后没变化）",
        "panel_core.py",
        '    if "block_persona_ids" in body:',
        '    if "block_personas" in body:',
        "tests/test_panel_core.py::test_persona_params_keeps_allow_and_block_apart",
    ),
    (
        "mute 不需要管理员（任何成员能禁用表情）",
        "commands_core.py",
        'WRITE_SUBS: frozenset[str] = frozenset({"on", "off", "grab", "mute", "unmute"})',
        'WRITE_SUBS: frozenset[str] = frozenset({"on", "off", "grab"})',
        "tests/test_T29_commands.py::test_member_cannot_mute",
    ),
    (
        "unmute 写成 mute（越恢复越禁）",
        "commands.py",
        "        return await self._toggle_persona_block(event, umo, enabled=False)",
        "        return await self._toggle_persona_block(event, umo, enabled=True)",
        "tests/test_T29_commands.py::test_unmute_undoes_the_block",
    ),
    (
        "裸命令名不再落 status（发了 /memenest 没反应）",
        "commands_core.py",
        '        return ParsedCommand("status", default=True)',
        '        return ParsedCommand("status")',
        "tests/test_T29_commands.py::test_bare_command_name_marks_default_subcommand",
    ),
    (
        "话题那一路完全失效（聊到该话题也选不中）",
        "publish/dispatcher.py",
        "            sims = [emb.dot(query, scene_vec)]",
        "            sims = [0.0]",
        "tests/test_embed_vector.py::test_scene_hit_works_from_reply_side",
    ),
    (
        "并发导入结局串味（先结束的算上别人的入库）",
        "ingest/pipeline.py",
        "            outcome = await self._process(ref, ctx)",
        "            await self._process(ref, ctx)\n"
        "            outcome = \"stored\"",
        "tests/test_T08_T12_pipeline.py::test_concurrent_import_reports_own_outcome",
    ),
    (
        "队列与导入各占一份并发（识图 API 被双倍打）",
        "ingest/pipeline.py",
        "        async with slots:\n            return await self._run_one(ref, ctx)",
        "        return await self._run_one(ref, ctx)",
        "tests/test_T08_T12_pipeline.py"
        "::test_queue_and_import_share_one_concurrency_budget",
    ),
    (
        "选图变化档位失效（三档一样，用户调了没反应）",
        "publish/dispatcher.py",
        "        delta = VARIETY_DELTA.get(variety, VARIETY_DELTA[\"balanced\"])",
        "        delta = 0.05",
        "tests/test_pick_variety.py"
        "::test_pick_variety_changes_spread_and_respects_floor",
    ),
    (
        "选图退化成均匀随机（质量完全让位给多样性）",
        "publish/dispatcher.py",
        "        weights = [",
        "        weights = [1.0 for _ in pool] or [",
        "tests/test_pick_variety.py"
        "::test_pick_variety_changes_spread_and_respects_floor",
    ),
    # ---- 0.2.3 批次：去重 / 收集护栏 / 选图先验 / 迁移 / 清单导入 ----
    (
        "动图复合指纹退化为只算首帧（首帧相同的动画被误判重复）",
        "ingest/fetch.py",
        "                percept = animated_percept_hash(image)",
        "                percept = percept_hash(image)",
        "tests/test_T24_fetch.py::test_animated_media_gets_composite_percept_hash",
    ),
    (
        "待审核池满了仍消耗冷却窗口（刷图后真表情收不到）",
        "ingest/collect.py",
        "    if inbox_full:\n        return CollectPlan(SKIP_INBOX_FULL, \"inbox_full\")",
        "    if False:\n        return CollectPlan(SKIP_INBOX_FULL, \"inbox_full\")",
        "tests/test_T03_throttle.py"
        "::test_plan_collect_does_not_consume_throttle_for_unqualified_media",
    ),
    (
        "同类最近使用惩罚失效（连续同情绪一直抽同一张）",
        "publish/pick.py",
        "    if not recent_ids or memo_id not in recent_ids:\n        return 0.0",
        "    if True:\n        return 0.0",
        "tests/test_pick_score.py"
        "::test_category_recent_penalty_demotes_only_the_same_label",
    ),
    (
        "标点先验失效（问号感叹号的语气线索全丢）",
        "publish/punctuation.py",
        "    labels: set[str] = set()",
        "    return set()",
        "tests/test_pick_score.py::test_punctuation_prior_only_adds_a_weak_signal",
    ),
    (
        "清单里有一个坏项就整份失败（导入后一条都进不来）",
        "panel_core.py",
        "        else:\n            continue\n        if url and url not in urls:",
        "        else:\n            return []\n        if url and url not in urls:",
        "tests/test_panel_core.py"
        "::test_manifest_urls_skips_bad_items_without_failing_whole_list",
    ),
    (
        "指纹回填算了但没写库（补齐永远是白跑）",
        "store/store.py",
        '                "UPDATE memo SET percept_hash = ? WHERE id = ?",',
        '                "SELECT ?, ?",',
        "tests/test_percept_backfill.py"
        "::test_backfill_writes_fingerprints_and_skips_unreadable",
    ),
    (
        "识图预算失效（刷图天照爆调用费）",
        "ingest/pipeline.py",
        "        if not self.vision_budget_left():",
        "        if not self.vision_budget_left() and False:",
        "tests/test_T08_T12_pipeline.py::test_vision_budget_caps_model_calls_per_day",
    ),
    (
        "关掉的延迟又被打开（用户明确要立即发却被加抖动）",
        "publish/gate.py",
        "    if base <= 0:\n        return 0.0",
        "    if False:\n        return 0.0",
        "tests/test_T17_T20_gate.py::test_humanized_delay_bounds_and_zero_base",
    ),
    (
        "备份把可再生目录也打进去（体积翻倍还没价值）",
        "backup.py",
        '_CONTENT_DIRS = ("blobs", "inbox")',
        '_CONTENT_DIRS = ("blobs", "inbox", "tmp")',
        "tests/test_backup.py::test_backup_contains_consistent_db_and_images",
    ),
    (
        "备份删除不校验文件名（路径穿越删任意文件）",
        "backup.py",
        '    if not _EXPORT_NAME_RE.match(name or ""):',
        "    if False:",
        "tests/test_backup.py::test_delete_export_rejects_traversal_names",
    ),
    (
        "回填动图退化为只算首帧（复合指纹语义丢失）",
        "ingest/backfill.py",
        _BACKFILL_ANIMATED,
        _BACKFILL_ANIMATED.replace("animated_percept_hash", "percept_hash"),
        "tests/test_percept_backfill.py"
        "::test_backfill_writes_fingerprints_and_skips_unreadable",
    ),
    (
        "复合指纹比对从「三帧都要像」退化为「有一帧像就算」（首帧相同的不同动画被误判）",
        "ingest/fingerprints.py",
        _PERCEPT_WITHIN_ALL,
        _PERCEPT_WITHIN_ALL.replace("all(", "any(", 1),
        "tests/test_T06_T07_dedup.py"
        "::test_same_animation_resized_is_duplicate_but_first_frame_twin_is_not",
    ),
    (
        "指纹回填不推进游标（头部补不出来的条目永远占住窗口）",
        "store/store.py",
        "                \"WHERE (percept_hash IS NULL OR percept_hash = '') AND id > ? \"",
        "                \"WHERE (percept_hash IS NULL OR percept_hash = '') AND ? >= 0 \"",
        "tests/test_percept_backfill.py"
        "::test_backfill_gets_past_a_block_of_uncomputable_rows",
    ),
    (
        "回填遇到 skip 就收工（limit 形同虚设，只补一批）",
        "ingest/backfill.py",
        "        if not rows:\n            # 游标之后确实没有缺指纹的条目了；这才是唯一可以结束的条件\n            break",
        "        if not rows or skipped:\n            break",
        "tests/test_percept_backfill.py::test_backfill_keeps_going_after_a_skip",
    ),
    (
        "工具发送不进发送记录（面板看不到工具发过的张数）",
        "publish/dispatcher.py",
        '        self._note_send(umo, memo, "", VIA_TOOL)',
        "        pass",
        "tests/test_T13_T23_dispatcher.py::test_tool_send_shows_up_in_send_records",
    ),
    (
        "标点先验映射到体系外的标签（这条规则永久失效）",
        "publish/punctuation.py",
        '        labels.add("puzzle")',
        '        labels.add("puzzled")',
        "tests/test_pick_score.py::test_punctuation_prior_only_adds_a_weak_signal",
    ),
    (
        "向量分分母按三路固定归一（没有话题词的图被永久压低）",
        "publish/dispatcher.py",
        '    if WEIGHT_SCENES and kinds.get("scene"):',
        "    if WEIGHT_SCENES:",
        "tests/test_vector_score.py::test_denominator_counts_only_present_lanes",
    ),
    (
        "负相似度被垫成 0（排序被悄悄压平）",
        "publish/dispatcher.py",
        '        value += WEIGHT_CAPTION * emb.dot(query, kinds.get("caption", ()))',
        '        value += WEIGHT_CAPTION * max(0.0, emb.dot(query, kinds.get("caption", ())))',
        "tests/test_vector_score.py::test_negative_similarity_is_not_floored",
    ),
    (
        "无模型入库不计数（识图没生效也看不出来）",
        "ingest/pipeline.py",
        "            self.counters.no_model += 1",
        "            pass",
        "tests/test_T08_T12_pipeline.py::test_missing_model_is_counted_not_silent",
    ),
    (
        "同类最近使用扫描不设窗口（每条消息都排全表）",
        "store/store.py",
        _RECENT_BY_LABEL_SQL,
        _RECENT_BY_LABEL_SQL.replace("LIMIT ?", "LIMIT ? + 100000"),
        "tests/test_pick_score.py::test_recent_by_label_honours_scan_window",
    ),
    (
        "识图第一次失败就放弃（网络抖一下这张图就没了）",
        "ingest/analyze.py",
        "    total = max(1, int(attempts))",
        "    total = 1",
        "tests/test_analyze_retry.py::test_retry_recovers_from_one_transient_failure",
    ),
    (
        "识图预算用尽仍然调模型（每日上限形同虚设）",
        "ingest/analyze.py",
        "        if can_attempt is not None and not can_attempt():",
        "        if False:",
        "tests/test_analyze_retry.py::test_budget_exhausted_stops_before_calling",
    ),
    (
        "重新识图结果不落库（点了没反应）",
        "ingest/reanalyze.py",
        "                caption=analysis.caption,",
        "                caption=memo.caption,",
        "tests/test_reanalyze.py::test_reanalyze_fills_description_and_persists",
    ),
    (
        "重新识图异常后不释放 running（面板永远显示进行中）",
        "ingest/reanalyze.py",
        "            self.state.running = False",
        "            pass",
        "tests/test_reanalyze.py::test_write_failure_still_releases_running_flag",
    ),
    (
        "缺失文件检测失效（库里有记录、文件没了也算健康）",
        "integrity.py",
        "        if not (data_dir / rel).is_file():",
        "        if False:",
        "tests/test_integrity.py::test_scan_reports_missing_and_orphans",
    ),
    (
        "孤儿判定失效（磁盘上的死文件永远查不出来）",
        "integrity.py",
        "            if rel not in known:\n                orphans.append(rel)",
        "            if False:\n                orphans.append(rel)",
        "tests/test_integrity.py::test_scan_reports_missing_and_orphans",
    ),
    (
        "清孤儿把有记录的图也删掉（无声丢数据）",
        "integrity.py",
        "            if rel not in known:\n                found.append(rel)",
        "            if True:\n                found.append(rel)",
        "tests/test_integrity.py::test_remove_orphans_keeps_referenced_files",
    ),
    (
        "缺失记录一键删除时连文件还在的一起删（无声丢图）",
        "integrity.py",
        "        if not (data_dir / rel).is_file()\n    ]",
        "        if False\n    ]",
        "tests/test_integrity.py::test_remove_missing_deletes_only_broken_rows",
    ),
    (
        "导出跳过缺失文件也报成功（用户以为都导出了）",
        "backup.py",
        "            if not target.is_file():\n                skipped += 1\n                continue",
        "            if not target.is_file():\n                included += 1\n                continue",
        "tests/test_export_selection.py::test_build_selection_zip_counts_skipped_files",
    ),
    (
        "导出文件名带冒号（Windows 下载失败）",
        "backup.py",
        'return f"{now:%Y-%m-%dT%H_%M_%S}_{now.microsecond // 1000:03d}.zip"',
        'return f"{now:%Y-%m-%dT%H:%M:%S}_{now.microsecond // 1000:03d}.zip"',
        "tests/test_export_selection.py::test_selection_name_is_filesystem_safe_with_millis",
    ),
    (
        "发送历史倒序写反（最新记录被排到最后）",
        "store/store.py",
        'f"FROM send_log{clause} ORDER BY id DESC LIMIT ? OFFSET ?"',
        'f"FROM send_log{clause} ORDER BY id ASC LIMIT ? OFFSET ?"',
        "tests/test_send_history.py::test_history_is_newest_first_and_filterable",
    ),
    (
        "发送历史超量裁剪删最新（留下最老的）",
        "store/store.py",
        '"  SELECT id FROM send_log ORDER BY id DESC LIMIT 1 OFFSET ?"',
        '"  SELECT id FROM send_log ORDER BY id ASC LIMIT 1 OFFSET ?"',
        "tests/test_send_history.py::test_overflow_prunes_oldest_keeps_newest",
    ),
    (
        "发送历史漏记分类（历史看不出为什么发这张）",
        "publish/dispatcher.py",
        "                label=memo.label,",
        '                label="other",',
        "tests/test_T13_T23_dispatcher.py::test_send_is_recorded_to_history",
    ),
    (
        "发送失败不记原因（历史只写「失败」，排查还得翻日志）",
        "publish/dispatcher.py",
        "            self._note_send(umo, memo, trigger_text, via, outcome=\"failed\", detail=detail)",
        "            self._note_send(umo, memo, trigger_text, via, outcome=\"failed\")",
        "tests/test_T13_T23_dispatcher.py::test_failed_send_records_error_type",
    ),
    (
        "投递失败丢掉异常类型（历史只剩「失败」两个字）",
        "publish/sender.py",
        "        return False, type(exc).__name__",
        '        return False, ""',
        "tests/test_sender.py::test_deliver_reports_error_type",
    ),
    (
        "宿主返回 False 与抛异常混成一种原因",
        "publish/sender.py",
        '        return False, "platform_unavailable"',
        '        return False, "send_failed"',
        "tests/test_sender.py::test_deliver_reports_error_type",
    ),
]
# 注：v1 → v2 的迁移目前没有 DDL（人格禁用表由 schema.sql 的 IF NOT EXISTS 建），
# 所以「跳过迁移」这个变异没有可观察行为，不列在这里；老库升级用例只守
# 「数据不丢 + 版本前进」。


def run_test(nodeid: str, workdir: pathlib.Path) -> tuple[str, str]:
    """跑目标用例，返回 (判定, 输出尾部)。判定取 pass / fail / invalid。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", f"{PKG}/{nodeid}", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = out[-400:]
    if "ModuleNotFoundError" in out or "ImportError while importing" in out:
        return "invalid", tail
    if re.search(r"no tests ran|errors? during collection|error during collection", out):
        return "invalid", tail
    failed = re.search(r"(\d+) failed", out)
    passed = re.search(r"(\d+) passed", out)
    if failed and int(failed.group(1)) > 0:
        return "fail", tail
    if passed and int(passed.group(1)) > 0:
        return "pass", tail
    return "invalid", tail


def main() -> int:
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="mn-mut-"))
    work = scratch / PKG
    shutil.copytree(
        ROOT,
        work,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".git"),
    )
    problems: list[str] = []
    try:
        for index, (label, rel, old, new, nodeid) in enumerate(MUTATIONS, start=1):
            path = work / rel
            if not path.is_file():
                print(f"[{index}] SKIP 文件不存在 {rel}")
                problems.append(f"{label}: 文件不存在 {rel}")
                continue
            original = path.read_text(encoding="utf-8")
            # 互为冗余的多层守卫单摘一层跑不出差别，这里允许一条变异改多处
            pairs = list(zip(old, new)) if isinstance(old, list) else [(old, new)]
            mutated = original
            missed = [item for item, repl in pairs if item not in mutated]
            if missed or not pairs:
                print(f"[{index}] SKIP 锚点未命中 {label}（{len(missed)} 处）")
                problems.append(f"{label}: 锚点未命中（{rel}）")
                continue
            for item, repl in pairs:
                mutated = mutated.replace(item, repl, 1)
            if mutated == original:
                print(f"[{index}] SKIP 等价改写 {label}")
                problems.append(f"{label}: 等价改写，改动后源码没变")
                continue

            baseline, _ = run_test(nodeid, scratch)
            if baseline != "pass":
                print(f"[{index}] INVALID 基线未通过 {label} ({baseline})")
                problems.append(f"{label}: 基线未通过")
                continue

            path.write_text(mutated, encoding="utf-8")
            try:
                verdict, _ = run_test(nodeid, scratch)
            finally:
                path.write_text(original, encoding="utf-8")

            if verdict == "fail":
                print(f"[{index}] 拦住 <- {rel} :: {label}")
            elif verdict == "invalid":
                print(f"[{index}] INVALID 变异后没跑起来 :: {label}")
                problems.append(f"{label}: 变异后用例没跑起来")
            else:
                print(f"[{index}] 未拦住 <- {rel} :: {label}")
                problems.append(f"{label}: 用例没失败")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    if problems:
        print("以下变异没有被任何用例拦住：")
        for item in problems:
            print("  -", item)
        return 1
    print(f"全部 {len(MUTATIONS)} 条变异都被拦住。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
