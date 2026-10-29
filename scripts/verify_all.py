"""全量检查：编译 / 未定义名 / 前后端契约 / 宿主路由匹配 / 端到端 / 部署一致性。

跑法（用 AstrBot 的解释器，需要能 import astrbot）：

    & $py memenest\\scripts\\verify_all.py

任何一项 FAIL 就返回非零。
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import re
import subprocess
import os
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_PY = ROOT / "panel.py"
APP_JS = ROOT / "pages" / "manager" / "app.js"

results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 1. 编译
def check_compile() -> None:
    bad = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            bad.append(f"{path.relative_to(ROOT)}: {exc}")
    record(not bad, "全仓可编译", "; ".join(bad) if bad else "所有 .py 通过 compile()")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """跑子进程，强制双方都用 UTF-8。

    Windows 默认按 cp936 给子进程的 stdout 编码，而这里按 utf-8 解，
    中文会变成乱码字符，最后 record() 往 GBK 控制台打印时直接
    UnicodeEncodeError——验证脚本自己先崩了。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


# ------------------------------------------------------- 2. 未定义全局名
def check_names() -> None:
    script = ROOT / "scripts" / "check_names.py"
    proc = _run([sys.executable, str(script)])
    detail = (proc.stdout or "").strip().splitlines()
    record(proc.returncode == 0, "未定义的名字", detail[-1] if detail else "")


# -------------------------------------------------- 3/4/5. 前后端契约
def parse_contract() -> tuple[dict[str, str], dict[str, set[str]], list[tuple[int, str, str]]]:
    text = PLUGIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(text)
    panel = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PanelApi")
    methods = {
        n.name for n in panel.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    routes: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "routes":
            for element in node.value.elts:
                routes[element.elts[0].value] = element.elts[1].attr

    body_keys: dict[str, set[str]] = {}
    for func in panel.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not func.name.startswith("handle_"):
            continue
        keys: set[str] = set()
        for sub in ast.walk(func):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "get"):
                continue
            owner = fn.value
            if isinstance(owner, ast.Name) and owner.id == "body" and sub.args:
                if isinstance(sub.args[0], ast.Constant):
                    keys.add(str(sub.args[0].value))
        if keys:
            body_keys[func.name] = keys

    js = APP_JS.read_text(encoding="utf-8")
    calls: list[tuple[int, str, str]] = []
    for match in re.finditer(
        r"\b(api\.get|api\.post|bridge\.upload|bridge\.download)\(\s*[\"'`]([^\"'`$]+)", js
    ):
        calls.append((js[: match.start()].count("\n") + 1, match.group(1), match.group(2)))
    return routes, body_keys, calls, methods  # type: ignore[return-value]


def check_routes() -> None:
    routes, _, calls, methods = parse_contract()
    orphan_route = [r for r, h in routes.items() if h not in methods]
    detail = str(orphan_route) if orphan_route else f"{len(routes)} 条"
    record(not orphan_route, "路由都有对应 handler", detail)

    handled = {h for h in methods if h.startswith("handle_")}
    unrouted = sorted(handled - set(routes.values()))
    record(not unrouted, "handler 都挂了路由", str(unrouted) if unrouted else "无孤儿方法")

    missing = [f"{line}: {kind}('{ep}')" for line, kind, ep in calls if ep not in routes]
    record(not missing, "前端调用都有路由", "; ".join(missing) if missing else f"{len(calls)} 处调用")

    touched = {ep for _, _, ep in calls}
    idle = sorted(set(routes) - touched)
    record(not idle, "没有闲置路由", str(idle) if idle else "每个路由都被前端使用")


