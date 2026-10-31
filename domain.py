"""共享领域模型：标签体系、数据载体、原因码。本模块不依赖宿主框架，可独立单测。"""

from __future__ import annotations

import re

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 标签体系（一套固定集合；模型返回体系外的值统一落到 other）
# ---------------------------------------------------------------------------

LABEL_KEYS: tuple[str, ...] = (
    "joy",
    "laugh",
    "smug",
    "kudos",
    "cheer",
    "love",
    "shy",
    "sad",
    "cry",
    "upset",
    "disgust",
    "blank",
    "awkward",
    "puzzle",
    "wow",
    "alarm",
    "drowsy",
    "slack",
    "refuse",
    "expect",
    "other",
)

LABEL_NAMES: dict[str, str] = {
    "joy": "开心",
    "laugh": "大笑",
    "smug": "得意",
    "kudos": "称赞",
    "cheer": "鼓励",
    "love": "心动",
    "shy": "害羞",
    "sad": "难过",
    "cry": "哭泣",
    "upset": "生气",
    "disgust": "嫌弃",
    "blank": "无语",
    "awkward": "尴尬",
    "puzzle": "疑惑",
    "wow": "震惊",
    "alarm": "害怕",
    "drowsy": "疲惫",
    "slack": "摸鱼",
    "refuse": "拒绝",
    "expect": "期待",
    "other": "其他",
}

# 中文别名只用于「模型说了中文」的宽进：一律映射到同一套 key。
LABEL_ALIASES: dict[str, str] = {
    "开心": "joy",
    "高兴": "joy",
    "快乐": "joy",
    "大笑": "laugh",
    "笑死": "laugh",
    "爆笑": "laugh",
    "搞笑": "laugh",
    "得意": "smug",
    "坏笑": "smug",
    "奸笑": "smug",
    "称赞": "kudos",
    "厉害": "kudos",
    "佩服": "kudos",
    "鼓励": "cheer",
    "加油": "cheer",
    "打气": "cheer",
    "心动": "love",
    "喜欢": "love",
    "爱了": "love",
    "害羞": "shy",
    "脸红": "shy",
    "难过": "sad",
    "伤心": "sad",
    "委屈": "sad",
    "哭泣": "cry",
    "大哭": "cry",
    "流泪": "cry",
    "生气": "upset",
    "愤怒": "upset",
    "嫌弃": "disgust",
    "不屑": "disgust",
    "轻蔑": "disgust",
    "鄙视": "disgust",
    "白眼": "disgust",
    "无语": "blank",
    "沉默": "blank",
    "尴尬": "awkward",
    "社死": "awkward",
    "疑惑": "puzzle",
    "困惑": "puzzle",
    "不解": "puzzle",
    "震惊": "wow",
    "惊讶": "wow",
    "吃惊": "wow",
    "害怕": "alarm",
    "惊恐": "alarm",
    "疲惫": "drowsy",
    "累": "drowsy",
    "困": "drowsy",
    "摸鱼": "slack",
    "摆烂": "slack",
    "躺平": "slack",
    "拒绝": "refuse",
    "不要": "refuse",
    "不行": "refuse",
    "期待": "expect",
    "等待": "expect",
    "盼望": "expect",
    "其他": "other",
}

FALLBACK_LABEL = "other"

GENERIC_PERSONA = "*"
"""关联表里代表「通用」的标记：任何人格都能用。"""

SCOPE_ANY = "any"
SCOPE_ORIGIN = "origin"
SCOPES = (SCOPE_ANY, SCOPE_ORIGIN)

STATE_INBOX = "inbox"
STATE_READY = "ready"
STATE_DISABLED = "disabled"
"""人工停用：留在库里但不会被挑中发送。"""
STATES = (STATE_INBOX, STATE_READY, STATE_DISABLED)

ORIGIN_CHAT = "chat"
ORIGIN_ADMIN = "admin"
ORIGIN_IMPORT = "import"

