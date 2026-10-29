"""静态检查：找出引用了但没定义的名字。

语法检查（`ast.parse` / `compile`）发现不了这类问题——`name=x-y` 语法合法，
是运行时才炸的 `NameError`。这里用 `symtable` 把每个作用域引用的全局名
与模块级定义、内置名单比对，能提前抓出来。

用法：
    & $py memenest\\scripts\\check_names.py
"""

from __future__ import annotations

import builtins
import pathlib
import symtable

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {"__pycache__", "tests", "docs", "data"}

BUILTIN_NAMES = set(dir(builtins)) | {
    "__name__",
    "__file__",
    "__doc__",
    "__package__",
    "__spec__",
    "__loader__",
    "__builtins__",
    "__cached__",
}


def check_source(source: str, label: str) -> list[str]:
    try:
        module = symtable.symtable(source, label, "exec")
    except SyntaxError as exc:
        return [f"{label}: 语法错误 {exc}"]
    module_names = {symbol.get_name() for symbol in module.get_symbols()}
    problems: list[str] = []

    def walk(table: symtable.SymbolTable, where: str) -> None:
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if symbol.is_global() and name not in module_names and name not in BUILTIN_NAMES:
                problems.append(f"{label}: {where} 引用了未定义的名字 {name!r}")
        for child in table.get_children():
            walk(child, f"{where}.{child.get_name()}")

    walk(module, "<module>")
    return problems


def main() -> int:
    problems: list[str] = []
    checked = 0
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        checked += 1
        problems.extend(check_source(path.read_text(encoding="utf-8"), rel.as_posix()))

    print(f"检查 {checked} 个文件")
    if problems:
        print("\n发现可疑名字：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("没有未定义的名字。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
