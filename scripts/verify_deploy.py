"""部署前自检：用 AstrBot 自己的解析器验证插件在部署端能不能被加载。

必须在 AstrBot 的运行解释器里执行（会 import astrbot）：

    & $py memenest\\scripts\\verify_deploy.py                       # 默认查部署端
    & $py memenest\\scripts\\verify_deploy.py D:\\AI\\dsh\\plugin\\memenest  # 查源目录（包名会不同）

检查项（任何一项失败都退出非 0）：
1. `_conf_schema.json` 能被 AstrBotConfig 解析（object 缺 items 这类错误会在这里炸）
2. metadata.yaml 能被解析出 name/version
3. 入口文件名被 `_get_modules` 规则接受（main.py 或 <目录名>.py）
4. `_get_classes` 能找到主类
5. import 后 `star_map` 的键等于加载器写入的 module_path（否则 handler 绑定失效）
6. 三个 hook 的 handler_module_path 与 module_path 一致
7. pages/<name>/index.html 与 i18n 文件齐全
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

APPDIR = Path(r"C:\Users\Administrator\AppData\Local\AstrBot\backend\app")
DATA_ROOT = Path(r"C:\Users\Administrator\.astrbot")
DEFAULT_PLUGIN_DIR = DATA_ROOT / "data" / "plugins" / "memenest"
DEFAULT_PACKAGE = "data.plugins.memenest"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    plugin_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_PLUGIN_DIR
    package = DEFAULT_PACKAGE
    if plugin_dir != DEFAULT_PLUGIN_DIR.resolve():
        # 源目录以包名 memenest 导入
        package = "memenest"
        sys.path.insert(0, str(plugin_dir.parent))
    else:
        sys.path.insert(0, str(DATA_ROOT))
    sys.path.insert(0, str(APPDIR))

    print(f"plugin dir : {plugin_dir}")
    print(f"package    : {package}\n")

    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot.core.star.star_manager import PluginManager

    # 1. schema
    schema_path = plugin_dir / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            AstrBotConfig(
                config_path=str(Path(tmp) / "cfg.json"),
                schema=schema,
            )
        check("schema 可被 AstrBotConfig 解析", True, f"{len(schema)} 项")
    except Exception as exc:
        check("schema 可被 AstrBotConfig 解析", False, f"{type(exc).__name__}: {exc}")

    # 2. metadata
    meta = None
    try:
        meta = PluginManager._load_plugin_metadata(plugin_path=str(plugin_dir))
        check(
            "metadata.yaml 可解析",
            bool(meta and meta.name),
            f"{getattr(meta, 'name', None)} v{getattr(meta, 'version', None)}",
        )
    except Exception as exc:
        check("metadata.yaml 可解析", False, f"{type(exc).__name__}: {exc}")

    # 3. 入口文件名（照抄 _get_modules 的判定规则）
    dir_name = plugin_dir.name
    has_main = (plugin_dir / "main.py").exists()
    has_named = (plugin_dir / f"{dir_name}.py").exists()
    check(
        "入口文件名被加载器接受",
        has_main or has_named,
        "main.py" if has_main else (f"{dir_name}.py" if has_named else "既无 main.py 也无 <目录名>.py"),
    )

    # 4~6. 导入与 star_map 对齐
    import importlib

    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    module_name = f"{package}.{'main' if has_main else dir_name}"
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        check("插件模块可导入", False, f"{type(exc).__name__}: {exc}")
        return _summary()

    classes = PluginManager._get_classes(module)
    check("找到主类", bool(classes), ", ".join(classes))
    check("star_map 键与 module_path 对齐", module_name in star_map, module_name)

    hooks = [
        handler
        for handler in star_handlers_registry
        if (handler.handler_module_path or "").startswith(package)
    ]
    mismatched = [h for h in hooks if h.handler_module_path != module_name]
    check(
        "hook 的模块路径一致",
        bool(hooks) and not mismatched,
        f"{len(hooks)} 个 hook" + (f"，{len(mismatched)} 个不一致" if mismatched else ""),
    )

    # 8. schema 叶子键与配置字段一一对应（少一个就是配置静默失效）
    try:
        import dataclasses

        config_module = importlib.import_module(f"{package}.config")
        fields_set = {
            field.name for field in dataclasses.fields(config_module.Settings)
        } - {"extra"}
        leaves: set[str] = set()
        for key, spec in schema.items():
            if spec.get("type") == "object":
                leaves.update(spec.get("items", {}))
            else:
                leaves.add(key)
        check(
            "schema 与配置字段一致",
            leaves == fields_set,
            f"schema {len(leaves)} / Settings {len(fields_set)}"
            + (f"；只在一侧：{sorted(leaves ^ fields_set)}" if leaves != fields_set else ""),
        )
        known = set(config_module._KNOWN_KEYS)
        check(
            "配置字段与已知键一致",
            fields_set == known,
            f"{len(known)} 项"
            + (f"；差异：{sorted(fields_set ^ known)}" if fields_set != known else ""),
        )
    except Exception as exc:
        check("schema 与配置字段一致", False, f"{type(exc).__name__}: {exc}")

    # 7. 页面与 i18n
    pages_root = plugin_dir / "pages"
    page_dirs = (
        sorted(p.name for p in pages_root.iterdir() if p.is_dir() and (p / "index.html").is_file())
        if pages_root.is_dir()
        else []
    )
    check("pages 可发现", bool(page_dirs), ", ".join(page_dirs) or "没有 index.html")

    i18n_dir = plugin_dir / ".astrbot-plugin" / "i18n"
    locales = []
    if i18n_dir.is_dir():
        for file in i18n_dir.glob("*.json"):
            try:
                json.loads(file.read_text(encoding="utf-8"))
                locales.append(file.stem)
            except json.JSONDecodeError as exc:
                check(f"i18n {file.name} 合法", False, str(exc))
    check("i18n 文件合法", bool(locales), ", ".join(locales) or "没有 i18n")

    return _summary()


def _summary() -> int:
    print()
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
