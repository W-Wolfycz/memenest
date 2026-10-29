"""选择打分（R-9）：纯函数，无 IO，候选集由调用方传入。

硬要求：先过作用域（fail-closed），再打分；回合标记置位时直接返回 None。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from .gate import session_key
from ..domain import GENERIC_PERSONA, SCOPE_ORIGIN, Memo

KEYWORD_MIN = 2
KEYWORD_MAX = 6
STOP_WORDS: frozenset[str] = frozenset(
    {
        "什么",
        "这个",
        "那个",
        "就是",
        "不是",
        "我们",
        "你们",
        "他们",
        "哈哈",
        "可以",
        "怎么",
        "还是",
        "然后",
        "但是",
        "因为",
        "所以",
        "已经",
        "可能",
        "一个",
        "现在",
    }
)
_TOKEN_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+")


@dataclass(frozen=True, slots=True)
class PickWeights:
    overlap: float = 3.0
    scene: float = 2.5
    caption: float = 1.5
    usage: float = 1.0
    fav: float = 0.8
    recency: float = 2.0
    jitter: float = 0.05


DEFAULT_WEIGHTS = PickWeights()


@dataclass(slots=True)
class PickContext:
    umo: str | None = None
    keywords: tuple[str, ...] = ()
    """机器人回复切出的词：匹配 text_in_image 与 caption。"""
    context_keywords: tuple[str, ...] = ()
    """用户消息切出的词：匹配 scenes。话题词没有说话人，回复那路也会参与。"""
    recent_ids: tuple[int, ...] = ()
    weights: PickWeights = DEFAULT_WEIGHTS
    turn_blocked: bool = False
    rng: random.Random | None = None
    persona_id: str | None = None
    persona_filter: bool = True
    min_score: float = 0.0
    """最高分低于它就不发。0 表示不设限——包括全场负分时照常发。"""


def extract_keywords(text: str, limit: int = KEYWORD_MAX) -> tuple[str, ...]:
    """中文用相邻二字片段、英文用整词；去停用词与纯数字片段。"""
    cleaned = _TOKEN_SPLIT.sub(" ", text or "")
    found: list[str] = []
    for chunk in cleaned.split():
        chunk = chunk.strip()
        if len(chunk) < KEYWORD_MIN:
            continue
        if chunk.isascii():
            token = chunk.lower()
            if token.isdigit() or token in STOP_WORDS:
                continue
            if token not in found:
                found.append(token)
            continue
        for index in range(len(chunk) - 1):
            token = chunk[index : index + 2]
            if token in STOP_WORDS or token.isdigit() or token in found:
                continue
            found.append(token)
            if len(found) >= limit:
                return tuple(found)
    return tuple(found[:limit])


def scope_allows(memo: Memo, umo: str | None) -> bool:
    """R-4：scope=origin 只允许在「同一个群 / 同一个私聊」里发。

    比较粒度是 `G:群号` / `F:好友号`，不看是哪台 BOT——同一个群换台 BOT
    还是同一个聊天，没必要分开限制。
    """
    if memo.scope != SCOPE_ORIGIN:
        return True
    current = session_key(umo)
    if not current:
        return False
    target = session_key(memo.origin_umo)
    return bool(target) and current == target


def persona_allows(memo: Memo, persona_id: str | None) -> bool:
    """人格归属：显式通用任何人格都能用；绑了人格的命中其一即可；全空则谁都不用。"""
    if GENERIC_PERSONA in memo.persona_ids:
        return True
    if not memo.persona_ids:
        return False
    return bool(persona_id) and persona_id in memo.persona_ids


def filter_scope(candidates: list[Memo], umo: str | None) -> list[Memo]:
    return [memo for memo in candidates if scope_allows(memo, umo)]


def filter_for_session(
    candidates: list[Memo], umo: str | None, persona_id: str | None = None
) -> list[Memo]:
    return [
        memo
        for memo in candidates
        if scope_allows(memo, umo) and persona_allows(memo, persona_id)
    ]


def _hit(keywords: tuple[str, ...], text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def score_memo(memo: Memo, context: PickContext) -> float:
    weights = context.weights
    score = 0.0
    # 机器人回复：我说了什么，配哪张图
    reply_hits = context.keywords
    if reply_hits:
        if _hit(reply_hits, memo.text_in_img):
            score += weights.overlap
        if _hit(reply_hits, memo.caption):
            score += weights.caption
    # 话题词：谁说都算——机器人自己在聊这个话题，或者用户在聊
    if memo.scenes:
        topics = memo.scenes
        if (user_hits := context.context_keywords) and any(
            _hit(user_hits, scene) for scene in topics
        ):
            score += weights.scene
        elif reply_hits and any(_hit(reply_hits, scene) for scene in topics):
            score += weights.scene
    score += weights.usage * min(1.0, memo.use_count / 10.0)
    if memo.is_fav:
        score += weights.fav
    if context.recent_ids and memo.id in context.recent_ids:
        position = context.recent_ids.index(memo.id)
        span = max(1, len(context.recent_ids))
        score -= weights.recency * (1.0 + (span - position) / span)
    rng = context.rng
    jitter_value = rng.random() if rng is not None else random.random()
    score += weights.jitter * jitter_value
    return score


def pick_best(
    candidates: list[Memo], context: PickContext
) -> tuple[Memo | None, bool]:
    """返回 (选中的表情, 是否因为分数不够而放弃)。

    第二个值只在下限真的开起来时才可能为 True：`min_score=0` 表示不设限，
    此时哪怕全场负分（刚用过一轮的小库）也要照常选一张，不能当成「不够相关」。
    """
    if context.turn_blocked:
        return None, False
    if context.persona_filter:
        pool = filter_for_session(candidates, context.umo, context.persona_id)
    else:
        pool = filter_scope(candidates, context.umo)
    if not pool:
        return None, False
    best: Memo | None = None
    best_score = float("-inf")
    for memo in pool:
        value = score_memo(memo, context)
        if value > best_score:
            best, best_score = memo, value
    # 下限：所有候选都没命中关键词时，剩下的分只来自使用次数/收藏/随机，
    # 这种分数发出去就是「硬凑一张」，不如这回合安静不发。
    # 注意 recency 惩罚能把整池压到负数，所以必须先判「开关有没有开」。
    if best is not None and context.min_score > 0 and best_score < context.min_score:
        return None, True
    return best, False


__all__ = [
    "DEFAULT_WEIGHTS",
    "PickContext",
    "PickWeights",
    "extract_keywords",
    "filter_for_session",
    "filter_scope",
    "persona_allows",
    "pick_best",
    "scope_allows",
    "score_memo",
]