def _extract_object_keys(js: str, open_index: int) -> tuple[set[str], bool]:
    """从 `{` 起提取对象字面量的键；返回 (键集合, 是否含展开)。"""
    depth = 0
    end = open_index
    for index in range(open_index, min(len(js), open_index + 800)):
        char = js[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    body = js[open_index:end]

    if "..." in body:
        spread = True
        body = re.sub(r"\.\.\.[A-Za-z_][A-Za-z0-9_]*", "", body)
    else:
        spread = False

    keys = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", body))
    # 简写：{ ids, label } 里没带冒号的标识符本身就是键
    stripped = re.sub(r"[A-Za-z_][A-Za-z0-9_]*\s*:[^,}]*", "", body)
    keys |= set(re.findall(r"[{,]\s*([A-Za-z_][A-Za-z0-9_]*)", stripped))
    return keys, spread


def check_params() -> None:
    routes, body_keys, calls, _ = parse_contract()
    js = APP_JS.read_text(encoding="utf-8")
    problems = []
    skipped = []

    for line, kind, endpoint in calls:
        if kind == "api.get" or endpoint not in routes:
            continue
        expected = body_keys.get(routes[endpoint], set())
        if not expected:
            continue

        # 找到这次调用的实参起点
        pattern = re.compile(
            r"\b%s\(\s*[\"'`]%s[\"'`]\s*,\s*" % (re.escape(kind), re.escape(endpoint))
        )
        match = pattern.search(js)
        if match is None:
            continue
        arg_start = match.end()
        if js[arg_start] != "{":
            skipped.append(f"{endpoint}（实参是变量）")
            continue

        sent, spread = _extract_object_keys(js, arg_start)
        if spread:
            sent |= {"persona_id", "scope", "umo"}  # importParams() 展开

        # 后端也可能经由 core.import_params 间接读，交集为空才算问题
        if not (sent & expected):
            problems.append(f"{endpoint}: 发 {sorted(sent) or '[]'} 收 {sorted(expected)}")

    detail = "; ".join(problems)
    if skipped:
        detail = (detail + " | " if detail else "") + "跳过变量实参：" + ", ".join(skipped)
    record(not problems, "参数名对得上", detail or "逐个核对一致")


# ------------------------------------------------- 6. 宿主路由正则匹配
def check_host_routes() -> None:
    sys.path.insert(0, r"C:\Users\Administrator\AppData\Local\AstrBot\backend\app")
    try:
        from astrbot.dashboard.api.plugins import _match_registered_web_api  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 只在缺宿主源码时
        record(True, "宿主路由匹配", f"跳过（import 失败：{type(exc).__name__}）")
        return

    routes = parse_contract()[0]
    reads = ("stats", "list", "thumb", "preview", "blocked")
    table = [
        (f"/memenest/{ep}", handler, ["GET"] if ep in reads else ["POST"], "")
        for ep, handler in routes.items()
    ]
    bad = []
    for endpoint, handler in routes.items():
        subpath = f"memenest/{endpoint}"
        method = "GET" if endpoint in reads else "POST"
        matched = _match_registered_web_api(table, subpath, method)
        if not matched or matched[0] != handler:
            bad.append(f"{endpoint}")
    record(not bad, "宿主能匹配到每条路由", str(bad) if bad else f"{len(routes)} 条全部命中")


# ------------------------------------------------ 7. 端到端上传处理
def check_end_to_end() -> None:
    from PIL import Image

    from memenest.config import load_settings
    from memenest.domain import MediaRef, PageQuery
    from memenest.ingest.pipeline import IngestContext, IngestPipeline
    from memenest.store import MemoStore

    async def fake_llm(prompt: str, images: list[str]) -> str:
        return json.dumps(
            {
                "label": "joy",
                "labels": ["cheer"],
                "scenes": ["验证"],
                "text_in_image": "",
                "caption": "验证图",
            },
            ensure_ascii=False,
        )

    class NullLog:
        def debug(self, *a, **k):
            pass

        info = warning = error = debug

    work = pathlib.Path(tempfile.mkdtemp(prefix="verify-"))
    data = work / "data"
    (data / "imports").mkdir(parents=True)

    def ref(source: str) -> MediaRef:
        return MediaRef(
            source=source,
            declared_ext=".png",
            origin_kind="import",
            platform="panel",
            reason="import_upload",
        )

    async def run() -> tuple[dict, dict, dict, tuple[str, ...] | None]:
        store = MemoStore(data / "memo.db")
        store.initialize()
        settings = load_settings({"send_delay_seconds": 0})
        pipeline = IngestPipeline(
            store=store,
            data_dir=data,
            settings=lambda: settings,
            llm_call=lambda: fake_llm,
            prompt="{{label_keys}}",
            log=NullLog(),
        )
        pipeline.prepare_dirs()
        ctx = IngestContext(umo=None, origin_kind="import", scope="any", persona_id="验证人格")
        src = data / "imports" / "aaa.png"
        Image.new("RGB", (120, 120), (30, 120, 210)).save(src, format="PNG")

        first = await pipeline.process_one(ref(str(src)), ctx)
        again = await pipeline.process_one(ref(str(src)), ctx)
        missing = await pipeline.process_one(ref(str(data / "imports" / "nope.png")), ctx)
        memos, _ = store.page(PageQuery())
        personas = memos[0].persona_ids if memos else None
        store.close()
        return first, again, missing, personas

    first, again, missing, personas = asyncio.run(run())

    ok = True
    detail = []
    if first.get("outcome") != "stored":
        ok = False
        detail.append(f"首次应当 stored，实际 {first.get('outcome')}")
    if again.get("outcome") != "duplicate":
        ok = False
        detail.append(f"重复应当 duplicate，实际 {again.get('outcome')}")
    if missing.get("outcome") != "rejected":
        ok = False
        detail.append(f"缺失文件应当 rejected，实际 {missing.get('outcome')}")
    if personas != ("验证人格",):
        ok = False
        detail.append(f"人格没落库：{personas}")
    detail_text = "; ".join(detail) if detail else "stored / duplicate / rejected / 人格 全部正确"
    record(ok, "端到端上传处理", detail_text)


# ---------------------------------------------------------- 8. 前端语法
def check_js() -> None:
    node = pathlib.Path(
        r"C:\Users\Administrator\AppData\Local\AstrBot\backend\python"
        r"\Lib\site-packages\playwright\driver\node.exe"
    )
    if not node.is_file():
        record(True, "app.js 语法", "跳过（没有 node）")
        return
    proc = _run([str(node), "--check", str(APP_JS)])
    record(proc.returncode == 0, "app.js 语法", (proc.stderr or "").strip()[:160] or "OK")


# ------------------------------------------------------- 9. 部署一致性
def check_deploy() -> None:
    script = ROOT / "scripts" / "verify_deploy.py"
    if not script.is_file():
        record(True, "部署端一致", "跳过（没有 verify_deploy.py）")
        return
    proc = _run([sys.executable, str(script)])
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    record(proc.returncode == 0, "部署端一致", tail[-1] if tail else "")


# --------------------------------------------------------------- main
def main() -> int:
    sys.path.insert(0, str(ROOT.parent))
    print("=" * 70)
    check_compile()
    check_names()
    check_routes()
    check_params()
    check_host_routes()
    check_end_to_end()
    check_js()
    check_deploy()
    print("=" * 70)

    failed = [name for ok, name, _ in results if not ok]
    if failed:
        print(f"{len(failed)} 项失败：")
        for name in failed:
            print(f"  - {name}")
        return 1
    print(f"全部 {len(results)} 项通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
