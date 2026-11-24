from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Chat, ChatInviteLink, ChatMemberMember, ChatMemberLeft, User

import config
import db as db_module
import main


@pytest.fixture
async def test_db(tmp_path):
    database = db_module.Database(tmp_path / "test_vip.db")
    await database.init_db()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_db_vip_invite_links(test_db):
    user_id = 12345
    link1 = "https://t.me/+invite111"
    link2 = "https://t.me/+invite222"
    chat_id = -1004422775130

    active = await test_db.get_active_vip_invites_for_user(user_id)
    assert active == []
    assert await test_db.has_user_joined_vip_community(user_id) is False

    await test_db.save_vip_invite_link(user_id, link1, chat_id)
    active = await test_db.get_active_vip_invites_for_user(user_id)
    assert active == [link1]

    info = await test_db.get_vip_invite_info(link1)
    assert info is not None
    assert info["user_id"] == user_id
    assert info["invite_link"] == link1
    assert info["chat_id"] == chat_id
    assert info["status"] == "active"

    assert await test_db.mark_vip_invite_link_revoked(link1) is True
    active = await test_db.get_active_vip_invites_for_user(user_id)
    assert active == []

    await test_db.save_vip_invite_link(user_id, link2, chat_id)
    assert await test_db.has_user_joined_vip_community(user_id) is False
    assert await test_db.mark_vip_invite_link_used(link2, user_id=user_id) is True
    assert await test_db.has_user_joined_vip_community(user_id) is True

    active = await test_db.get_active_vip_invites_for_user(user_id)
    assert active == []


@pytest.mark.asyncio
async def test_issue_vip_community_link(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    user_id = 998877

    mock_bot = AsyncMock()
    mock_invite = SimpleNamespace(invite_link="https://t.me/+secretVIPlink")
    mock_bot.create_chat_invite_link.return_value = mock_invite

    link = await main.issue_vip_community_link(mock_bot, user_id)
    assert link == "https://t.me/+secretVIPlink"
    mock_bot.create_chat_invite_link.assert_awaited_once_with(
        chat_id=config.vip_community_chat_id,
        name=f"VIP_{user_id}",
        member_limit=1,
    )

    active = await test_db.get_active_vip_invites_for_user(user_id)
    assert active == ["https://t.me/+secretVIPlink"]

    mock_invite2 = SimpleNamespace(invite_link="https://t.me/+newVIPlink")
    mock_bot.create_chat_invite_link.return_value = mock_invite2

    link2 = await main.issue_vip_community_link(mock_bot, user_id)
    assert link2 == "https://t.me/+newVIPlink"
    mock_bot.revoke_chat_invite_link.assert_awaited_once_with(
        chat_id=config.vip_community_chat_id,
        invite_link="https://t.me/+secretVIPlink",
    )
    active2 = await test_db.get_active_vip_invites_for_user(user_id)
    assert active2 == ["https://t.me/+newVIPlink"]


@pytest.mark.asyncio
async def test_vip_chat_member_joined_revocation(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    user_id = 555000
    invite_url = "https://t.me/+oneTimeVIP"
    chat_id = config.vip_community_chat_id

    await test_db.save_vip_invite_link(user_id, invite_url, chat_id)

    mock_bot = AsyncMock()
    monkeypatch.setattr(main, "bot", mock_bot, raising=False)

    event = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, username="vipuser"),
        old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=SimpleNamespace(
            status="member",
            user=SimpleNamespace(id=user_id, username="vipuser"),
        ),
        invite_link=SimpleNamespace(invite_link=invite_url),
    )

    await main.vip_chat_member_handler(event, bot=mock_bot)

    mock_bot.revoke_chat_invite_link.assert_awaited_once_with(
        chat_id=chat_id,
        invite_link=invite_url,
    )
    assert await test_db.has_user_joined_vip_community(user_id) is True
    active = await test_db.get_active_vip_invites_for_user(user_id)
    assert active == []


@pytest.mark.asyncio
async def test_vip_chat_member_joined_without_invite_link_object(test_db, monkeypatch):
    monkeypatch.setattr(main, "db", test_db, raising=False)
    user_id = 777111
    invite_url = "https://t.me/+anotherOneTimeVIP"
    chat_id = config.vip_community_chat_id

    await test_db.save_vip_invite_link(user_id, invite_url, chat_id)

    mock_bot = AsyncMock()
    monkeypatch.setattr(main, "bot", mock_bot, raising=False)

    event = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, username="vipuser2"),
        old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=SimpleNamespace(
            status="member",
            user=SimpleNamespace(id=user_id, username="vipuser2"),
        ),
        invite_link=None,
    )

    await main.vip_chat_member_handler(event, bot=mock_bot)

    mock_bot.revoke_chat_invite_link.assert_awaited_once_with(
        chat_id=chat_id,
        invite_link=invite_url,
    )
    assert await test_db.has_user_joined_vip_community(user_id) is True


@pytest.mark.asyncio
async def test_successful_payment_delivers_vip_link(monkeypatch):
    mock_db = SimpleNamespace(
        complete_payment=AsyncMock(return_value="granted"),
        save_vip_invite_link=AsyncMock(),
        get_active_vip_invites_for_user=AsyncMock(return_value=[]),
    )
    mock_bot = AsyncMock()
    mock_bot.create_chat_invite_link.return_value = SimpleNamespace(
        invite_link="https://t.me/+vipStarsLink"
    )
    say_mock = AsyncMock()

    monkeypatch.setattr(main, "db", mock_db, raising=False)
    monkeypatch.setattr(main, "bot", mock_bot, raising=False)
    monkeypatch.setattr(main, "say", say_mock)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(id=123),
        successful_payment=SimpleNamespace(
            invoice_payload=main.vip_payload,
            telegram_payment_charge_id="charge-stars-1",
            currency="XTR",
            total_amount=main.config.vip_price,
        ),
    )

    await main.successful_payment(message)

    say_mock.assert_awaited_once()
    sent_text = say_mock.await_args.args[2]
    sent_kb = say_mock.await_args.args[3]

    assert "https://t.me/+vipStarsLink" in sent_text
    buttons = [btn.text for row in sent_kb.inline_keyboard for btn in row]
    assert "Приватный VIP-форум" in buttons


@pytest.mark.asyncio
async def test_access_middleware_allows_forum_chat_member(monkeypatch):
    middleware = main.AccessMiddleware()
    handler = AsyncMock(return_value="ok")

    event = SimpleNamespace(
        chat=SimpleNamespace(id=config.vip_community_chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=999),
        old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=999)),
    )
    update = SimpleNamespace(
        chat_member=event,
        my_chat_member=None,
        message=None,
        edited_message=None,
        callback_query=None,
        pre_checkout_query=None,
    )

    db_mock = SimpleNamespace(is_user_banned=AsyncMock(return_value=False))
    monkeypatch.setattr(main, "db", db_mock, raising=False)

    res = await middleware(handler, update, {"event_from_user": event.from_user})
    assert res == "ok"
    handler.assert_awaited_once()
