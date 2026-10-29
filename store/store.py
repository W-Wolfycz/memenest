"""SQLite 存储层：唯一出现 SQL 的文件。

依赖方向：store → domain（叶子数据模型）。本模块不 import ingest/publish/plugin，
也不做任何网络或文件搬运——删文件由调用方按返回值执行。

写操作会推进 `epoch`：上层缓存（候选集）据此判断是否需要重查，无需回调。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..domain import (
    GENERIC_PERSONA,
    SCOPE_ANY,
    SCOPE_ORIGIN,
    STATE_DISABLED,
    STATE_INBOX,
    STATE_READY,
    clean_topics,
    MAX_CAPTION,
    MAX_TEXT_IN_IMAGE,
    CandidateQuery,
    Draft,
    Memo,
    PageQuery,
    Stats,
)

SCHEMA_VERSION = "1"
PERCEPT_SCAN_LIMIT = 2000
MAX_PAGE_SIZE = 50
MAX_CANDIDATES = 2000
MAX_PURGE = 500


class StoreError(RuntimeError):
    """库不可用或写入失败。调用方负责回滚文件副作用。"""


class MemoStore:
    def __init__(self, db_path: Path, schema_path: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.schema_path = schema_path or Path(__file__).with_name("schema.sql")
        self._initialized = False
        self._epoch = 0

    @property
    def epoch(self) -> int:
        """每次写操作 +1；缓存用它判断是否过期。"""
        return self._epoch

    def _touch(self) -> None:
        self._epoch += 1

    # -- 基础设施 ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self) -> None:
        """建库建表。幂等，插件每次启动调用一次。

        结构版本对不上时明确报错，不做静默迁移：这个插件还在 0.1.0 阶段，
        不存在需要兼容的旧库，让用户删掉数据目录重来比带着半旧结构跑更安全。
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        ddl = self.schema_path.read_text(encoding="utf-8")
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                # 先跑建表脚本（都是 IF NOT EXISTS，对已存在的库无副作用），
                # 之后 meta 一定存在，才查得到结构版本。
                conn.executescript(ddl)
                stored = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if stored is not None and str(stored["value"]) != SCHEMA_VERSION:
                    raise StoreError(
                        f"数据库结构版本 {stored['value']} 与插件要求的 {SCHEMA_VERSION} 不一致，"
                        f"请删除 {self.db_path.parent} 后重新收集"
                    )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (SCHEMA_VERSION,),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"初始化数据库失败: {exc}") from exc
        self._initialized = True

    def close(self) -> None:
        """连接按操作创建，这里只需要把 WAL 落一次盘。"""
        if not self._initialized:
            return
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            # 关库失败不该让停机流程抛异常；下次打开会自动恢复。
            pass

    # -- 黑名单 -----------------------------------------------------------

    def is_blocked(self, content_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM blocklist WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        return row is not None

    def block(
        self, content_hash: str, now: int, thumb: bytes | None = None
    ) -> None:
        """拉黑一个内容指纹；thumb 是给人看的小图，可为空。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO blocklist(content_hash, created_at, thumb) VALUES(?, ?, ?) "
                # 已经拉黑过就保持原样，但如果之前没存缩略图（老数据），这次补上
                "ON CONFLICT(content_hash) DO UPDATE SET "
                "thumb = COALESCE(blocklist.thumb, excluded.thumb)",
                (content_hash, now, thumb),
            )
        self._touch()

    def unblock_prefix(self, prefix: str) -> int:
        """按指纹前缀解除拉黑，返回解除条数。前缀必须是十六进制。"""
        text = (prefix or "").strip().lower()
        # 只接受十六进制：否则 % / _ 会被 LIKE 当成通配符，误删整个黑名单。
        if not re.fullmatch(r"[0-9a-f]{8,64}", text):
            return 0
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM blocklist WHERE content_hash LIKE ?", (f"{text}%",)
            )
        self._touch()
        return cursor.rowcount

    def blocked_page(
        self, limit: int = 10, offset: int = 0
    ) -> list[tuple[str, int, bool]]:
        """返回 (指纹, 拉黑时间, 是否有缩略图)；列表不带图片本身。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content_hash, created_at, thumb IS NOT NULL AS has_thumb "
                "FROM blocklist ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (max(1, min(50, limit)), max(0, offset)),
            ).fetchall()
        return [
            (str(row["content_hash"]), int(row["created_at"]), bool(row["has_thumb"]))
            for row in rows
        ]

    def blocked_thumb(self, content_hash: str) -> bytes | None:
        """取黑名单条目的缩略图字节；老数据没有就返回 None。"""
        text = (content_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{8,64}", text):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thumb FROM blocklist WHERE content_hash = ?", (text,)
            ).fetchone()
            if row is None or row["thumb"] is None:
                # 允许用唯一前缀取，和 unban 的前缀语义一致
                row = conn.execute(
                    "SELECT thumb FROM blocklist WHERE content_hash LIKE ? LIMIT 1",
                    (f"{text}%",),
                ).fetchone()
        if row is None or row["thumb"] is None:
            return None
        return bytes(row["thumb"])

    def blocked_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM blocklist").fetchone()
        return int(row["n"])

    # -- 去重 -------------------------------------------------------------

    def match_content_hash(self, content_hash: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM memo WHERE content_hash = ? ORDER BY id LIMIT 1",
                (content_hash,),
            ).fetchone()
        return int(row["id"]) if row else None

    def percept_rows(self, limit: int = PERCEPT_SCAN_LIMIT) -> list[tuple[int, str]]:
        """按入库时间倒序取有限行做近似比对，避免全表线性扫描。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, percept_hash FROM memo "
                "WHERE percept_hash IS NOT NULL AND percept_hash <> '' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(int(row["id"]), str(row["percept_hash"])) for row in rows]

    # -- 写入 -------------------------------------------------------------

    def add(self, draft: Draft) -> int | None:
        """插入一条完整记录。rel_path 冲突或标签写入失败时整行回滚并返回 None。"""
        scenes = _unique(draft.analysis.scenes)
        labels = _unique(draft.analysis.labels)
        state_value = (
            draft.state
            if draft.state in (STATE_INBOX, STATE_READY, STATE_DISABLED)
            else STATE_INBOX
        )
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO memo("
                    " rel_path, content_hash, percept_hash, label, caption, text_in_img,"
                    " scope, origin_umo, state, created_at, origin_kind"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        draft.rel_path,
                        draft.content_hash,
                        draft.percept_hash,
                        draft.analysis.label,
                        _clip(draft.analysis.caption, MAX_CAPTION),
                        _clip(draft.analysis.text_in_image, MAX_TEXT_IN_IMAGE),
                        draft.scope if draft.scope in (SCOPE_ANY, SCOPE_ORIGIN) else SCOPE_ANY,
                        draft.origin_umo,
                        state_value,
                        draft.created_at,
                        draft.origin_kind,
                    ),
                )
                memo_id = int(cursor.lastrowid)
                self._write_associations(conn, memo_id, scenes, labels)
                # 新建时没指定人格的视为通用，显式记一个 * 标记
                _write_personas(conn, memo_id, draft.persona_ids or (GENERIC_PERSONA,))
        except sqlite3.IntegrityError:
            # rel_path 撞车：等价于「这条已经在了」。
            return None
        except sqlite3.Error as exc:
            raise StoreError(f"写入失败: {exc}") from exc
        self._touch()
        return memo_id

    @staticmethod
    def _write_associations(
        conn: sqlite3.Connection,
        memo_id: int,
        scenes: list[str],
        labels: list[str],
    ) -> None:
        conn.executemany(
            "INSERT INTO memo_scene(memo_id, scene) VALUES(?, ?) "
            "ON CONFLICT(memo_id, scene) DO NOTHING",
            [(memo_id, scene) for scene in scenes],
        )
        conn.executemany(
            "INSERT INTO memo_label(memo_id, label) VALUES(?, ?) "
            "ON CONFLICT(memo_id, label) DO NOTHING",
            [(memo_id, label) for label in labels],
        )

    def set_state(self, memo_id: int, state: str, rel_path: str | None = None) -> bool:
        """改状态；可选同时改路径（文件已经挪好时传入新 rel_path）。"""
        if state not in (STATE_INBOX, STATE_READY, STATE_DISABLED):
            return False
        with self._connect() as conn:
            if rel_path:
                cursor = conn.execute(
                    "UPDATE memo SET state = ?, rel_path = ? WHERE id = ?",
                    (state, rel_path, memo_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE memo SET state = ? WHERE id = ?", (state, memo_id)
                )
        self._touch()
        return cursor.rowcount > 0

    def set_scope(self, memo_id: int, scope: str) -> bool:
        if scope not in (SCOPE_ANY, SCOPE_ORIGIN):
            return False
        with self._connect() as conn:
            if scope == SCOPE_ORIGIN:
                # 没有来源会话的条目设成 origin 会变成永远选不中的死条目。
                row = conn.execute(
                    "SELECT origin_umo FROM memo WHERE id = ?", (memo_id,)
                ).fetchone()
                if row is None or not row["origin_umo"]:
                    return False
            cursor = conn.execute("UPDATE memo SET scope = ? WHERE id = ?", (scope, memo_id))
        self._touch()
        return cursor.rowcount > 0

    def set_fav(self, memo_id: int, fav: bool) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE memo SET is_fav = ? WHERE id = ?", (1 if fav else 0, memo_id)
            )
        self._touch()
        return cursor.rowcount > 0

    # -- 向量 -------------------------------------------------------------

    def save_embeddings(
        self, rows: list[tuple[int, str, str, int, bytes]], now: int
    ) -> int:
        """批量写向量（memo_id, kind, model, dim, vec）。同 (memo_id, kind, model) 覆盖。"""
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO memo_embedding(memo_id, kind, model, dim, vec, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(memo_id, kind, model) DO UPDATE SET "
                "dim = excluded.dim, vec = excluded.vec, updated_at = excluded.updated_at",
                [
                    (memo_id, kind, model, dim, vec, now)
                    for memo_id, kind, model, dim, vec in rows
                ],
            )
        self._touch()
        return len(rows)

    def load_embeddings(self, model: str) -> dict[int, dict[str, tuple[int, bytes]]]:
        """取全部向量；返回 {memo_id: {kind: (dim, blob)}}。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT memo_id, kind, dim, vec FROM memo_embedding WHERE model = ?",
                (model,),
            ).fetchall()
        result: dict[int, dict[str, tuple[int, bytes]]] = {}
        for row in rows:
            result.setdefault(int(row["memo_id"]), {})[str(row["kind"])] = (
                int(row["dim"]),
                bytes(row["vec"]),
            )
        return result


    def missing_embedding_memos(
        self, model: str, dim: int = 0, limit: int = 0
    ) -> list[Memo]:
        """还缺某一路向量的条目（可用 + 待审核都算）。

        dim>0 时只认维度匹配的行：模型没换但维度变了，旧向量不能复用，
        必须当成「缺」重算，否则检索会一直静默降级。

        「缺」逐路判断：图上没字的条目本来就不该有 text 向量，
        不能因为少了这一路就当成没算过、被反复返回、无限重算。
        """
        with self._connect() as conn:
            if dim > 0:
                covered = conn.execute(
                    "SELECT memo_id, kind FROM memo_embedding WHERE model = ? AND dim = ?",
                    (model, dim),
                ).fetchall()
            else:
                covered = conn.execute(
                    "SELECT memo_id, kind FROM memo_embedding WHERE model = ?", (model,)
                ).fetchall()
            have: dict[int, set[str]] = {}
            for row in covered:
                have.setdefault(int(row["memo_id"]), set()).add(str(row["kind"]))

            rows = conn.execute(
                "SELECT * FROM memo WHERE state != ? ORDER BY id DESC", (STATE_DISABLED,)
            ).fetchall()
            pending: list[Memo] = []
            for row in rows:
                memo = self._row_to_memo(conn, row)
                want = set()
                if memo.text_in_img.strip():
                    want.add("text")
                if memo.caption.strip():
                    want.add("caption")
                if memo.scenes or memo.label:
                    want.add("scene")
                if not want - have.get(memo.id, set()):
                    continue
                pending.append(memo)
                if limit > 0 and len(pending) >= limit:
                    break
            return pending

    def ids_in_state(self, state: str) -> list[int]:
        """某状态下的全部 id（不受分页影响），供「一键转换」使用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM memo WHERE state = ? ORDER BY id DESC", (state,)
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def embedding_coverage(self, model: str, dim: int = 0) -> tuple[int, int]:
        """(算齐的条目数, 应算的条目数)。

        计数单位是「条目」不是「向量行」——一条表情有三路向量，
        按行数算会让进度显示成 3 倍。所谓「算齐」= 该有的那几路都在。
        """
        with self._connect() as conn:
            if dim > 0:
                covered_rows = conn.execute(
                    "SELECT memo_id, kind FROM memo_embedding WHERE model = ? AND dim = ?",
                    (model, dim),
                ).fetchall()
            else:
                covered_rows = conn.execute(
                    "SELECT memo_id, kind FROM memo_embedding WHERE model = ?", (model,)
                ).fetchall()
            have: dict[int, set[str]] = {}
            for row in covered_rows:
                have.setdefault(int(row["memo_id"]), set()).add(str(row["kind"]))

            rows = conn.execute(
                "SELECT * FROM memo WHERE state != ?", (STATE_DISABLED,)
            ).fetchall()
            expected = 0
            done = 0
            for row in rows:
                memo = self._row_to_memo(conn, row)
                want = set()
                if memo.text_in_img.strip():
                    want.add("text")
                if memo.caption.strip():
                    want.add("caption")
                if memo.scenes or memo.label:
                    want.add("scene")
                if not want:
                    continue
                expected += 1
                if not want - have.get(memo.id, set()):
                    done += 1
        return done, expected

    def clear_embeddings(self, model: str | None = None) -> int:
        with self._connect() as conn:
            if model is None:
                cursor = conn.execute("DELETE FROM memo_embedding")
            else:
                cursor = conn.execute(
                    "DELETE FROM memo_embedding WHERE model = ?", (model,)
                )
        self._touch()
        return int(cursor.rowcount or 0)

    def set_personas(self, memo_id: int, persona_ids: list[str] | tuple[str, ...]) -> bool:
        """整体替换某条表情绑定的人格；传空表示改回通用。"""
        values = [str(item).strip() for item in persona_ids if str(item).strip()]
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM memo WHERE id = ?", (memo_id,)
            ).fetchone()
            if exists is None:
                return False
            conn.execute("DELETE FROM memo_persona WHERE memo_id = ?", (memo_id,))
            _write_personas(conn, memo_id, values)
        self._touch()
        return True

    def update_analysis(
        self,
        memo_id: int,
        *,
        caption: str | None = None,
        text_in_image: str | None = None,
        scenes: list[str] | None = None,
        labels: list[str] | None = None,
        label: str | None = None,
    ) -> bool:
        """人工修订打标结果：只改传入的字段，全部在一个连接里完成。

        `None` 表示"不动这一项"，空列表表示"清空"。
        """
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM memo WHERE id = ?", (memo_id,)).fetchone() is None:
                return False
            if caption is not None:
                conn.execute(
                    "UPDATE memo SET caption = ? WHERE id = ?", (caption, memo_id)
                )
            if text_in_image is not None:
                conn.execute(
                    "UPDATE memo SET text_in_img = ? WHERE id = ?", (text_in_image, memo_id)
                )
            if label is not None:
                conn.execute("UPDATE memo SET label = ? WHERE id = ?", (label, memo_id))
            if scenes is not None:
                # 条数与单条长度都在这里收口：上层（面板/工具）虽然已限制，
                # 但直接调 store 的路径不能靠上层自觉。
                # 超长整条丢弃而不是截断——6 字上限下截出来就是半截废词。
                scenes = clean_topics(scenes)
                conn.execute("DELETE FROM memo_scene WHERE memo_id = ?", (memo_id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO memo_scene(memo_id, scene) VALUES(?, ?)",
                    [(memo_id, item) for item in scenes],
                )
            if labels is not None:
                conn.execute("DELETE FROM memo_label WHERE memo_id = ?", (memo_id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO memo_label(memo_id, label) VALUES(?, ?)",
                    [(memo_id, item) for item in labels],
                )
        self._touch()
        return True

    def bump_usage(self, memo_id: int, now: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE memo SET use_count = use_count + 1, used_at = ? WHERE id = ?",
                (now, memo_id),
            )
        self._touch()

    def delete(self, memo_id: int) -> str | None:
        """删行并返回原相对路径，由调用方删除文件（行先走，文件后走）。"""
        with self._connect() as conn:
            row = conn.execute("SELECT rel_path FROM memo WHERE id = ?", (memo_id,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM memo WHERE id = ?", (memo_id,))
        self._touch()
        return str(row["rel_path"])

    def evict(self, cap: int) -> list[str]:
        """按「收藏/人工导入永不淘汰 → 用得少 → 最久未用 → 最早入库」淘汰。"""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM memo WHERE state = ?", (STATE_READY,)
            ).fetchone()["n"]
            overflow = int(total) - max(0, cap)
            if overflow <= 0:
                return []
            rows = conn.execute(
                "SELECT id, rel_path FROM memo "
                "WHERE state = ? AND is_fav = 0 AND COALESCE(origin_kind, 'chat') <> 'import' "
                "ORDER BY use_count ASC, used_at ASC, created_at ASC, id ASC LIMIT ?",
                (STATE_READY, overflow),
            ).fetchall()
            if not rows:
                return []
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM memo WHERE id IN ({placeholders})", ids)
        self._touch()
        return [str(row["rel_path"]) for row in rows]

    # -- 读取 -------------------------------------------------------------

    def get(self, memo_id: int) -> Memo | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memo WHERE id = ?", (memo_id,)).fetchone()
            if row is None:
                return None
            return self._row_to_memo(conn, row)

    def page(self, query: PageQuery) -> tuple[list[Memo], int]:
        size = max(1, min(MAX_PAGE_SIZE, query.size))
        page = max(1, query.page)
        clauses: list[str] = []
        params: list[Any] = []
        if query.state:
            clauses.append("state = ?")
            params.append(query.state)
        if query.label:
            clauses.append(
                "(label = ? OR id IN (SELECT memo_id FROM memo_label WHERE label = ?))"
            )
            params.extend([query.label, query.label])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            total = int(
                conn.execute(f"SELECT COUNT(*) AS n FROM memo {where}", params).fetchone()["n"]
            )
            rows = conn.execute(
                f"SELECT * FROM memo {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, size, (page - 1) * size],
            ).fetchall()
            memos = [self._row_to_memo(conn, row) for row in rows]
        return memos, total

    def candidates(self, query: CandidateQuery) -> list[Memo]:
        """作用域与人格在这里先收敛（fail-closed）：取不到就只给通用表情。"""
        params: list[Any] = [query.state]
        scope_clause = "scope = 'any'"
        if query.ignore_scope:
            scope_clause = "1 = 1"
        elif query.allow_origin and query.umo:
            scope_clause = "(scope = 'any' OR (scope = 'origin' AND origin_umo = ?))"
            params.append(query.umo)
        persona_clause = ""
        if query.persona_filter:
            # 显式通用（*）任何会话都能用；绑了人格的命中当前人格才可用
            if query.persona_id:
                persona_clause = (
                    " AND EXISTS (SELECT 1 FROM memo_persona mp WHERE mp.memo_id = memo.id"
                    " AND (mp.persona_id = ? OR mp.persona_id = ?))"
                )
                params.extend([GENERIC_PERSONA, query.persona_id])
            else:
                persona_clause = (
                    " AND EXISTS (SELECT 1 FROM memo_persona mp WHERE mp.memo_id = memo.id"
                    " AND mp.persona_id = ?)"
                )
                params.append(GENERIC_PERSONA)
        label_clause = ""
        if query.label:
            label_clause = (
                " AND (label = ? OR id IN (SELECT memo_id FROM memo_label WHERE label = ?))"
            )
            params.extend([query.label, query.label])
        limit = max(1, min(MAX_CANDIDATES, query.limit))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memo WHERE state = ? AND "
                f"{scope_clause}{persona_clause}{label_clause} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            return [self._row_to_memo(conn, row) for row in rows]

    def labels_in_use(self) -> dict[str, int]:
        """主标签 + 附加标签的实际分布（调标签体系时看这个）。"""
        with self._connect() as conn:
            counts: dict[str, int] = {}
            for row in conn.execute(
                "SELECT label, COUNT(*) AS n FROM memo GROUP BY label"
            ).fetchall():
                counts[str(row["label"])] = counts.get(str(row["label"]), 0) + int(row["n"])
            for row in conn.execute(
                "SELECT label, COUNT(*) AS n FROM memo_label GROUP BY label"
            ).fetchall():
                counts[str(row["label"])] = counts.get(str(row["label"]), 0) + int(row["n"])
        return counts

    def personas(self) -> dict[str, int]:
        """每个人格被多少条表情绑定；空字符串键表示「通用」。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT persona_id, COUNT(*) AS n FROM memo_persona GROUP BY persona_id"
            ).fetchall()
        result: dict[str, int] = {}
        for row in rows:
            key = str(row["persona_id"])
            key = "" if key == GENERIC_PERSONA else key
            result[key] = result.get(key, 0) + int(row["n"])
        return result

    def recent_ids(self, limit: int = 10) -> tuple[int, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM memo WHERE used_at > 0 ORDER BY used_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return tuple(int(row["id"]) for row in rows)

    def stats(self) -> Stats:
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) AS n FROM memo").fetchone()["n"])
            per_state = {
                str(row["state"]): int(row["n"])
                for row in conn.execute(
                    "SELECT state, COUNT(*) AS n FROM memo GROUP BY state"
                ).fetchall()
            }
            blocked = int(
                conn.execute("SELECT COUNT(*) AS n FROM blocklist").fetchone()["n"]
            )
            by_label = {
                str(row["label"]): int(row["n"])
                for row in conn.execute(
                    "SELECT label, COUNT(*) AS n FROM memo GROUP BY label"
                ).fetchall()
            }
        return Stats(
            total=total,
            ready=per_state.get(STATE_READY, 0),
            inbox=per_state.get(STATE_INBOX, 0),
            disabled=per_state.get(STATE_DISABLED, 0),
            blocked=blocked,
            by_label=by_label,
        )

    # -- 行映射 -----------------------------------------------------------

    def _row_to_memo(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Memo:
        memo_id = int(row["id"])
        scenes = tuple(
            str(item["scene"])
            for item in conn.execute(
                "SELECT scene FROM memo_scene WHERE memo_id = ?", (memo_id,)
            ).fetchall()
        )
        labels = tuple(
            str(item["label"])
            for item in conn.execute(
                "SELECT label FROM memo_label WHERE memo_id = ? ORDER BY label", (memo_id,)
            ).fetchall()
        )
        return Memo(
            id=memo_id,
            rel_path=str(row["rel_path"]),
            content_hash=str(row["content_hash"]),
            percept_hash=row["percept_hash"],
            label=str(row["label"]),
            caption=str(row["caption"] or ""),
            text_in_img=str(row["text_in_img"] or ""),
            scope=str(row["scope"]),
            origin_umo=row["origin_umo"],
            state=str(row["state"]),
            is_fav=bool(row["is_fav"]),
            use_count=int(row["use_count"]),
            created_at=int(row["created_at"]),
            used_at=int(row["used_at"]),
            origin_kind=row["origin_kind"],
            scenes=scenes,
            labels=labels,
            persona_ids=_read_personas(conn, int(row["id"])),
        )


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _clip(text: str, limit: int) -> str:
    return str(text or "")[:limit]


__all__ = [
    "MemoStore",
    "StoreError",
    "MAX_CANDIDATES",
    "MAX_PAGE_SIZE",
    "PERCEPT_SCAN_LIMIT",
    "SCHEMA_VERSION",
]


def _write_personas(
    conn: sqlite3.Connection, memo_id: int, persona_ids: tuple[str, ...] | list[str]
) -> None:
    conn.executemany(
        "INSERT INTO memo_persona(memo_id, persona_id) VALUES(?, ?) "
        "ON CONFLICT(memo_id, persona_id) DO NOTHING",
        [(memo_id, item) for item in _unique(list(persona_ids))],
    )


def _read_personas(conn: sqlite3.Connection, memo_id: int) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT persona_id FROM memo_persona WHERE memo_id = ? ORDER BY persona_id",
        (memo_id,),
    ).fetchall()
    return tuple(str(row["persona_id"]) for row in rows)
