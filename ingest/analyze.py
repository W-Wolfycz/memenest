"""分类（F2）：一次模型调用 + 容错解析（P-1~P-5）。

模型调用通过 llm_call 注入，本模块不 import 宿主框架；动图合成与临时文件清理
都在这里闭环（R-7：超时取消路径同样会删）。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain import (
    LABEL_KEYS,
    LABEL_NAMES,
    MAX_CAPTION,
    MAX_LABELS,
    MAX_SCENE_CHARS,
    MAX_SCENES,
    MAX_TEXT_IN_IMAGE,
    Analysis,
    normalize_label,
    normalize_labels,
)
from . import frames

LLMCall = Callable[[str, list[str]], Awaitable[str]]

_QUOTE_CHARS = "\"'`“”‘’"
_WRAPPER_PAIRS = (
    ("[", "]"),
    ("【", "】"),
    ("（", "）"),
    ("(", ")"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
)
_TRAILING_PUNCT = "。，、；：！？,.;:!?~…—-"
# 中日朝文字范围：这些字符之间的空白是排版产物，不是词间分隔
_CJK_RANGE = (
    "\u4e00-\u9fff"      # 中日韩统一表意文字
    "\u3040-\u30ff"      # 日文假名
    "\uac00-\ud7af"      # 韩文
    "\u3000-\u303f"      # 中文标点（、。〈〉《》「」【】）
    "\uff00-\uffef"      # 全角标点（，！？：；（））
    "\u2014\u2026"        # 破折号与省略号
)
_LIST_SPLIT = re.compile(r"[,，;；/、|]+")
PROMPT_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass(slots=True)
class AnalyzeOutcome:
    analysis: Analysis | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.analysis is not None


# ---------------------------------------------------------------------------
# 解析契约（P-1 ~ P-3）
# ---------------------------------------------------------------------------


def squeeze_space(text: str) -> str:
    """压空白：中文之间的换行与空格删掉，英文的换行变空格。

    模型转写气泡里的多行文字时会带回换行。中文排版里换行不代表空格，
    留着会变成「我不评价 这个了」；但英文不能删，`no\nidea` 会粘成 `noidea`。
    """
    # 换行先统一变空格，再由下一行把中日朝文字之间的空格删掉：
    # 中文里换行不代表空格，英文里必须留一个，两步合起来各语言都对。
    text = re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(rf"(?<=[{_CJK_RANGE}])\s+(?=[{_CJK_RANGE}])", "", text)
    return text.strip()


def clean_text(value: object) -> str:
    """逐字段清洗：去包裹引号/括号、去尾随标点、压空白。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    text = squeeze_space(str(value))
    changed = True
    while changed and text:
        changed = False
        if text[0] in _QUOTE_CHARS or text[-1] in _QUOTE_CHARS:
            text = text.strip(_QUOTE_CHARS).strip()
            changed = True
        for left, right in _WRAPPER_PAIRS:
            if text.startswith(left) and text.endswith(right) and len(text) >= 2:
                text = text[len(left) : -len(right)].strip()
                changed = True
    return text.rstrip(_TRAILING_PUNCT).strip()


