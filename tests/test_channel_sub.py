from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import main


@pytest.fixture(autouse=True)
def clean_sub_cache():
    main._sub_cache.clear()
    yield
    main._sub_cache.clear()


@pytest.mark.asyncio
async def test_check_channel_sub_subscribed():
    mock_bot = AsyncMock()
    mock_bot.get_chat_member.return_value = SimpleNamespace(status="member")

    res = await main.check_channel_subscription(mock_bot, 12345)
    assert res is True
    mock_bot.get_chat_member.assert_awaited_once_with(chat_id=config.channel_id, user_id=12345)


@pytest.mark.asyncio
async def test_check_channel_sub_not_subscribed():
    mock_bot = AsyncMock()
    mock_bot.get_chat_member.return_value = SimpleNamespace(status="left")

    res = await main.check_channel_subscription(mock_bot, 54321)
    assert res is False
    mock_bot.get_chat_member.assert_awaited_once_with(chat_id=config.channel_id, user_id=54321)


@pytest.mark.asyncio
async def test_check_channel_sub_admin_exempt(monkeypatch):
    mock_bot = AsyncMock()
    admin_id = config.admin_ids[0]

    res = await main.check_channel_subscription(mock_bot, admin_id)
    assert res is True
    mock_bot.get_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_channel_sub_disabled(monkeypatch):
    monkeypatch.setattr(config, "force_channel_sub", False)
    mock_bot = AsyncMock()

    res = await main.check_channel_subscription(mock_bot, 112233)
    assert res is True
    mock_bot.get_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_channel_sub_cache():
    mock_bot = AsyncMock()
    mock_bot.get_chat_member.return_value = SimpleNamespace(status="member")

    res1 = await main.check_channel_subscription(mock_bot, 99901)
    assert res1 is True
    assert mock_bot.get_chat_member.await_count == 1

    res2 = await main.check_channel_subscription(mock_bot, 99901)
    assert res2 is True
    assert mock_bot.get_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_cmd_start_stops_unsubscribed(monkeypatch):
    mock_bot = AsyncMock()
    mock_bot.get_chat_member.return_value = SimpleNamespace(status="left")
    monkeypatch.setattr(main, "bot", mock_bot, raising=False)

    say_mock = AsyncMock()
    monkeypatch.setattr(main, "say", say_mock)

    class MockDB:
        async def add_user(self, *args, **kwargs):
            pass

        async def is_user_banned(self, *args):
            return False

    monkeypatch.setattr(main, "db", MockDB(), raising=False)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=8888, first_name="Test", username="test"),
        chat=SimpleNamespace(id=8888),
    )
    state = AsyncMock()

    await main.cmd_start(message, state)

    say_mock.assert_awaited_once()
    assert "Обязательная подписка" in say_mock.await_args.args[2]
    assert config.channel_id in say_mock.await_args.args[2]


@pytest.mark.asyncio
async def test_check_sub_cb_flow(monkeypatch):
    mock_bot = AsyncMock()
    mock_bot.get_chat_member.return_value = SimpleNamespace(status="left")
    monkeypatch.setattr(main, "bot", mock_bot, raising=False)

    safe_answer = AsyncMock()
    monkeypatch.setattr(main, "safe_cb_answer", safe_answer)
    show_mock = AsyncMock()
    monkeypatch.setattr(main, "show", show_mock)

    class MockDB:
        async def has_accepted_terms(self, user_id):
            return True

        async def check_user_exists_privilege(self, user_id):
            return False

    monkeypatch.setattr(main, "db", MockDB(), raising=False)

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=7777, first_name="User", username="user"),
        message=SimpleNamespace(chat=SimpleNamespace(id=7777)),
    )
    state = AsyncMock()

    await main.check_sub_cb(cb, state)
    safe_answer.assert_awaited_once()
    assert "Вы ещё не подписались" in safe_answer.await_args.args[1]
    show_mock.assert_not_awaited()

    mock_bot.get_chat_member.return_value = SimpleNamespace(status="member")
    safe_answer.reset_mock()

    await main.check_sub_cb(cb, state)
    assert safe_answer.await_args.args[1] == "Подписка подтверждена!"
    show_mock.assert_awaited_once()