MAX_SCENES = 3

MAX_SCENE_CHARS = 6
"""单个话题词的字数上限；超了整条丢弃，不截断——写成长句说明跑偏了。"""
MAX_LABELS = 3
"""附加主标签上限；加上主标签，一条表情最多覆盖 5 类情绪。"""
MAX_CAPTION = 80
# 图上的台词常常比一句话长（梗图里整段对话都有），单独给一个更宽的上限
MAX_TEXT_IN_IMAGE = 200
MAX_CANDIDATES = 5


def clean_topics(items: object) -> list[str]:
    """话题词：去空、去重、超长整条丢弃（不截断），最多 MAX_SCENES 个。

    截断在这里是有害的：6 字上限下切出来的是半截废词（「这需求文档写」），
    会一起进关联表、关键词路和 scene 向量，静默拉低检索质量。
    """
    if items is None:
        return []
    if isinstance(items, str):
        candidates: list[object] = items.splitlines()
    elif isinstance(items, (list, tuple, set)):
        candidates = list(items)
    else:
        candidates = [items]
    result: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text or len(text) > MAX_SCENE_CHARS:
            continue
        if not any(ch.isalnum() for ch in text):
            continue
        if text in result:
            continue
        result.append(text)
        if len(result) >= MAX_SCENES:
            break
    return result


_LABEL_TOKEN_SPLIT = re.compile(r"[\s,，、;；/|]+")


def normalize_label(value: object) -> str:
    """把任意模型输出收敛到固定的 21 类之一；无法识别时返回 other。

    模型可能给 "joy, disgust"、"开心、嫌弃"、甚至照抄提示词里的 "disgust(嫌弃/不屑)"，
    这些都取第一个能认出来的词，不要因为多写了点东西就整条丢成 other。
    """
    if isinstance(value, (list, tuple)):
        # label 位置给了数组：取第一个元素
        for item in value:
            key = normalize_label(item)
            if key != FALLBACK_LABEL:
                return key
        return FALLBACK_LABEL
    text = str(value or "").strip().strip("\"'`[]【】「」()（）").strip().lower()
    if not text:
        return FALLBACK_LABEL
    if text in LABEL_NAMES:
        return text
    if text in LABEL_ALIASES:
        return LABEL_ALIASES[text]
    # 括号里常带中文释义：disgust(嫌弃/不屑) → disgust
    head = re.split(r"[(（\[]", text, maxsplit=1)[0].strip()
    if head in LABEL_NAMES:
        return head
    if head in LABEL_ALIASES:
        return LABEL_ALIASES[head]
    # 再按标点切开逐个试
    for token in _LABEL_TOKEN_SPLIT.split(text):
        token = token.strip()
        if not token:
            continue
        if token in LABEL_NAMES:
            return token
        if token in LABEL_ALIASES:
            return LABEL_ALIASES[token]
    return FALLBACK_LABEL


def normalize_labels(values: object, exclude: str | None = None) -> list[str]:
    """多主标签：逐个归一、去重、去掉与主标签重复的项，最多 MAX_LABELS 个。"""
    if values is None:
        return []
    items = [values] if isinstance(values, (str, bytes)) else list(values)  # type: ignore[arg-type]
    result: list[str] = []
    for item in items:
        key = normalize_label(item)
        if key == FALLBACK_LABEL or key == exclude or key in result:
            continue
        result.append(key)
        if len(result) >= MAX_LABELS:
            break
    return result
def label_display(key: str) -> str:
    return LABEL_NAMES.get(key, LABEL_NAMES[FALLBACK_LABEL])


# ---------------------------------------------------------------------------
# 原因码：数字码 + 短名。失败原因必须在返回值与日志里可区分。
# ---------------------------------------------------------------------------