def clean_list(value: object, limit: int, item_limit: int = MAX_SCENE_CHARS) -> list[str]:
    """把任意形态转成去空、去重、限长的字符串数组。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = _LIST_SPLIT.split(value)
    elif isinstance(value, (list, tuple, set)):
        # 列表里每项本来就是一条完整条目；逗号是句内的停顿，再切会把一句话拆成两条，
        # 再撞上 MAX_SCENES 截断，整条 scene 就这么没了。
        raw_items = list(value)
    else:
        raw_items = [value]

    result: list[str] = []
    for item in raw_items:
        text = clean_text(item)
        if not text or len(text) > item_limit:
            continue
        if not any(ch.isalnum() for ch in text):
            continue
        if text in result:
            continue
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _first_json_object(raw: str) -> dict | None:
    """P-1：取出第一个可解析的 JSON 对象，容忍前后缀与代码围栏。"""
    decoder = json.JSONDecoder()
    text = raw or ""
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _build_analysis(payload: dict) -> Analysis:
    """P-3：字段缺失用默认值补齐，不因为缺字段丢掉整条。"""
    label = normalize_label(payload.get("label") or payload.get("class"))
    extra_labels = normalize_labels(
        payload.get("labels") or payload.get("also") or payload.get("emotions"),
        exclude=label,
    )
    return Analysis(
        label=label,
        scenes=clean_list(payload.get("scenes") or payload.get("situations"), MAX_SCENES),
        text_in_image=clean_text(payload.get("text_in_image"))[:MAX_TEXT_IN_IMAGE],
        caption=clean_text(payload.get("caption") or payload.get("desc"))[:MAX_CAPTION],
        labels=extra_labels[:MAX_LABELS],
    )


def parse_analysis(raw: str) -> Analysis | None:
    """P-4：整体不可解析返回 None（调用方按失败路径处理，不入库）。"""
    payload = _first_json_object(raw or "")
    if payload is None:
        return None
    return _build_analysis(payload)


# ---------------------------------------------------------------------------
# 提示词渲染（P-5：不用 str.format，避免 JSON 花括号炸掉）
# ---------------------------------------------------------------------------


def render_prompt(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return PROMPT_PLACEHOLDER.sub(replace, template)


# 只给「光看中文名会选错」的 key 补一层释义；其余直接用 LABEL_NAMES
_LABEL_GLOSS = {
    "disgust": "嫌弃/不屑",
    "blank": "无语/被噎住",
    "refuse": "拒绝/不接话",
}


def build_prompt(template: str) -> str:
    # 带上中文名：模型对 joy/smug/awkward 这类英文 key 的把握不如「得意/尴尬」
    keys = " | ".join(
        f"{key}({_LABEL_GLOSS.get(key) or LABEL_NAMES.get(key, key)})" for key in LABEL_KEYS
    )
    return render_prompt(template, {"label_keys": keys})


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------


def _summarize_failure(raw: str) -> str:
    """只保留形状信息，不把模型正文写进日志。"""
    if not raw:
        return "model_empty"
    return f"model_unparsable(len={len(raw)})"


async def analyze_detailed(
    path: Path,
    *,
    prompt: str,
    llm_call: LLMCall,
    timeout: float | None = None,
    work_dir: Path | None = None,
) -> AnalyzeOutcome:
    """一次模型调用，绝不重试（I-1）。动图先合成一张静态拼图（R-7）。"""
    grid_path: Path | None = None
    try:
        if frames.has_animation(path) and work_dir is not None:
            candidate = work_dir / f"{path.stem}-grid.jpg"
            if await asyncio.to_thread(frames.compose_grid, path, candidate):
                grid_path = candidate
        image_path = grid_path or path
        try:
            call = llm_call(prompt, [str(image_path)])
            if timeout and timeout > 0:
                raw = await asyncio.wait_for(call, timeout=max(1.0, timeout))
            else:
                # 不设超时：交给 provider 自己的 timeout 配置
                raw = await call
        except asyncio.TimeoutError:
            return AnalyzeOutcome(None, "model_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return AnalyzeOutcome(None, f"model_error_{type(exc).__name__}")
        if not isinstance(raw, str) or not raw.strip():
            return AnalyzeOutcome(None, "model_empty")
        analysis = parse_analysis(raw)
        if analysis is None:
            return AnalyzeOutcome(None, _summarize_failure(raw))
        return AnalyzeOutcome(analysis, "ok")
    finally:
        # 超时/取消路径也会走到这里，这是 R-7 最容易漏的泄漏点。
        if grid_path is not None:
            try:
                grid_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "AnalyzeOutcome",
    "analyze_detailed",
    "build_prompt",
    "clean_list",
    "clean_text",
    "parse_analysis",
    "render_prompt",
    "squeeze_space",
]
