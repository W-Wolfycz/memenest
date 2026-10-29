"""测试替身：假事件、假组件、假 store。只实现被测代码真正会读到的那几个属性。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from memenest.domain import GENERIC_PERSONA, STATE_READY, Memo


class FakeImage:
    """最小 Image 组件替身（type/url/file/path 与真实组件同名）。"""

    type = "Image"

    def __init__(self, url: str = "", file: str = "", path: str = "") -> None:
        self.url = url
        self.file = file
        self.path = path


class FakePlain:
    type = "Plain"

    def __init__(self, text: str) -> None:
        self.text = text


@dataclass
class FakeMessageObj:
    message: list[Any] = field(default_factory=list)
    raw_message: Any = None
    group_id: str = ""


class FakeEvent:
    """duck typing 的事件替身：collect / gate / commands 读到的接口都在这里。"""

    def __init__(
        self,
        *,
        message_str: str = "",
        components: list[Any] | None = None,
        raw_segments: list[dict[str, Any]] | None = None,
        platform: str = "aiocqhttp",
        platform_id: str = "BOT1",
        umo: str = "aiocqhttp:GroupMessage:1",
        admin: bool = False,
    ) -> None:
        raw = {"message": raw_segments} if raw_segments is not None else None
        self.message_obj = FakeMessageObj(message=components or [], raw_message=raw)
        self.message_str = message_str
        self.platform = platform
        self.platform_id = platform_id
        self.unified_msg_origin = umo
        self.role = "admin" if admin else "member"
        self._result = None
        self.call_llm = False

    def get_messages(self) -> list[Any]:
        return list(self.message_obj.message)

    def get_message_str(self) -> str:
        return self.message_str

    def get_platform_name(self) -> str:
        return self.platform

    def get_platform_id(self) -> str:
        return self.platform_id

    def is_admin(self) -> bool:
        return self.role == "admin"

    def set_result(self, result: Any) -> None:
        self._result = result

    def get_result(self) -> Any:
        return self._result

    def should_call_llm(self, value: bool) -> None:
        self.call_llm = value


@dataclass
class FakeMemoSource:
    """find_duplicate 需要的三个查询，全部可由用例注入。"""

    blocked: set[str] = field(default_factory=set)
    rows: list[tuple[int, str]] = field(default_factory=list)
    by_hash: dict[str, int] = field(default_factory=dict)

    def is_blocked(self, content_hash: str) -> bool:
        return content_hash in self.blocked

    def match_content_hash(self, content_hash: str) -> int | None:
        return self.by_hash.get(content_hash)

    def percept_rows(self, limit: int = 2000) -> list[tuple[int, str]]:
        return list(self.rows)[:limit]


def make_memo(
    memo_id: int,
    *,
    rel_path: str | None = None,
    scope: str = "any",
    origin_umo: str | None = "aiocqhttp:GroupMessage:1",
    label: str = "joy",
    caption: str = "猫捂脸笑",
    text_in_img: str = "",
    scenes: tuple[str, ...] = ("我裂开了",),
    labels: tuple[str, ...] = (),
    persona_ids: tuple[str, ...] = (GENERIC_PERSONA,),
    use_count: int = 0,
    is_fav: bool = False,
    state: str = STATE_READY,
    created_at: int | None = None,
) -> Memo:
    return Memo(
        id=memo_id,
        rel_path=rel_path or f"blobs/202601/1_{memo_id:08d}.png",
        content_hash=f"{memo_id:064x}",
        percept_hash=None,
        label=label,
        caption=caption,
        text_in_img=text_in_img,
        scope=scope,
        origin_umo=origin_umo,
        state=state,
        is_fav=is_fav,
        use_count=use_count,
        created_at=created_at or int(time.time()),
        used_at=0,
        origin_kind="chat",
        scenes=scenes,
        labels=labels,
        persona_ids=persona_ids,
    )
