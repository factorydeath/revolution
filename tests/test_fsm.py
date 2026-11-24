from __future__ import annotations

from aiogram.fsm.storage.base import StorageKey

import main
from db import Database


def _key(user_id: int) -> StorageKey:
    return StorageKey(bot_id=1, chat_id=1, user_id=user_id, destiny="default")


async def test_state_and_data_roundtrip(tmp_path):
    db = Database(tmp_path / "fsm.db")
    await db.init_db()
    try:
        storage = main.SqliteStorage()
        storage.bind(db)
        key = _key(1)

        await storage.set_state(key, main.AttackState.waiting_phone)
        await storage.set_data(key, {"phones": ["79161234567"]})

        assert await storage.get_state(key) == main.AttackState.waiting_phone.state
        assert await storage.get_data(key) == {"phones": ["79161234567"]}

        await storage.set_state(key, None)
        assert await storage.get_state(key) is None
        assert await storage.get_data(key) == {"phones": ["79161234567"]}
    finally:
        await db.close()


async def test_cache_is_written_through(tmp_path):
    db = Database(tmp_path / "fsm.db")
    await db.init_db()
    try:
        first = main.SqliteStorage()
        first.bind(db)
        key = _key(7)
        await first.set_state(key, main.AttackState.waiting_duration)
        await first.set_data(key, {"phones": ["380671234567"]})

        second = main.SqliteStorage()
        second.bind(db)
        assert await second.get_state(key) == main.AttackState.waiting_duration.state
        assert await second.get_data(key) == {"phones": ["380671234567"]}
    finally:
        await db.close()


async def test_data_before_state_keeps_state(tmp_path):
    db = Database(tmp_path / "fsm.db")
    await db.init_db()
    try:
        storage = main.SqliteStorage()
        storage.bind(db)
        key = _key(9)

        await storage.set_data(key, {"action": "grant"})
        assert await storage.get_data(key) == {"action": "grant"}
        assert await storage.get_state(key) is None

        await storage.set_state(key, main.AdminState.waiting_user)
        assert await storage.get_state(key) == main.AdminState.waiting_user.state
        assert await storage.get_data(key) == {"action": "grant"}
    finally:
        await db.close()
