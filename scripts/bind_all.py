"""外挂脚本：不启动插件，直接改库里所有表情的人格归属。

用途：插件加载失败、或者一次要改几千条时，绕开宿主直接动数据库。

用法（只需要 Python 标准库，不需要 AstrBot 的依赖）：

    # 先干跑看一眼影响多少条
    python bind_all.py --db <memo.db> --persona 拉菲 --dry-run

    # 全部只绑「拉菲」
    python bind_all.py --db <memo.db> --persona 拉菲

    # 全部绑「拉菲」和「伊19」（任一个人格都能用）
    python bind_all.py --db <memo.db> --persona 拉菲,伊19

    # 全部改成通用（任何人格都能用）
    python bind_all.py --db <memo.db> --generic

    # 全部改成「谁都不用」
    python bind_all.py --db <memo.db> --clear

注意：插件正在跑的时候改库，内存里的向量/候选缓存不会立刻失效
（插件靠 store.epoch 判断，外部写入不会推进它）。改完重启插件，或者等 30 秒。
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sqlite3
import sys
import time

GENERIC = "*"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量改全部表情的人格归属")
    parser.add_argument("--db", required=True, help="memo.db 的完整路径")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--persona", help="人格 id，多个用逗号分隔")
    group.add_argument(
        "--generic", action="store_true", help="全部改成通用（任何人格都能用）"
    )
    group.add_argument(
        "--clear", action="store_true", help="全部改成谁都不用（关联表清空）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只报告会影响多少条，不写库"
    )
    parser.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    return parser.parse_args(argv)


def target_personas(args: argparse.Namespace) -> tuple[list[str], str]:
    if args.generic:
        return [GENERIC], "通用"
    if args.clear:
        return [], "谁都不用"
    parts = [part.strip() for part in str(args.persona).replace("，", ",").split(",")]
    values = list(dict.fromkeys(part for part in parts if part))
    if not values:
        raise SystemExit("--persona 不能为空")
    return values, "/".join(values)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    db_path = pathlib.Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"找不到数据库：{db_path}")
        return 1

    personas, label = target_personas(args)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "memo" not in tables or "memo_persona" not in tables:
            print("这个库看起来不是 memenest 的（缺少 memo / memo_persona）")
            return 1
        total = int(conn.execute("SELECT COUNT(*) AS n FROM memo").fetchone()["n"])
        bound = [
            (row["persona_id"], int(row["n"]))
            for row in conn.execute(
                "SELECT persona_id, COUNT(*) AS n FROM memo_persona GROUP BY persona_id"
            )
        ]

    print(f"数据库：{db_path}")
    print(f"条目总数：{total}")
    print("当前绑定分布：")
    if bound:
        for persona_id, count in sorted(bound, key=lambda item: -item[1]):
            name = "通用" if persona_id == GENERIC else persona_id
            print(f"  {name}: {count} 条")
    else:
        print("  （全部没有绑定 = 谁都不用）")
    print(f"\n本次将把全部 {total} 条改成：{label}")

    if total == 0:
        print("库里没有条目，什么都没做。")
        return 0
    if args.dry_run:
        print("\n[dry-run] 没有写库。去掉 --dry-run 才会生效。")
        return 0

    if not args.no_backup:
        stamp = time.strftime("%Y%m%d%H%M%S")
        backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
        shutil.copy2(db_path, backup)
        print(f"\n已备份：{backup.name}")

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM memo_persona")
        if personas:
            ids = [int(row[0]) for row in conn.execute("SELECT id FROM memo")]
            conn.executemany(
                "INSERT OR IGNORE INTO memo_persona(memo_id, persona_id) VALUES(?, ?)",
                [(memo_id, persona_id) for memo_id in ids for persona_id in personas],
            )
        conn.commit()
        # 让插件侧的 epoch 推进不了，这里只保证文件落盘
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    print(f"已把全部 {total} 条改成：{label}")
    print("如果插件正在运行，重启它（或者等 30 秒）让缓存失效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
