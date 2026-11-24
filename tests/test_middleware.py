from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


class BanDB:
    def __init__(self, banned: bool) -> None:
        self.banned = banned

    async def is_user_banned(self, user_id: int) -> bool:
        return self.banned


async def test_banned_user_is_stopped_centrally(monkeypatch):
    monkeypatch.setattr(main, "db", BanDB(True), raising=False)
    handler = AsyncMock(return_value="handled")
    event = SimpleNamespace(text="79161234567", from_user=SimpleNamespace(id=999))
    result = await main.AccessMiddleware()(handler, event, {})
    assert result is None
    handler.assert_not_awaited()


async def test_successful_payment_reaches_handler_for_banned(monkeypatch):
    class PaymentMessage(SimpleNamespace):
        pass

    monkeypatch.setattr(main, "db", BanDB(True), raising=False)
    monkeypatch.setattr(main, "Message", PaymentMessage, raising=False)
    handler = AsyncMock(return_value="handled")
    event = PaymentMessage(
        successful_payment=object(),
        text="",
        from_user=SimpleNamespace(id=999),
    )
    result = await main.AccessMiddleware()(handler, event, {})
    assert result == "handled"
    handler.assert_awaited_once()


async def test_start_still_reaches_handler_for_banned(monkeypatch):
    monkeypatch.setattr(main, "db", BanDB(True), raising=False)
    handler = AsyncMock(return_value="handled")
    event = SimpleNamespace(text="/start", from_user=SimpleNamespace(id=999))
    result = await main.AccessMiddleware()(handler, event, {})
    assert result == "handled"
    handler.assert_awaited_once()


async def test_unbanned_user_passes(monkeypatch):
    monkeypatch.setattr(main, "db", BanDB(False), raising=False)
    handler = AsyncMock(return_value="handled")
    event = SimpleNamespace(text="hello", from_user=SimpleNamespace(id=999))
    result = await main.AccessMiddleware()(handler, event, {})
    assert result == "handled"
    handler.assert_awaited_once()


async def test_user_lock_registry_prunes_stale_and_keeps_busy(monkeypatch):
    monkeypatch.setattr(main, "_user_locks", {}, raising=False)
    busy_lock = await main._user_lock(1)
    stale_lock = await main._user_lock(2)
    monkeypatch.setattr(main.time, "monotonic", lambda: 10_000.0, raising=False)
    main._user_locks[1].touched = 0.0
    main._user_locks[2].touched = 0.0
    async with busy_lock:
        assert await main._user_lock(3) is not None
        main._prune_user_locks(10_000.0)
        assert 1 in main._user_locks
        assert 2 not in main._user_locks
    assert stale_lock is not None
