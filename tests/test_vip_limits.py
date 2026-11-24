from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from db import Database, get_today_date
import main

class DummyAttack:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.id = "dummy-1"
        self._gate = gate or asyncio.Event()

    def stop(self, by_user: bool = False) -> None:
        self._gate.set()

    async def wait(self) -> None:
        await self._gate.wait()

    def snapshot(self) -> dict:
        return {"sent": 0, "ok": 0, "err": 0, "running": False}

class DummyManager:
    def __init__(self) -> None:
        self.calls = 0

    async def start(self, phone, duration, workers=1, *, guard=None):
        self.calls += 1
        if guard is not None:
            assert await guard() is True
        gate = asyncio.Event()
        gate.set()
        return DummyAttack(gate)

class BlockingManager:
    def __init__(self) -> None:
        self.gates: list[asyncio.Event] = []

    async def start(self, phone, duration, workers=1, *, guard=None):
        gate = asyncio.Event()
        self.gates.append(gate)
        return DummyAttack(gate)

@pytest.fixture
async def test_db(tmp_path):
    database = Database(tmp_path / "limits_test.db")
    await database.init_db()
    yield database
    await database.close()

def test_config_limits():
    assert config.default_max_duration == 40
    assert config.default_max_active == 1
    assert config.default_daily_limit == 5

    assert config.vip_max_duration == 7200
    assert config.vip_max_active == 10
    assert config.vip_daily_limit == 200

