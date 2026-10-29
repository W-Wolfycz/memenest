"""去重判定（R-5）：先用便宜的哈希精确比对，再用感知指纹近似比对。

这里刻意做成接收「可注入的 store」的纯协调函数：SQL 在 store，位运算在
fingerprints，调用方可单测判定顺序而不碰数据库。
"""

from __future__ import annotations

from dataclasses import dataclass

from .fingerprints import hamming


@dataclass(slots=True)
class DedupVerdict:
    duplicate: bool
    reason: str
    existing_id: int | None = None


class _PerceptSource:
    """只暴露去重需要的两个查询，方便测试替身。"""

    def is_blocked(self, content_hash: str) -> bool:  # pragma: no cover - protocol
        raise NotImplementedError

    def match_content_hash(self, content_hash: str) -> int | None:  # pragma: no cover
        raise NotImplementedError

    def percept_rows(self, limit: int = 2000) -> list[tuple[int, str]]:  # pragma: no cover
        raise NotImplementedError


def find_duplicate(
    store: _PerceptSource,
    content_hash: str,
    percept_hash: str | None,
    threshold: int,
    percept_rows: list[tuple[int, str]] | None = None,
) -> DedupVerdict:
    if store.is_blocked(content_hash):
        return DedupVerdict(True, "blocked")
    existing = store.match_content_hash(content_hash)
    if existing is not None:
        return DedupVerdict(True, "duplicate_bytes", existing)
    if not percept_hash or threshold <= 0:
        return DedupVerdict(False, "unique")
    rows = percept_rows if percept_rows is not None else store.percept_rows()
    for memo_id, candidate in rows:
        if hamming(percept_hash, candidate) <= threshold:
            return DedupVerdict(True, "duplicate_percept", memo_id)
    return DedupVerdict(False, "unique")


__all__ = ["DedupVerdict", "find_duplicate"]
