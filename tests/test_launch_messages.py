from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


async def test_started_card_is_sent_as_rich_message(monkeypatch):
    message = SimpleNamespace(chat=SimpleNamespace(id=123), from_user=SimpleNamespace(id=42))
    ack = SimpleNamespace(chat=SimpleNamespace(id=123), message_id=50)
    launch = AsyncMock(return_value=main.LaunchOutcome(attack_id=7, phone="79161234567"))
    monkeypatch.setattr(main, "runtime", SimpleNamespace(launch=launch), raising=False)
    send = AsyncMock(return_value=ack)
    monkeypatch.setattr(main, "say", send)
    show = AsyncMock(return_value=ack)
    monkeypatch.setattr(main, "show", show)
    monkeypatch.setattr(main, "bot", object(), raising=False)
    monkeypatch.setattr(
        main,
        "safe_answer",
        AsyncMock(side_effect=AssertionError("стартовая карточка отправлена обычным текстом")),
    )

    ok, reason = await main._start_attack(
        user_id=42,
        phone="79161234567",
        duration=30,
        has_privilege=False,
        max_duration=300,
        message=message,
    )

    assert (ok, reason) == (True, "")
    send.assert_awaited_once()
    assert "Принял" in send.await_args.args[2]
    show.assert_awaited_once()
    args = show.await_args.args
    assert args[0] is ack
    assert "Поехали" in args[1]
    keyboard = args[2]
    assert keyboard.inline_keyboard[0][0].callback_data == "stop:7"


def test_attack_finished_text_hides_statistics():
    text = main.attack_finished_text(
        phone="+7 (916) 123-45-67",
        duration=60,
    )
    assert "+7 (916) 123-45-67" in text
    assert "Время работы" in text
    assert "Всего запросов" not in text
    assert "Принято сервисами" not in text
    assert "Без подтверждения" not in text
    assert "Ошибок сервисов" not in text


async def test_watch_updates_card_on_natural_finish(monkeypatch):
    mock_db = SimpleNamespace(
        mark_finished=AsyncMock(),
        mark_stopped=AsyncMock(),
    )
    mock_manager = SimpleNamespace()
    runtime = main.AttackRuntime(mock_manager, mock_db)

    attack = SimpleNamespace(
        wait=AsyncMock(),
        user_stopped=False,
        snapshot=lambda: {
            "sent": 50,
            "ok": 40,
            "err": 5,
            "unconfirmed": 5,
            "running": False,
            "failures": {},
            "statuses": {200: 40},
            "by_service": {"ServiceA": {200: 40}},
        },
    )

    card = SimpleNamespace(chat=SimpleNamespace(id=123), message_id=99)
    show = AsyncMock(return_value=card)
    say = AsyncMock()
    monkeypatch.setattr(main, "show", show)
    monkeypatch.setattr(main, "say", say)
    monkeypatch.setattr(main, "bot", object(), raising=False)

    await runtime._watch(
        user_id=42,
        attack_id=10,
        attack=attack,
        phone="79161234567",
        duration=30,
        card_message=card,
        chat_id=123,
    )

    mock_db.mark_finished.assert_awaited_once_with(10)
    show.assert_awaited_once()
    target_card, text, kb = show.await_args.args
    assert target_card is card
    assert "Сессия завершена" in text
    assert main.display_phone("79161234567") in text
    say.assert_not_awaited()


async def test_watch_falls_back_to_say_if_card_missing(monkeypatch):
    mock_db = SimpleNamespace(
        mark_finished=AsyncMock(),
        mark_stopped=AsyncMock(),
    )
    mock_manager = SimpleNamespace()
    runtime = main.AttackRuntime(mock_manager, mock_db)

    attack = SimpleNamespace(
        wait=AsyncMock(),
        user_stopped=False,
        snapshot=lambda: {"sent": 10, "ok": 10, "err": 0},
    )

    say = AsyncMock()
    monkeypatch.setattr(main, "say", say)
    monkeypatch.setattr(main, "bot", object(), raising=False)

    await runtime._watch(
        user_id=42,
        attack_id=11,
        attack=attack,
        phone="79161234567",
        duration=30,
        card_message=None,
        chat_id=123,
    )

    mock_db.mark_finished.assert_awaited_once_with(11)
    say.assert_awaited_once()
    assert "Сессия завершена" in say.await_args.args[2]
