from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


class State:
    def __init__(self) -> None:
        self.data = {"phones": ["79160000001", "79160000002", "79160000003"]}
        self.update_data = AsyncMock()
        self.set_state = AsyncMock()
        self.clear = AsyncMock()

    async def get_data(self):
        return self.data


async def test_failed_numbers_stay_queued_with_leftovers(monkeypatch):
    state = State()
    message = SimpleNamespace(from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=1))
    say = AsyncMock()
    started: list[str] = []

    async def fake_start(
        user_id, phone, duration, has_privilege, max_duration, _message, **kwargs
    ):
        started.append(phone)
        return len(started) != 1, "engine"

    monkeypatch.setattr(main, "bot", object(), raising=False)
    monkeypatch.setattr(main, "say", say)
    monkeypatch.setattr(main, "_start_attack", fake_start)
    monkeypatch.setattr(
        main,
        "runtime",
        SimpleNamespace(
            cancel_epoch=lambda _user_id: 0,
            active_for=lambda _user_id: 0,
            total_active=lambda: 0,
        ),
        raising=False,
    )

    await main._launch_batch(
        message,
        state,
        ["79160000001", "79160000002", "79160000003"],
        duration=30,
        has_privilege=False,
        max_active=2,
        max_duration=300,
    )

    assert started == ["79160000001", "79160000002"]
    state.update_data.assert_awaited_once_with(
        phones=["79160000001", "79160000003"]
    )
    state.set_state.assert_awaited_once_with(main.AttackState.waiting_duration)
    state.clear.assert_not_awaited()
    rendered = "\n".join(call.args[2] for call in say.await_args_list)
    assert "Запустил 1" in rendered
    assert "В очереди ещё 2" in rendered


async def test_cancelled_queue_stops_slipped_attacks(monkeypatch):
    state = State()
    message = SimpleNamespace(from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=1))
    say = AsyncMock()
    started: list[str] = []
    stop_user = AsyncMock()
    epochs = iter([0, 0, 1])

    async def fake_start(
        user_id, phone, duration, has_privilege, max_duration, _message, **kwargs
    ):
        started.append(phone)
        return True, ""

    monkeypatch.setattr(main, "bot", object(), raising=False)
    monkeypatch.setattr(main, "say", say)
    monkeypatch.setattr(main, "_start_attack", fake_start)
    monkeypatch.setattr(
        main,
        "runtime",
        SimpleNamespace(
            cancel_epoch=lambda _user_id: next(epochs, 1),
            active_for=lambda _user_id: 0,
            total_active=lambda: 0,
            stop_user=stop_user,
        ),
        raising=False,
    )

    await main._launch_batch(
        message,
        state,
        ["79160000001", "79160000002", "79160000003"],
        duration=30,
        has_privilege=False,
        max_active=3,
        max_duration=300,
    )

    assert started == ["79160000001"]
    stop_user.assert_awaited_once_with(42)
    state.update_data.assert_awaited_once_with(
        phones=["79160000002", "79160000003"]
    )
    assert "Остановлено" in "\n".join(call.args[2] for call in say.await_args_list)
