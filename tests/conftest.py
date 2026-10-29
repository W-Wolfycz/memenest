"""测试公共设置：把插件父目录加入 sys.path，使 memenest 可作为包导入。

约定：测试只覆盖插件自身逻辑，不导入真实 astrbot 模块。惰性导入的
memenest/__init__.py 与 memenest/publish/__init__.py 保证纯逻辑子模块可独立导入。
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent

for path in (WORKSPACE_ROOT, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
