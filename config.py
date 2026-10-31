"""配置读取与校验：只做 dict → 不可变设置的转换，不碰任何 IO。

配置项在界面上分 4 组（基础 / 收集与并发 / 识别与入库 / 发表情），顶层另有 log_with_bot_id；
读的时候先展平成一张表再取值，所以分组只是展示结构，代码里仍是一层字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PLUGIN_NAME = "memenest"
"""插件唯一名：数据目录与 Web API 前缀都用它，必须写全名避免撞库。"""

LOG_TAG = "MemeNest"
COMMAND_NAME = "memenest"
"""固定指令名。指令名不再做配置项，避免用户改完自己都找不到入口。"""

PERCEPT_THRESHOLD = 8
"""同图判定容差（0~64）。内部常量，不暴露成配置项。"""

"""强制收录默认时长。/memenest grab 60 可以临时改。"""

THROTTLE_MODES = ("ratio", "cooldown")

GROUP_KEYS: tuple[str, ...] = (
    "basic_settings",
    "collect_settings",
    "classify_settings",
    "send_settings",
    "semantic_settings",
)
"""界面上的分组容器名。只有这些键会被展平，避免把用户数据（如别名表）也拆开。"""


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(
    value: Any, default: int, minimum: int | None = None, maximum: int | None = None
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        parsed = minimum
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed


VARIETY_DELTA: dict[str, float] = {
    "stable": 0.02,
    "balanced": 0.05,
    "lively": 0.10,
}
"""选图变化程度对应的分数窗口。实测（500 张库，同一句话连发 3 次）：
stable 57% 还是同一张，balanced 25%，lively 经常换但平均名次仍在 2.5 以内。"""

DEFAULT_VARIETY = "balanced"

# ---- 写死的调优值 ----
# 这 4 个权重是在 500 张真实表情包上扫出来的（用户查询 + 机器人回复两组各 1498 条）：
# 最优组合 0.30/0.20/0.40 与 0.15/0.25/0.30 只差 1.2 个百分点，属于噪声范围，
# 所以取后者——它对「用户说的话」更友好。暴露成配置项只会让用户调坏。
WEIGHT_TEXT = 0.15
WEIGHT_CAPTION = 0.25
WEIGHT_SCENES = 0.30
WEIGHT_LABEL = 0.10

# 相似度下限：实测「该发」和「不该发」的分数区间重叠，开这个只会误杀，
# 留给关键词兜底那条路去管「宁可不发」。
EMBEDDING_THRESHOLD = 0.0

# 纯技术参数：默认值在所有实测场景下都够用，没有调的必要。
QUEUE_CAPACITY = 32
MAX_PIXELS_MP = 40
SEND_MAX_SIDE = 512
SEND_MAX_KB = 512.0



def _as_variety(value: object) -> str:
    """只认三个档位，写坏了回落到默认，不让配置错误变成随机行为。"""
    text = str(value or "").strip().lower()
    return text if text in VARIETY_DELTA else DEFAULT_VARIETY


def _as_float(value: Any, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _as_choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in choices else default


def _as_str_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        return ()
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def _as_whitelist(value: Any) -> tuple[str, ...]:
    """发送白名单只认 F:xxx / G:xxx，其它写法直接忽略。"""
    items = _as_str_list(value)
    kept: list[str] = []
    for item in items:
        text = item.strip()
        if len(text) > 2 and text[1] == ":" and text[0].upper() in ("F", "G"):
            kept.append(text[0].upper() + text[1:])
    return tuple(kept)


def flatten_config(raw: Any, depth: int = 0) -> dict[str, Any]:
    """把界面分组展平成一层的键值表；只展开已知分组，其它字典原样保留。"""
    if not isinstance(raw, dict) or depth > 3:
        return {}
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if key in GROUP_KEYS and isinstance(value, dict):
            flat.update(flatten_config(value, depth + 1))
        else:
            flat[key] = value
    return flat


@dataclass(frozen=True, slots=True)
class Settings:
    """一次加载得到的不可变设置快照；运行期改配置由插件重建快照。"""

    collect_enabled: bool = True
    collect_all_images: bool = False
    collect_blacklist: tuple[str, ...] = ()
    throttle_mode: str = "ratio"
    collect_ratio: float = 0.3
    cooldown_seconds: int = 30
    worker_count: int = 2
    max_file_mb: float = 32.0
    vision_provider_id: str = ""
    review_enabled: bool = True
    library_cap: int = 800
    send_enabled: bool = True
    send_tools_enabled: bool = True
    send_ratio: float = 0.25
    send_delay_seconds: int = 5
    send_cooldown_seconds: int = 120
    send_compress: bool = True
    persona_isolation: bool = True
    embedding_enabled: bool = False
    embedding_provider_id: str = ""
    # 关键词兜底路的最低分：低于它就不发（0 = 关闭，永远发）
    min_keyword_score: float = 2.0
    pick_variety: str = "balanced"
    # 四路检索权重。text=图上文字（机器人说这句时用），caption=画面描述，
    # scenes=话题词（谁说都算），label=情绪标签。
    # 标签怎么取：vector=用标签向量比对（免费），llm=让模型萃取（更准、多一次调用）
    label_extraction: str = "vector"
    send_whitelist: tuple[str, ...] = ()
    log_with_bot_id: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def max_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    @property
    def max_pixels(self) -> int:
        return MAX_PIXELS_MP * 1_000_000

    @property
    def send_max_bytes(self) -> int:
        return int(SEND_MAX_KB * 1024)

    @property
    def command_prefix(self) -> str:
        return COMMAND_NAME


def flattened_keys(raw: dict[str, Any] | None) -> set[str]:
    """展平后出现的所有键（含已废弃的），用于启动时提示一次配置迁移。"""
    return set(flatten_config(raw))


def deprecated_keys(raw: dict[str, Any] | None) -> list[str]:
    """不在当前 schema 里的键（旧的指令名、黑名单、感知阈值等）。"""
    return sorted(flattened_keys(raw) - _KNOWN_KEYS)


def load_settings(raw: dict[str, Any] | None, *, warn: Any = None) -> Settings:
    """把宿主配置字典转成设置快照，任何非法值都退回默认值而不是抛异常。"""
    data = flatten_config(raw)
    dropped = sorted(set(data) - _KNOWN_KEYS)
    if dropped and warn is not None:
        warn("忽略已废弃的配置项：%s", ", ".join(dropped))
    return Settings(
        collect_enabled=_as_bool(data.get("collect_enabled"), True),
        collect_all_images=_as_bool(data.get("collect_all_images"), False),
        collect_blacklist=_as_whitelist(data.get("collect_blacklist")),
        throttle_mode=_as_choice(data.get("throttle_mode"), THROTTLE_MODES, "ratio"),
        collect_ratio=_as_float(data.get("collect_ratio"), 0.3),
        cooldown_seconds=_as_int(data.get("cooldown_seconds"), 30, 0, 86_400),
        worker_count=_as_int(data.get("worker_count"), 2, 1, 8),
        max_file_mb=_as_float(data.get("max_file_mb"), 32.0, 0.1, 1024.0),
        vision_provider_id=str(data.get("vision_provider_id") or "").strip(),
        review_enabled=_as_bool(data.get("review_enabled"), True),
        library_cap=_as_int(data.get("library_cap"), 800, 10, 100_000),
        send_enabled=_as_bool(data.get("send_enabled"), True),
        send_tools_enabled=_as_bool(data.get("send_tools_enabled"), True),
        send_ratio=_as_float(data.get("send_ratio"), 0.25),
        send_delay_seconds=_as_int(data.get("send_delay_seconds"), 5, 0, 120),
        send_cooldown_seconds=_as_int(data.get("send_cooldown_seconds"), 120, 0, 86_400),
        send_compress=_as_bool(data.get("send_compress"), True),
        persona_isolation=_as_bool(data.get("persona_isolation"), True),
        embedding_enabled=_as_bool(data.get("embedding_enabled"), False),
        embedding_provider_id=str(data.get("embedding_provider_id") or "").strip(),
        min_keyword_score=_as_float(data.get("min_keyword_score"), 2.0, 0.0, 10.0),
        pick_variety=_as_variety(data.get("pick_variety")),
        label_extraction=(
            "llm"
            if str(data.get("label_extraction") or "").strip().lower() == "llm"
            else "vector"
        ),
        send_whitelist=_as_whitelist(data.get("send_whitelist")),
        log_with_bot_id=_as_bool(data.get("log_with_bot_id"), False),
        extra={k: v for k, v in data.items() if k not in _KNOWN_KEYS},
    )


_KNOWN_KEYS = frozenset(
    {
        "collect_enabled",
        "collect_all_images",
        "collect_blacklist",
        "throttle_mode",
        "collect_ratio",
        "cooldown_seconds",
            "worker_count",
        "max_file_mb",
            "vision_provider_id",
        "review_enabled",
        "library_cap",
        "send_enabled",
        "send_tools_enabled",
        "send_ratio",
        "send_delay_seconds",
        "send_cooldown_seconds",
        "send_compress",
                "persona_isolation",
        "embedding_enabled",
        "embedding_provider_id",
            "min_keyword_score",
    "pick_variety",
                        "label_extraction",
        "send_whitelist",
        "log_with_bot_id",
    }
)
