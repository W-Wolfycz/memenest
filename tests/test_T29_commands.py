"""T-29 / F5.2：管理指令的权限门与磁盘副作用。

守的静默失效：管理员门失效（任何成员能删库）、批量删除只删行不删文件、
容量下限被绕过。这些错误在界面上都看不出来。
"""

from __future__ import annotations

import pytest

from memenest import commands_core as core
from memenest.commands import CommandRunner
from memenest.domain import STATE_READY, Analysis, Draft
from memenest.store import MemoStore


class FakeImage:
    type = "Image"

    def __init__(self, url: str = "https://cdn.example.com/a.png") -> None:
        self.url = url
        self.file = ""
        self.path = ""


class FakeReply:
    type = "Reply"

    def __init__(self, *sources: str) -> None:
        self.chain = [FakeImage(source) for source in sources]


class FakeEvent:
    def __init__(
        self,
        role: str = "admin",
        umo: str = "aiocqhttp:GroupMessage:1",
        components: list | None = None,
    ) -> None:
        self.role = role
        self.unified_msg_origin = umo
        self._components = components or []

    def is_admin(self) -> bool:
        return self.role == "admin"

    def get_messages(self) -> list:
        return list(self._components)


class FakePlugin:
    def __init__(self, store: MemoStore, data_dir) -> None:
        self.store = store
        self.data_dir = data_dir
        self.errors: list[tuple] = []
        self.muted: list[tuple] = []
        self.collect = True
        self.cap_override: int | None = None

    def command_name(self) -> str:
        return "memenest"

    def collect_enabled(self) -> bool:
        return self.collect

    def library_cap(self) -> int:
        return self.cap_override or 800

    def queue_depth(self) -> int:
        return 0

    def set_collect(self, value: bool, minutes: int | None = None) -> None:
        self.collect = value

    async def mute_for_persona(self, sources, umo, *, enabled: bool = True):
        self.muted.append((tuple(sources), umo, enabled))
        return {"ok": True, "code": "ok", "memo_id": 7}

    def log_error(self, message, *args) -> None:
        self.errors.append((message, args))


@pytest.fixture()
def runner(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = MemoStore(data_dir / "memo.db")
    store.initialize()
    rel = "inbox/1_aaaaaaaa.png"
    target = data_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"png")
    store.add(
        Draft(
            rel_path=rel,
            content_hash="a" * 64,
            percept_hash=None,
            analysis=Analysis(label="joy", caption="猫在笑"),
            scope="any",
            origin_umo=None,
            origin_kind="chat",
            created_at=1,
            state=STATE_READY,
        )
    )
    plugin = FakePlugin(store, data_dir)
    yield CommandRunner(plugin), plugin, store, target
    store.close()


@pytest.mark.asyncio
async def test_member_cannot_toggle_collect(runner):
    """管理员门失效 = 任何群成员都能悄悄关掉收集。"""
    commands, plugin, _store, _target = runner
    parsed = core.parse_command("/memenest off", "memenest")
    assert parsed is not None
    reply = await commands.run(FakeEvent(role="member"), parsed)
    assert reply.strip() == "这条指令需要管理员权限。"
    assert plugin.collect_enabled() is True


@pytest.mark.asyncio
async def test_mute_targets_the_persona_of_this_session(runner):
    """指令落的是「这张图 + 当前会话人格」：把 umo 传错就会禁到别的人格上。"""
    commands, plugin, _store, _target = runner
    parsed = core.parse_command("/memenest mute", "memenest")
    assert parsed is not None
    event = FakeEvent(umo="aiocqhttp:GroupMessage:2", components=[FakeImage()])
    reply = await commands.run(event, parsed)
    assert "#7" in reply
    assert plugin.muted == [
        (("https://cdn.example.com/a.png",), "aiocqhttp:GroupMessage:2", True)
    ]


