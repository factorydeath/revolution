from __future__ import annotations

import asyncio
import datetime
import sqlite3

import pytest

from db import Database

@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.init_db()
    yield database
    await database.close()

async def test_users(db):
    assert not await db.user_exists(1)
    await db.add_user(1, "Danya", "danya")
    assert await db.user_exists(1)
    await db.add_user(1, "Danya2", "danya2")
    assert await db.get_user_by_id(1) == ("Danya2", "danya2")
    assert await db.get_total_users() == 1
    assert (await db.get_user_by_username("@danya2"))[0] == 1

async def test_terms_cache_invalidation(db):
    await db.add_user(1, "A")
    assert not await db.has_accepted_terms(1)
    await db.accept_terms(1)
    assert await db.has_accepted_terms(1)

async def test_vip(db):
    assert not await db.check_user_exists_privilege(1)
    assert await db.add_user_privilege(1) is True
    assert await db.add_user_privilege(1) is False
    assert await db.check_user_exists_privilege(1)
    assert await db.get_privileged_count() == 1
    assert await db.remove_user_privilege(1) is True
    assert not await db.check_user_exists_privilege(1)

async def test_vip_expiration(db):
    user_id = 42
    assert not await db.check_user_exists_privilege(user_id)
    assert await db.add_user_privilege(user_id, days=30) is True
    exp = await db.get_user_privilege_expiry(user_id)
    assert exp is not None
    past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat()
    await db._execute("UPDATE users_with_privileges SET expires_at = ? WHERE user_id = ?", (past, user_id))
    db._cache.drop(f"vip:{user_id}")
    assert not await db.check_user_exists_privilege(user_id)
    assert await db.get_user_privilege_expiry(user_id) is None
    assert await db.get_privileged_count() == 0
    assert await db.add_user_privilege(user_id, days=30) is True
    assert await db.check_user_exists_privilege(user_id) is True

async def test_ban(db):
    await db.add_user(1, "A")
    assert not await db.is_user_banned(1)
    await db.ban_user(1)
    assert await db.is_user_banned(1)
    assert await db.get_banned_count() == 1
    await db.unban_user(1)
    assert not await db.is_user_banned(1)

async def test_overrides(db):
    assert await db.get_user_override(5) is None
    await db.set_user_override(5, 3, 900)
    assert await db.get_user_override(5) == (3, 900)
    assert await db.remove_user_override(5) is True
    assert await db.get_user_override(5) is None
    assert await db.remove_user_override(5) is False

async def test_protection(db):
    await db.add_user(1, "A")
    assert not await db.is_phone_protected("79161234567")
    assert await db.add_protected_number("79161234567", 1) is True
    assert await db.add_protected_number("79161234567", 1) is False
    assert await db.is_phone_protected("79161234567")
    assert (await db.get_protected_by_user(1))[0][0] == "79161234567"
    assert await db.remove_protected_number("79161234567") is True
    assert not await db.is_phone_protected("79161234567")

async def test_filter_protected_batch(db):
    await db.add_user(1, "A")
    await db.add_protected_number("+7 (916) 123-45-67", 1)

    phones = ["79161234567", "79160000001", "380671234567"]
    allowed, hits = await db.filter_protected(phones)
    assert allowed == ["79160000001", "380671234567"]
    assert hits == ["79161234567"]

    assert await db.filter_protected(phones) == (allowed, hits)
    assert await db.filter_protected([]) == ([], [])

async def test_attack_history_semantics(db):
    await db.add_user(1, "A")
    first = await db.add_attack(1, "79161234567", 60)
    second = await db.add_attack(1, "79161234567", 30)
    await db.mark_finished(first)
    await db.mark_stopped(second)

    rows = await db.get_attack_history(1)
    assert [(r[0], r[1], r[3]) for r in rows] == [
        ("79161234567", 30, 1),
        ("79161234567", 60, 0),
    ]
    assert await db.get_total_attacks() == 2
    assert (await db.get_user_stats(1))[3] == 2

async def test_dangling_attacks_closed_on_restart(db):
    await db.add_user(1, "A")
    running = await db.add_attack(1, "79161234567", 60)
    await db.mark_started(running)
    finished = await db.add_attack(1, "79161234567", 60)
    await db.mark_finished(finished)
    never_started = await db.add_attack(1, "79161234567", 60)

    closed = await db.close_dangling_attacks()
    assert closed == 2

    row = await db._fetchone(
        "SELECT status FROM attacks WHERE id = ?", (never_started,)
    )
    assert row[0] == "failed"
    assert (await db.get_user_stats(1))[3] == 2
    assert await db.get_total_attacks() == 2

    assert await db.close_dangling_attacks() == 0

async def test_search_and_paging(db):
    for index in range(1, 13):
        await db.add_user(index, f"user{index}", f"u{index}")
    rows = await db.search_users("user1", limit=10)
    assert rows
    page = await db.get_all_users(page=1, per_page=5)
    assert len(page) == 5
    vip_rows, total = await db.get_vip_users(page=1, per_page=5)
    assert total == 0 and vip_rows == []

async def test_persistent_connection_survives_many_ops(db):
    for index in range(50):
        await db.add_user(index, f"n{index}")
    assert await db.get_total_users() == 50

async def test_transaction_rolls_back_whole_batch(db):
    await db.add_user(1, "A")
    with pytest.raises(sqlite3.OperationalError):
        await db._execute_batch(
            [
                ("UPDATE users SET first_name = 'B' WHERE user_id = 1", ()),
                ("INSERT INTO no_such_table (x) VALUES (1)", ()),
            ]
        )
    assert (await db.get_user_by_id(1))[0] == "A"

async def test_payment_is_idempotent(db):
    await db.add_user(1, "A")
    assert await db.complete_payment(1, "charge-1", 75, "XTR", "vip") == "granted"
    assert await db.complete_payment(1, "charge-1", 75, "XTR", "vip") == "duplicate"
    assert await db.get_privileged_count() == 1
    assert await db.complete_payment(1, "charge-2", 75, "XTR", "vip") == "already_vip"
    assert (await db.get_payment("charge-2"))[5] == "already_vip"
    await db.mark_payment_refund_pending("charge-2")
    assert await db.complete_payment(1, "charge-2", 75, "XTR", "vip") == "refund_pending"
    await db.mark_payment_refunded("charge-2")
    assert await db.complete_payment(1, "charge-2", 75, "XTR", "vip") == "duplicate"

async def test_protection_unique_per_user_is_atomic(db):
    await db.add_user(1, "A")
    results = await asyncio.gather(
        db.add_protected_number("79160000001", 1),
        db.add_protected_number("79160000002", 1),
    )
    assert results.count(True) == 1
    rows = await db.get_protected_by_user(1)
    assert len(rows) == 1
    numbers = {"79160000001", "79160000002"}
    rows = await db.get_protected_by_user(1)
    winner = rows[0][0]
    loser = (numbers - {winner}).pop()
    assert not await db.is_phone_protected(loser, fresh=True)

async def test_fresh_protection_read_bypasses_cache(db):
    await db.add_user(1, "A")
    phone = "79161234567"
    assert not await db.is_phone_protected(phone)
    await db._execute(
        "INSERT INTO protected_numbers (phone, user_id, added_at) VALUES (?, ?, ?)",
        (phone, 1, "now"),
    )
    assert not await db.is_phone_protected(phone)
    assert await db.is_phone_protected(phone, fresh=True)