@pytest.mark.asyncio
async def test_resolve_limits_for_non_vip_and_vip(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    user_id = 1001
    await test_db.add_user(user_id, "TestUser")

    max_active, max_duration = await main.resolve_limits(user_id)
    daily_limit = await main.resolve_daily_limit(user_id)
    assert max_duration == 40
    assert max_active == 1
    assert daily_limit == 5

    await test_db.add_user_privilege(user_id)
    max_active, max_duration = await main.resolve_limits(user_id)
    daily_limit = await main.resolve_daily_limit(user_id)
    assert max_duration == 7200
    assert max_active == 10
    assert daily_limit == 200

    await test_db.remove_user_privilege(user_id)
    max_active, max_duration = await main.resolve_limits(user_id)
    daily_limit = await main.resolve_daily_limit(user_id)
    assert max_duration == 40
    assert max_active == 1
    assert daily_limit == 5

@pytest.mark.asyncio
async def test_server_side_duration_limit_enforcement(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    manager = DummyManager()
    runtime = main.AttackRuntime(manager, test_db)
    user_id = 2001
    await test_db.add_user(user_id, "UserDuration")

    outcome = await runtime.launch(
        user_id=user_id,
        phone="79161111111",
        duration=40,
        has_privilege=False,
    )
    assert outcome.attack_id > 0

    with pytest.raises(main.DurationLimitReached) as exc_info:
        await runtime.launch(
            user_id=user_id,
            phone="79161111111",
            duration=41,
            has_privilege=False,
        )
    assert "превышает лимит 40" in str(exc_info.value)

    await test_db.add_user_privilege(user_id)
    outcome_vip = await runtime.launch(
        user_id=user_id,
        phone="79161111111",
        duration=7200,
        has_privilege=True,
    )
    assert outcome_vip.attack_id > 0

    with pytest.raises(main.DurationLimitReached) as exc_info_vip:
        await runtime.launch(
            user_id=user_id,
            phone="79161111111",
            duration=7201,
            has_privilege=True,
        )
    assert "превышает лимит 7200" in str(exc_info_vip.value)

@pytest.mark.asyncio
async def test_concurrent_sessions_limit_non_vip(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    manager = BlockingManager()
    runtime = main.AttackRuntime(manager, test_db)
    user_id = 3001
    await test_db.add_user(user_id, "UserConcurrent")

    await runtime.launch(
        user_id=user_id,
        phone="79160000001",
        duration=30,
        has_privilege=False,
    )
    assert runtime.active_for(user_id) == 1

    with pytest.raises(main.AttackLimitReached) as exc_info:
        await runtime.launch(
            user_id=user_id,
            phone="79160000002",
            duration=30,
            has_privilege=False,
        )
    assert "лимит 1" in str(exc_info.value)

    attack_ids = list(runtime._slots.keys())
    assert len(attack_ids) == 1
    await runtime.stop(user_id, attack_ids[0])
    assert runtime.active_for(user_id) == 0

    await runtime.launch(
        user_id=user_id,
        phone="79160000002",
        duration=30,
        has_privilege=False,
    )
    assert runtime.active_for(user_id) == 1

@pytest.mark.asyncio
async def test_concurrent_sessions_limit_vip(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    manager = BlockingManager()
    runtime = main.AttackRuntime(manager, test_db)
    user_id = 4001
    await test_db.add_user(user_id, "VipConcurrent")
    await test_db.add_user_privilege(user_id)

    for i in range(10):
        await runtime.launch(
            user_id=user_id,
            phone=f"791600000{i:02d}",
            duration=30,
            has_privilege=True,
        )
    assert runtime.active_for(user_id) == 10

    with pytest.raises(main.AttackLimitReached) as exc_info:
        await runtime.launch(
            user_id=user_id,
            phone="79160000099",
            duration=30,
            has_privilege=True,
        )
    assert "лимит 10" in str(exc_info.value)

@pytest.mark.asyncio
async def test_daily_launch_limit_non_vip(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    manager = DummyManager()
    runtime = main.AttackRuntime(manager, test_db)
    user_id = 5001
    await test_db.add_user(user_id, "UserDaily")

    for i in range(5):
        assert await test_db.get_daily_attacks(user_id) == i
        outcome = await runtime.launch(
            user_id=user_id,
            phone=f"791600000{i:02d}",
            duration=30,
            has_privilege=False,
        )
        await runtime.stop(user_id, outcome.attack_id)

    assert await test_db.get_daily_attacks(user_id) == 5

    with pytest.raises(main.DailyLimitReached) as exc_info:
        await runtime.launch(
            user_id=user_id,
            phone="79160000099",
            duration=30,
            has_privilege=False,
        )
    assert "суточный лимит 5" in str(exc_info.value)

@pytest.mark.asyncio
async def test_daily_launch_limit_vip(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    manager = DummyManager()
    runtime = main.AttackRuntime(manager, test_db)
    user_id = 6001
    await test_db.add_user(user_id, "VipDaily")
    await test_db.add_user_privilege(user_id)

    today = get_today_date()
    await test_db._execute(
        "INSERT INTO daily_launches (user_id, launch_date, count) VALUES (?, ?, 199)",
        (user_id, today),
    )
    test_db.reset_daily_cache()
    assert await test_db.get_daily_attacks(user_id) == 199

    outcome = await runtime.launch(
        user_id=user_id,
        phone="79160000200",
        duration=30,
        has_privilege=True,
    )
    await runtime.stop(user_id, outcome.attack_id)
    assert await test_db.get_daily_attacks(user_id) == 200

    with pytest.raises(main.DailyLimitReached) as exc_info:
        await runtime.launch(
            user_id=user_id,
            phone="79160000201",
            duration=30,
            has_privilege=True,
        )
    assert "суточный лимит 200" in str(exc_info.value)

@pytest.mark.asyncio
async def test_automatic_daily_reset_on_new_day(test_db, monkeypatch):
    user_id = 7001
    await test_db.add_user(user_id, "UserReset")

    day1 = "2026-11-15"
    day2 = "2026-11-16"

    for _ in range(5):
        await test_db.increment_daily_attacks(user_id, date=day1)
    assert await test_db.get_daily_attacks(user_id, date=day1) == 5

    assert await test_db.get_daily_attacks(user_id, date=day2) == 0

    await test_db.increment_daily_attacks(user_id, date=day2)
    assert await test_db.get_daily_attacks(user_id, date=day2) == 1
    assert await test_db.get_daily_attacks(user_id, date=day1) == 5

@pytest.mark.asyncio
async def test_reset_daily_attacks_method(test_db):
    user_id = 8001
    await test_db.add_user(user_id, "UserManualReset")
    today = get_today_date()

    await test_db.increment_daily_attacks(user_id)
    await test_db.increment_daily_attacks(user_id)
    assert await test_db.get_daily_attacks(user_id) == 2

    await test_db.reset_daily_attacks(user_id, today)
    assert await test_db.get_daily_attacks(user_id) == 0

@pytest.mark.asyncio
async def test_failed_attack_rolls_back_daily_counter(test_db):
    user_id = 9001
    await test_db.add_user(user_id, "RollbackUser")

    assert await test_db.get_daily_attacks(user_id) == 0
    attack_id = await test_db.add_attack(user_id, "79160000001", 30)
    assert await test_db.get_daily_attacks(user_id) == 1

    undone = await test_db.undo_attack(attack_id)
    assert undone is True
    assert await test_db.get_daily_attacks(user_id) == 0

@pytest.mark.asyncio
async def test_user_messages_on_limit_exhausted():
    no_slots_non_vip = main._no_slots_text(has_privilege=False, max_active=1)
    assert "1" in no_slots_non_vip
    assert "VIP" in no_slots_non_vip

    daily_non_vip = main._daily_limit_text(has_privilege=False, daily_limit=5)
    assert "Суточный лимит исчерпан" in daily_non_vip
    assert "5" in daily_non_vip
    assert "00:00 МСК" in daily_non_vip

    daily_vip = main._daily_limit_text(has_privilege=True, daily_limit=200)
    assert "Суточный лимит исчерпан" in daily_vip
    assert "200" in daily_vip
    assert "00:00 МСК" in daily_vip

@pytest.mark.asyncio
async def test_client_spoofed_privilege_rejected_by_server(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    manager = DummyManager()
    runtime = main.AttackRuntime(manager, test_db)
    user_id = 11001
    await test_db.add_user(user_id, "AttackerUser")

    with pytest.raises(main.DurationLimitReached):
        await runtime.launch(
            user_id=user_id,
            phone="79161112233",
            duration=50,
            has_privilege=True,
        )

    for i in range(5):
        outcome = await runtime.launch(
            user_id=user_id,
            phone=f"791611122{i:02d}",
            duration=30,
            has_privilege=True,
        )
        await runtime.stop(user_id, outcome.attack_id)

    with pytest.raises(main.DailyLimitReached):
        await runtime.launch(
            user_id=user_id,
            phone="79161112299",
            duration=30,
            has_privilege=True,
        )

@pytest.mark.asyncio
async def test_launch_batch_respects_daily_limit(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    user_id = 12001
    await test_db.add_user(user_id, "BatchUser")
    for _ in range(4):
        await test_db.increment_daily_attacks(user_id)
    assert await test_db.get_daily_attacks(user_id) == 4

    say_mock = AsyncMock()
    monkeypatch.setattr(main, "say", say_mock)
    monkeypatch.setattr(main, "bot", object(), raising=False)

    started_phones: list[str] = []

    async def fake_start(user_id, phone, duration, has_privilege, max_duration, message, **kwargs):
        started_phones.append(phone)
        await test_db.increment_daily_attacks(user_id)
        return True, ""

    monkeypatch.setattr(main, "_start_attack", fake_start)
    monkeypatch.setattr(
        main,
        "runtime",
        SimpleNamespace(
            cancel_epoch=lambda _u: 0,
            active_for=lambda _u: 0,
            total_active=lambda: 0,
        ),
        raising=False,
    )

    class MockState:
        def __init__(self):
            self.cleared = False
            self.data = {}
        async def clear(self):
            self.cleared = True
        async def update_data(self, **kwargs):
            self.data.update(kwargs)
        async def set_state(self, state):
            pass

    state = MockState()
    message = SimpleNamespace(from_user=SimpleNamespace(id=user_id), chat=SimpleNamespace(id=100))

    phones = ["79160000001", "79160000002", "79160000003"]
    await main._launch_batch(
        message=message,
        state=state,
        phones=phones,
        duration=30,
        has_privilege=False,
        max_active=10,
        max_duration=40,
    )

    assert len(started_phones) == 1
    assert started_phones == ["79160000001"]
    assert state.cleared is True
    rendered = "\n".join(call.args[2] for call in say_mock.await_args_list)
    assert "Суточный лимит исчерпан" in rendered

@pytest.mark.asyncio
async def test_open_order_blocks_when_daily_limit_reached(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    user_id = 13001
    await test_db.add_user(user_id, "OrderBlockUser")
    for _ in range(5):
        await test_db.increment_daily_attacks(user_id)

    show_mock = AsyncMock()
    monkeypatch.setattr(main, "show", show_mock)
    monkeypatch.setattr(main, "safe_cb_answer", AsyncMock())

    class MockState:
        def __init__(self):
            self.state = None
        async def set_state(self, st):
            self.state = st
        async def clear(self):
            pass

    state = MockState()
    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=100)),
    )

    await main.open_order(cb, state)
    assert state.state is None
    show_mock.assert_awaited_once()
    shown_text = show_mock.await_args.args[1]
    assert "Суточный лимит исчерпан" in shown_text
    assert "5" in shown_text

@pytest.mark.asyncio
async def test_async_rate_limiter_pacing():
    from attack import AsyncRateLimiter
    import time

    limiter = AsyncRateLimiter(20.0)
    start_time = time.monotonic()
    for _ in range(5):
        ok = await limiter.acquire()
        assert ok is True
    elapsed = time.monotonic() - start_time
    assert elapsed >= 0.18, f"Expected elapsed >= 0.18s, got {elapsed}s"

@pytest.mark.asyncio
async def test_async_rate_limiter_stop_and_deadline():
    from attack import AsyncRateLimiter
    import time

    limiter = AsyncRateLimiter(10.0)
    stop_event = asyncio.Event()
    assert await limiter.acquire(stop_event=stop_event) is True

    stop_event.set()
    assert await limiter.acquire(stop_event=stop_event) is False

    limiter2 = AsyncRateLimiter(5.0)
    assert await limiter2.acquire() is True
    now = time.monotonic()
    assert await limiter2.acquire(deadline=now + 0.05) is False

@pytest.mark.asyncio
async def test_admin_grant_vip_notifies_user(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    user_id = 998877
    await test_db.add_user(user_id, "VipNotifyUser")

    mock_bot = AsyncMock()
    monkeypatch.setattr(main, "bot", mock_bot, raising=False)
    say_mock = AsyncMock()
    monkeypatch.setattr(main, "say", say_mock)

    res = await main._grant_vip(user_id)
    assert "VIP выдан" in res
    assert await test_db.check_user_exists_privilege(user_id) is True
    say_mock.assert_awaited_once()
    args = say_mock.await_args.args
    assert args[1] == user_id
    assert "Вам выдан VIP-статус" in args[2]

    say_mock.reset_mock()
    res2 = await main._grant_vip(user_id)
    assert "VIP уже был" in res2
    say_mock.assert_not_awaited()

@pytest.mark.asyncio
async def test_attack_actual_rate_limits(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    user_id = 445566
    await test_db.add_user(user_id, "RateLimitTestUser")

    recorded_rates = []

    class RateRecordingManager:
        async def start(
            self,
            phone,
            duration,
            workers=1,
            rate_limit=None,
            guard=None,
            emulation=False,
            emul_workers=None,
        ):
            recorded_rates.append((rate_limit, emulation))
            gate = asyncio.Event()
            gate.set()
            return DummyAttack(gate)

    manager = RateRecordingManager()
    runtime = main.AttackRuntime(manager, test_db)

    await runtime.launch(user_id=user_id, phone="79162223344", duration=20, has_privilege=False)
    assert len(recorded_rates) == 1
    assert recorded_rates[0][0] == 5.0
    assert recorded_rates[0][1] is False

    await test_db.add_user_privilege(user_id)
    await runtime.launch(user_id=user_id, phone="79162223344", duration=20, has_privilege=True)
    assert len(recorded_rates) == 2
    assert recorded_rates[1][0] == 10.0
    assert recorded_rates[1][1] is True


@pytest.mark.asyncio
async def test_get_duration_reads_privilege_once(monkeypatch):
    calls = 0

    async def check_privilege(_user_id: int) -> bool:
        nonlocal calls
        calls += 1
        return True

    fake_db = SimpleNamespace(
        check_user_exists_privilege=check_privilege,
        get_daily_attacks=AsyncMock(return_value=0),
        get_user_override=AsyncMock(return_value=None),
        is_user_banned=AsyncMock(return_value=False),
    )
    launch = AsyncMock()
    monkeypatch.setattr(main, "db", fake_db, raising=False)
    monkeypatch.setattr(main, "ensure_subscribed", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "resolve_limits", AsyncMock(return_value=(10, 7200)))
    monkeypatch.setattr(
        main, "runtime", SimpleNamespace(total_active=lambda: 0), raising=False
    )
    monkeypatch.setattr(main, "_launch_batch", launch)

    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"phones": ["79161234567"]}),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=42),
        text="30",
    )

    await main.get_duration(message, state)

    assert calls == 1
    launch.assert_awaited_once()
