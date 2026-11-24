from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main


class FakeAttack:
    def __init__(self, gate: asyncio.Event) -> None:
        self.id = "engine-1"
        self.user_stopped = False
        self._gate = gate

    def stop(self, by_user: bool = False) -> None:
        self.user_stopped = True
        self._gate.set()

    async def wait(self) -> None:
        await self._gate.wait()

    def snapshot(self) -> dict:
        return {"sent": 0, "ok": 0, "err": 0, "running": False}


class FakeDB:
    def __init__(self) -> None:
        self.next_id = 1
        self.marked_started: list[int] = []
        self.undone: list[int] = []

    async def get_user_override(self, user_id: int):
        return None

    async def check_user_exists_privilege(self, user_id: int) -> bool:
        return False

    async def add_attack(self, user_id: int, phone: str, duration: int) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    async def is_phone_protected(self, phone: str, *, fresh: bool = False) -> bool:
        return False

    async def is_user_banned(self, user_id: int, *, fresh: bool = False) -> bool:
        return False

    async def mark_started(self, attack_id: int) -> None:
        self.marked_started.append(attack_id)

    async def mark_finished(self, attack_id: int) -> None:
        pass

    async def mark_stopped(self, attack_id: int) -> None:
        pass

    async def undo_attack(self, attack_id: int) -> bool:
        self.undone.append(attack_id)
        return True


class SlowManager:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.calls = 0

    async def start(self, phone, duration, workers=1, *, guard=None):
        self.calls += 1
        self.entered.set()
        if guard is not None:
            assert await guard() is True
        await self.gate.wait()
        return FakeAttack(self.gate)


async def test_same_user_cannot_launch_twice(monkeypatch):
    fake_db = FakeDB()
    manager = SlowManager()
    monkeypatch.setattr(main, "db", fake_db, raising=False)
    monkeypatch.setattr(main.config, "default_max_active", 1)
    runtime = main.AttackRuntime(manager, fake_db)

    first = asyncio.create_task(
        runtime.launch(
            user_id=42,
            phone="79161234567",
            duration=30,
            has_privilege=False,
        )
    )
    try:
        await asyncio.wait_for(manager.entered.wait(), timeout=2)

        with pytest.raises(main.AttackLimitReached):
            await asyncio.wait_for(
                runtime.launch(
                    user_id=42,
                    phone="79161234567",
                    duration=30,
                    has_privilege=False,
                ),
                timeout=2,
            )
        assert manager.calls == 1

        manager.gate.set()
        outcome = await asyncio.wait_for(first, timeout=2)
        assert outcome.attack_id == 1
        assert fake_db.marked_started == [1]
        await asyncio.wait_for(
            asyncio.gather(*list(runtime._tasks.values()), return_exceptions=True),
            timeout=2,
        )
    finally:
        manager.gate.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_stop_during_preparation_prevents_publish(monkeypatch):
    fake_db = FakeDB()
    manager = SlowManager()
    monkeypatch.setattr(main, "db", fake_db, raising=False)
    runtime = main.AttackRuntime(manager, fake_db)

    launch = asyncio.create_task(
        runtime.launch(
            user_id=42,
            phone="79161234567",
            duration=30,
            has_privilege=False,
        )
    )
    try:
        await asyncio.wait_for(manager.entered.wait(), timeout=2)
        assert await runtime.stop(42, 1) is True
        manager.gate.set()

        with pytest.raises(main.LaunchFailed):
            await asyncio.wait_for(launch, timeout=2)
        assert runtime.total_active() == 0
        assert fake_db.marked_started == []
        assert fake_db.undone == [1]
    finally:
        manager.gate.set()
        launch.cancel()
        await asyncio.gather(launch, return_exceptions=True)
