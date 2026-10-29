"""把入站事件拆成待抓取的媒体引用（R-1 判定树）。

本模块刻意不 import 宿主框架：事件对象只按 duck typing 读取，测试里可以用
极简假对象驱动，判定逻辑因此可以完整单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..domain import MediaRef
from .throttle import Throttle

MAX_REFS = 4

# 表情 CDN 特征：命中任意一条即视为平台级表情包（属于我自己的判定集合）。
CDN_MARKERS: tuple[str, ...] = (
    "/sticker",
    "sticker_",
    "/emoji",
    "-emoji",
    "emoji_id=",
    "qzonestyle",
    "gxh.vip.qq.com",
)

ANIMATED_EXTS: tuple[str, ...] = (".gif", ".webp", ".apng")

# 平台给图片打的“这是表情”的 summary 片段。
STICKER_SUMMARY_MARKERS: tuple[str, ...] = ("动画表情", "表情", "sticker")

_EXT_RE = re.compile(r"\.([a-zA-Z0-9]{2,5})(?:$|\?|#)")


@dataclass(slots=True)
class RawMedia:
    """从平台原始消息里抠出来的一段媒体，未经任何判定。"""

    kind: str
    data: dict[str, Any]
    is_animated: bool = False


@dataclass(slots=True)
class JudgeOutcome:
    refs: list[MediaRef] = field(default_factory=list)
    seen: int = 0
    reason: str = ""


# ---------------------------------------------------------------------------
# 事件读取（全部容错，永不抛异常）
# ---------------------------------------------------------------------------


def _platform_name(event: Any) -> str:
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    meta = getattr(event, "platform_meta", None)
    return str(getattr(meta, "name", "") or "")


def _raw_message(event: Any) -> Any:
    obj = getattr(event, "message_obj", None)
    return getattr(obj, "raw_message", None)


def _raw_value(raw: Any, key: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)


def _segment_type(segment: Any) -> str:
    value = _raw_value(segment, "type")
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().lower()


def raw_media(event: Any) -> list[RawMedia]:
    """从 OneBot / Telegram 类原始消息里取出媒体段（顺序保持）。"""
    raw = _raw_message(event)
    if raw is None:
        return []
    segments = _raw_value(raw, "message")
    if not isinstance(segments, (list, tuple)):
        return []
    found: list[RawMedia] = []
    for segment in segments:
        kind = _segment_type(segment)
        if kind in ("image", "mface"):
            data = _raw_value(segment, "data")
            payload = dict(data) if isinstance(data, dict) else {}
            source = str(payload.get("url") or payload.get("file") or "")
            found.append(
                RawMedia(kind=kind, data=payload, is_animated=_looks_animated(source))
            )
    return found


def image_components(event: Any) -> list[Any]:
    getter = getattr(event, "get_messages", None)
    if not callable(getter):
        return []
    try:
        chain = getter() or []
    except Exception:
        return []
    result = []
    for comp in chain:
        type_value = getattr(comp, "type", None)
        name = str(getattr(type_value, "value", type_value) or "")
        if name == "Image":
            result.append(comp)
    return result


def quoted_image_sources(event: Any) -> list[str]:
    """从消息里的引用（Reply）取出被引用消息中的图片源。

    宿主在转换 aiocqhttp 消息时默认就把被引用消息解析进 `Reply.chain`，
    所以这里不用再调 get_msg。

    返回图片源列表（url/file/path 中第一个非空的）；拿不到就返回空列表。
    """
    getter = getattr(event, "get_messages", None)
    if not callable(getter):
        return []
    try:
        chain = getter() or []
    except Exception:
        return []

    found: list[str] = []
    for comp in chain:
        type_value = getattr(comp, "type", None)
        name = str(getattr(type_value, "value", type_value) or "")
        if name != "Reply":
            continue
        inner = getattr(comp, "chain", None) or []
        for item in inner:
            inner_type = getattr(item, "type", None)
            inner_name = str(getattr(inner_type, "value", inner_type) or "")
            if inner_name != "Image":
                continue
            source = component_source(item)
            if source:
                found.append(source)
    return found


def component_source(comp: Any) -> str:
    for key in ("url", "file", "path"):
        value = getattr(comp, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# 判定（R-1）
# ---------------------------------------------------------------------------


def _looks_animated(source: str) -> bool:
    lowered = source.lower()
    if lowered.startswith("data:image/gif"):
        return True
    match = _EXT_RE.search(lowered.split("?", 1)[0])
    ext = f".{match.group(1).lower()}" if match else ""
    return ext in ANIMATED_EXTS


def guess_ext(source: str) -> str:
    if source.startswith("data:image/"):
        head = source[11:].split(";", 1)[0].strip().lower()
        return f".{'jpg' if head == 'jpeg' else head}"
    if source.startswith("base64://"):
        return ""
    match = _EXT_RE.search(source.split("?", 1)[0])
    return f".{match.group(1).lower()}" if match else ""


def _cdn_hit(source: str) -> bool:
    lowered = source.lower()
    return any(marker in lowered for marker in CDN_MARKERS)


def _make_ref(source: str, reason: str, platform: str, is_animated: bool) -> MediaRef | None:
    text = source.strip()
    if not text:
        return None
    return MediaRef(
        source=text,
        declared_ext=guess_ext(text),
        origin_kind="chat",
        platform=platform,
        reason=reason,
        is_animated=is_animated,
    )


def judge_raw_media(
    item: RawMedia, platform: str, accept_all: bool = False
) -> tuple[MediaRef | None, str]:
    """一条原始媒体段是否算表情包。只走 QQ（OneBot）的判定路径。"""
    data = item.data
    url = str(data.get("url") or "").strip()
    file_ref = str(data.get("file") or "").strip()
    source = url or file_ref

    if accept_all:
        ref = _make_ref(source, "collect_all", platform, item.is_animated)
        return (ref, "collect_all") if ref else (None, "no_source")

    if item.kind == "mface":
        ref = _make_ref(source, "sticker_mface", platform, item.is_animated)
        return (ref, "sticker_mface") if ref else (None, "mface_without_source")

    # kind == "image"：有平台标记或用图床特征兜底，都没有就丢弃。
    sub_type = str(data.get("sub_type") or "").strip()
    summary = str(data.get("summary") or "")
    if sub_type in ("1", "hot", "sticker"):
        ref = _make_ref(source, "sticker_subtype", platform, item.is_animated)
        return (ref, "sticker_subtype") if ref else (None, "sticker_without_source")
    if any(marker in summary for marker in STICKER_SUMMARY_MARKERS):
        ref = _make_ref(source, "sticker_summary", platform, item.is_animated)
        return (ref, "sticker_summary") if ref else (None, "sticker_without_source")
    if _cdn_hit(source):
        ref = _make_ref(source, "sticker_cdn", platform, item.is_animated)
        return (ref, "sticker_cdn") if ref else (None, "sticker_without_source")
    return None, "no_sticker_mark"


def judge_component(
    comp: Any, platform: str, accept_all: bool = False
) -> tuple[MediaRef | None, str]:
    source = component_source(comp)
    if not source:
        return None, "component_without_source"
    animated = _looks_animated(source)
    if accept_all:
        return _make_ref(source, "collect_all", platform, animated), "collect_all"
    if _cdn_hit(source):
        return _make_ref(source, "sticker_cdn", platform, animated), "sticker_cdn"
    return None, "no_sticker_mark"


def collect_refs(
    event: Any,
    limit: int = MAX_REFS,
    accept_all: bool = False,
) -> JudgeOutcome:
    """把事件拆成媒体引用：段级标记优先于组件级，最多 limit 条。

    accept_all=True（配置「连普通图片也收」）时，任何能取到来源的图片都收，
    不再区分表情与普通照片。
    """
    platform = _platform_name(event)
    outcome = JudgeOutcome()
    refs: list[MediaRef] = []
    reasons: list[str] = []

    medias = raw_media(event)
    if medias:
        outcome.seen = len(medias)
        for item in medias:
            ref, reason = judge_raw_media(item, platform, accept_all)
            reasons.append(reason)
            if ref is not None and len(refs) < limit:
                refs.append(ref)
    else:
        comps = image_components(event)
        outcome.seen = len(comps)
        for comp in comps:
            ref, reason = judge_component(comp, platform, accept_all)
            reasons.append(reason)
            if ref is not None and len(refs) < limit:
                refs.append(ref)

    outcome.refs = refs
    if refs:
        outcome.reason = refs[0].reason
    elif reasons:
        # 只保留第一条未命中原因，避免日志刷屏；重复原因去重后计数由调用方处理。
        outcome.reason = reasons[0]
    else:
        outcome.reason = "no_media"
    return outcome


COLLECT = "collect"
SKIP_DISABLED = "skip_disabled"
SKIP_NO_MEDIA = "skip_no_media"
SKIP_THROTTLED = "skip_throttled"


@dataclass(slots=True)
class CollectPlan:
    action: str
    reason: str

    @property
    def should_collect(self) -> bool:
        return self.action == COLLECT


def plan_collect(
    *,
    enabled: bool,
    has_refs: bool,
    throttle: Throttle,
    now: float,
) -> CollectPlan:
    """收集决策的唯一入口：开关 → 有无合格媒体 → 节流。

    关键顺序：判定不合格的媒体（普通照片）绝不能消耗节流窗口，
    所以 `has_refs` 的判断必须排在 `throttle.allows()` 之前。
    """
    if not enabled:
        return CollectPlan(SKIP_DISABLED, "collect_disabled")
    if not has_refs:
        return CollectPlan(SKIP_NO_MEDIA, "no_media")
    allowed, reason = throttle.allows(now)
    if not allowed:
        return CollectPlan(SKIP_THROTTLED, reason)
    return CollectPlan(COLLECT, reason)
