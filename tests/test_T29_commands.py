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


class FakeEvent:
    def __init__(self, role: str = "admin", umo: str = "aiocqhttp:GroupMessage:1") -> None:
        self.role = role
        self.unified_msg_origin = umo

    def is_admin(self) -> bool:
        return self.role == "admin"


class FakePlugin:
    def __init__(self, store: MemoStore, data_dir) -> None:
        self.store = store
        self.data_dir = data_dir
        self.errors: list[tuple] = []
        self.collect = True
        self.auto_send = True
        self.cap_override: int | None = None

    def command_name(self) -> str:
        return "memenest"

    def collect_enabled(self) -> bool:
        return self.collect


    def library_cap(self) -> int:
        return self.cap_override or 800


    def queue_depth(self) -> int:
        return 0


    def set_collect(self, value: bool) -> None:
        self.collect = value


    def collect_existing_files(self) -> set[str]:
        return set()


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
    assert "管理员" in reply
    assert plugin.collect_enabled() is True
