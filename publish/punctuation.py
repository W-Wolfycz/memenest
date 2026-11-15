"""标点情绪先验：从回复的标点猜语气，映射到本插件的 21 类标签。

零成本信号，不需要模型调用，只做正则。强度刻意压得很低（关键词路只值 0.4 分、
向量路只值 0.02），用来在「关键词与向量都读不出情绪」时给一点方向，不参与硬过滤，
也不会盖过真正的语义命中。
"""

from __future__ import annotations

import re

# 引号、代码、URL 里的标点属于被引用者，先剔掉再统计。
_NON_SPEECH = re.compile(
    r"```[\s\S]*?(?:```|$)|`[^`\n]*`|https?://\S+"
    r"|“[^”]*”|「[^」]*」|『[^』]*』|\([^)]{0,30}\)|（[^）]{0,30}）"
)

_QUESTION = re.compile(r"[?？]")
_REPEATED_QUESTION = re.compile(r"[?？]{2,}")
_INTERROBANG = re.compile(r"[?？]+[!！]+|[!！]+[?？]+")
_EXCLAMATION = re.compile(r"[!！]")
_STRONG_EXCLAMATION = re.compile(r"[!！]{2,}")
_ELLIPSIS = re.compile(r"\.{3,}|…+")
_TILDE = re.compile(r"[~～]+")


def expected_labels(text: str) -> set[str]:
    """返回标点强烈暗示的标签 key 集合；没有可用信号时返回空集。"""
    clean = _NON_SPEECH.sub(" ", text or "")
    # Markdown 引用行是别人的话，不算机器人自己的语气
    clean = re.sub(r"(?m)^\s*>[^\n]*", " ", clean)
    if not any(char.isalnum() for char in clean):
        return set()

    labels: set[str] = set()
    if _INTERROBANG.search(clean):
        labels.update({"wow", "puzzle"})
    elif _REPEATED_QUESTION.search(clean):
        labels.add("puzzle")
    elif _QUESTION.search(clean):
        labels.add("puzzle")

    if _STRONG_EXCLAMATION.search(clean):
        labels.update({"wow", "cheer"})
    elif _EXCLAMATION.search(clean):
        labels.add("cheer")

    if _ELLIPSIS.search(clean):
        labels.update({"blank", "slack"})
    if _TILDE.search(clean):
        labels.update({"shy", "love"})
    return labels


__all__ = ["expected_labels"]
