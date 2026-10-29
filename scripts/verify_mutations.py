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
        "图内文字上限退回 80（长台词检索不到后半句）",
        "domain.py",
        "MAX_TEXT_IN_IMAGE = 200",
        "MAX_TEXT_IN_IMAGE = 80",
        "tests/test_T09_parse.py::test_text_in_image_keeps_long_lines",
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
            "        for state in self._turns.values():\n            state.cancelled = True\n",
            "        if state.cancelled or self._closed:\n",
        ],
        [
            "        for task in tasks:\n            pass\n",
            "",
            "        if False:\n",
        ],
        "tests/test_T13_T23_dispatcher.py::test_T23_shutdown_cancels_pending_and_refuses_new_work",
    ),
    (
        "新消息打断排期（延迟发送被吞）",
        "publish/dispatcher.py",
        "        if not umo:\n            return False\n        return_value = False\n",
        "        if not umo:\n            return False\n        return_value = False\n"
        "        _victim = self._turns.get(umo)\n"
        "        if _victim is not None and _victim.task is not None:\n"
        "            _victim.task.cancel()\n",
        "tests/test_T13_T23_dispatcher.py::test_T13_new_message_does_not_cancel_the_pending_send",
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
        "阈值方向反了（该发的不发、不该发的照发）",
        "publish/dispatcher.py",
        "        if best < threshold:",
        "        if best > threshold:",
        "tests/test_embed_vector.py::test_vector_pick_hits_threshold_rejects_and_degrades",
    ),
    (
        "多样性冲掉相似度（选图变成随机）",
        "publish/dispatcher.py",
        "            value = similarity + self._diversity_bonus(memo, recent)",
        "            value = self._diversity_bonus(memo, recent)",
        "tests/test_embed_vector.py::test_vector_pick_hits_threshold_rejects_and_degrades",
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
        "        if hamming(percept_hash, candidate) <= threshold:",
        "        if hamming(percept_hash, candidate) <= -1:",
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
        "    if _NUMERIC_HOST.fullmatch(name):",
        "    if False:",
        "tests/test_T21_T24_store.py::test_T24_numeric_host_forms_are_forbidden",
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
        "话题只认用户消息（机器人自己接梗时选不中）",
        "publish/dispatcher.py",
        "                    if context_query is not None:\n"
        "                        sims.append(emb.dot(context_query, scene_vec))",
        "                    if context_query is not None:\n"
        "                        sims = [emb.dot(context_query, scene_vec)]",
        "tests/test_embed_vector.py::test_scene_hit_works_from_reply_side",
    ),
]


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