class Reason:
    """拒绝原因。编号按发送链路的判定顺序排，不是字典序。"""

    OK = "001 ok"
    SWITCH_OFF = "002 switch_off"
    TURN_USED = "003 turn_used"
    TURN_SUPERSEDED = "004 turn_superseded"
    REPLY_HAS_IMAGE = "005 reply_has_image"
    SESSION_DENIED = "006 session_denied"
    SESSION_COOLDOWN = "007 session_cooldown"
    TEXT_TOO_SHORT = "008 text_too_short"
    TEXT_IS_COMMAND = "009 text_is_command"
    TEXT_TOO_MANY_QUESTIONS = "010 text_too_many_questions"
    DICE_LOST = "011 dice_lost"
    NO_CANDIDATE = "012 no_candidate"
    LOW_RELEVANCE = "013 low_relevance"
    SCOPE_DENIED = "014 scope_denied"
    PERSONA_DENIED = "015 persona_denied"
    FILE_MISSING = "016 file_missing"
    BAD_HANDLE = "017 bad_handle"
    STALE_HANDLE = "018 stale_handle"
    FEATURE_OFF = "019 feature_off"
    SEND_FAILED = "020 send_failed"


# ---------------------------------------------------------------------------
# 数据载体
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MediaRef:
    """一条待抓取的媒体引用。只在收集阶段存在，不含任何用户可读文本。"""

    source: str
    declared_ext: str
    origin_kind: str
    platform: str
    reason: str
    is_animated: bool = False


@dataclass(slots=True)
class Analysis:
    label: str
    scenes: list[str] = field(default_factory=list)
    text_in_image: str = ""
    caption: str = ""
    labels: list[str] = field(default_factory=list)
    """附加主标签（不含 label 本身）；用于同一条表情覆盖多种情绪。"""


@dataclass(slots=True)
class Draft:
    """入库草案：文件已就位后交给 store 的完整一行。"""

    rel_path: str
    content_hash: str
    percept_hash: str | None
    analysis: Analysis
    scope: str
    origin_umo: str | None
    origin_kind: str
    created_at: int
    state: str = STATE_INBOX
    persona_ids: tuple[str, ...] = ()
    """绑定的人格；空表示通用（任何人格都能用）。"""


@dataclass(slots=True)
class Memo:
    id: int
    rel_path: str
    content_hash: str
    percept_hash: str | None
    label: str
    caption: str
    text_in_img: str
    scope: str
    origin_umo: str | None
    state: str
    is_fav: bool
    use_count: int
    created_at: int
    used_at: int
    origin_kind: str | None
    scenes: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    persona_ids: tuple[str, ...] = ()
    """绑定的人格；空表示通用。"""

    @property
    def haystack(self) -> str:
        parts = [self.caption, self.text_in_img, label_display(self.label), self.label]
        parts.extend(label_display(key) for key in self.labels)
        parts.extend(self.labels)
        parts.extend(self.scenes)
        return " ".join(part for part in parts if part)

    @property
    def all_labels(self) -> tuple[str, ...]:
        return (self.label, *self.labels)

    @property
    def persona_generic(self) -> bool:
        """显式通用（关联表里带 *）；全空表示谁都不用。"""
        return GENERIC_PERSONA in self.persona_ids


@dataclass(slots=True)
class PageQuery:
    state: str | None = None
    label: str | None = None
    page: int = 1
    size: int = 10


@dataclass(slots=True)
class CandidateQuery:
    """检索候选：状态、作用域、人格与关键词在 SQL 侧收敛，打分交给 publish/pick.py。"""

    state: str = STATE_READY
    label: str | None = None
    umo: str | None = None
    allow_origin: bool = True
    limit: int = 60
    persona_id: str | None = None
    persona_filter: bool = True
    ignore_scope: bool = False
    """缓存层预载全量候选时用：作用域在内存里按会话过滤。"""


@dataclass(slots=True)
class Stats:
    total: int = 0
    ready: int = 0
    inbox: int = 0
    disabled: int = 0
    blocked: int = 0
    by_label: dict[str, int] = field(default_factory=dict)
