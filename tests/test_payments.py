from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=1),
        successful_payment=SimpleNamespace(
            invoice_payload=main.vip_payload,
            telegram_payment_charge_id="charge-1",
            currency="XTR",
            total_amount=main.config.vip_price,
        ),
    )


async def test_refund_failure_is_retried_on_next_update(monkeypatch):
    db = SimpleNamespace(
        complete_payment=AsyncMock(return_value="refund_pending"),
        mark_payment_refund_pending=AsyncMock(),
        mark_payment_refunded=AsyncMock(),
    )
    refund = AsyncMock(return_value=True)
    say = AsyncMock()
    monkeypatch.setattr(main, "db", db, raising=False)
    monkeypatch.setattr(main, "bot", object(), raising=False)
    monkeypatch.setattr(main, "_refund_stars", refund)
    monkeypatch.setattr(main, "say", say)

    await main.successful_payment(_message())

    refund.assert_awaited_once_with(42, "charge-1")
    db.mark_payment_refund_pending.assert_awaited_once_with("charge-1")
    db.mark_payment_refunded.assert_awaited_once_with("charge-1")


async def test_duplicate_payment_is_not_refunded(monkeypatch):
    db = SimpleNamespace(
        complete_payment=AsyncMock(return_value="duplicate"),
        mark_payment_refund_pending=AsyncMock(),
        mark_payment_refunded=AsyncMock(),
    )
    refund = AsyncMock()
    monkeypatch.setattr(main, "db", db, raising=False)
    monkeypatch.setattr(main, "bot", object(), raising=False)
    monkeypatch.setattr(main, "_refund_stars", refund)
    monkeypatch.setattr(main, "say", AsyncMock())

    await main.successful_payment(_message())

    refund.assert_not_awaited()
    db.mark_payment_refund_pending.assert_not_awaited()


async def test_pay_stars_answers_callback_once_when_vip(monkeypatch):
    db = SimpleNamespace(check_user_exists_privilege=AsyncMock(return_value=True))
    answer = AsyncMock()
    say = AsyncMock()
    monkeypatch.setattr(main, "db", db, raising=False)
    monkeypatch.setattr(main, "safe_cb_answer", answer)
    monkeypatch.setattr(main, "say", say)

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=7),
        message=SimpleNamespace(chat=SimpleNamespace(id=7)),
        data="pay_stars",
    )
    state = SimpleNamespace(clear=AsyncMock())

    await main.pay_stars(cb, state)

    answer.assert_awaited_once()
    assert answer.await_args.kwargs.get("show_alert") is True
    say.assert_not_awaited()


async def test_xrocket_create_invoice(monkeypatch):
    monkeypatch.setattr(main.config, "xrocket_enabled", True)
    mock_client = SimpleNamespace(
        create_invoice=AsyncMock(
            return_value={
                "id": "777",
                "priceAmount": "1.5",
                "priceCurrency": "USDT",
                "status": "active",
                "pay_url": "https://t.me/xrocket?start=inv_777",
            }
        )
    )
    mock_db = SimpleNamespace(
        check_user_exists_privilege=AsyncMock(return_value=False),
        save_xrocket_invoice=AsyncMock(),
    )
    mock_show = AsyncMock()
    mock_cb_answer = AsyncMock()

    monkeypatch.setattr(main, "xrocket_client", mock_client)
    monkeypatch.setattr(main, "db", mock_db, raising=False)
    monkeypatch.setattr(main, "show", mock_show)
    monkeypatch.setattr(main, "safe_cb_answer", mock_cb_answer)

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=100),
        data="xrocket_create:USDT",
        message=SimpleNamespace(chat=SimpleNamespace(id=100)),
    )
    await main.xrocket_create(cb)

    mock_client.create_invoice.assert_awaited_once_with(
        amount=main.config.xrocket_vip_price_usdt,
        currency="USDT",
        description="VIP доступ Revolution",
        user_id=100,
    )
    mock_db.save_xrocket_invoice.assert_awaited_once_with(
        "777", 100, str(main.config.xrocket_vip_price_usdt), "USDT", "https://t.me/xrocket?start=inv_777"
    )
    mock_show.assert_awaited_once()


async def test_xrocket_check_invoice_paid(monkeypatch):
    mock_client = SimpleNamespace(
        get_invoice=AsyncMock(
            return_value={
                "id": "888",
                "priceAmount": "3.5",
                "priceCurrency": "USDT",
                "status": "paid",
            }
        )
    )
    mock_db = SimpleNamespace(
        get_xrocket_invoice=AsyncMock(
            return_value=("888", 200, "3.5", "USDT", "active", "https://t.me/xrocket?start=inv_888")
        ),
        check_user_exists_privilege=AsyncMock(return_value=False),
        complete_payment=AsyncMock(return_value="granted"),
        update_xrocket_invoice_status=AsyncMock(),
    )
    mock_show = AsyncMock()
    mock_cb_answer = AsyncMock()

    monkeypatch.setattr(main, "xrocket_client", mock_client)
    monkeypatch.setattr(main, "db", mock_db, raising=False)
    monkeypatch.setattr(main, "show", mock_show)
    monkeypatch.setattr(main, "safe_cb_answer", mock_cb_answer)

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=200),
        data="xrocket_check:888",
        message=SimpleNamespace(chat=SimpleNamespace(id=200)),
    )
    await main.xrocket_check(cb)

    mock_client.get_invoice.assert_awaited_once_with("888")
    mock_db.complete_payment.assert_awaited_once_with(200, "xrocket_888", 350, "USDT", "vip")
    mock_db.update_xrocket_invoice_status.assert_awaited_once_with("888", "paid")
    mock_show.assert_awaited_once()


async def test_xrocket_check_invoice_wrong_user(monkeypatch):
    mock_db = SimpleNamespace(
        get_xrocket_invoice=AsyncMock(
            return_value=("888", 999, "3.5", "USDT", "active", "https://t.me/xrocket?start=inv_888")
        ),
        check_user_exists_privilege=AsyncMock(return_value=False),
        complete_payment=AsyncMock(),
    )
    mock_cb_answer = AsyncMock()

    monkeypatch.setattr(main, "db", mock_db, raising=False)
    monkeypatch.setattr(main, "safe_cb_answer", mock_cb_answer)

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=200),
        data="xrocket_check:888",
        message=SimpleNamespace(chat=SimpleNamespace(id=200)),
    )
    await main.xrocket_check(cb)

    mock_cb_answer.assert_awaited_once()
    assert "другому пользователю" in mock_cb_answer.call_args[0][1]
    mock_db.complete_payment.assert_not_awaited()


async def test_xrocket_poll_loop(monkeypatch):
    import asyncio

    mock_client = SimpleNamespace(
        get_invoice=AsyncMock(
            return_value={
                "id": "999",
                "priceAmount": "1.5",
                "priceCurrency": "USDT",
                "status": "paid",
            }
        )
    )
    mock_db = SimpleNamespace(
        get_active_xrocket_invoices=AsyncMock(
            return_value=[("999", 300, "1.5", "USDT", "https://t.me/xrocket?start=inv_999")]
        ),
        complete_payment=AsyncMock(return_value="granted"),
        update_xrocket_invoice_status=AsyncMock(),
    )
    mock_say = AsyncMock()

    monkeypatch.setattr(main, "db", mock_db, raising=False)
    monkeypatch.setattr(main, "bot", object(), raising=False)
    monkeypatch.setattr(main, "say", mock_say)

    stop = asyncio.Event()
    call_count = 0
    orig_wait_for = asyncio.wait_for

    async def fast_wait_for(fut, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            try:
                fut.close()
            except Exception:
                pass
            raise asyncio.TimeoutError()
        stop.set()
        return await orig_wait_for(fut, 0.01)

    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

    await main._xrocket_poll_loop(mock_client, stop)

    mock_db.complete_payment.assert_awaited_once_with(300, "xrocket_999", 150, "USDT", "vip")
    mock_db.update_xrocket_invoice_status.assert_awaited_once_with("999", "paid")
    mock_say.assert_awaited_once()


async def test_xrocket_create_invoice_gram_fixed_price(monkeypatch):
    monkeypatch.setattr(main.config, "xrocket_enabled", True)
    mock_client = SimpleNamespace(
        create_invoice=AsyncMock(
            return_value={
                "id": "901",
                "priceAmount": "2.2",
                "priceCurrency": "GRAM",
                "status": "active",
                "pay_url": "https://t.me/xrocket?start=inv_901",
            }
        )
    )
    mock_db = SimpleNamespace(
        check_user_exists_privilege=AsyncMock(return_value=False),
        save_xrocket_invoice=AsyncMock(),
    )
    mock_show = AsyncMock()

    monkeypatch.setattr(main, "xrocket_client", mock_client)
    monkeypatch.setattr(main, "db", mock_db, raising=False)
    monkeypatch.setattr(main, "show", mock_show)
    monkeypatch.setattr(main, "safe_cb_answer", AsyncMock())

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=101),
        data="xrocket_create:GRAM",
        message=SimpleNamespace(chat=SimpleNamespace(id=101)),
    )
    await main.xrocket_create(cb)

    mock_client.create_invoice.assert_awaited_once_with(
        amount=2.2,
        currency="GRAM",
        description="VIP доступ Revolution",
        user_id=101,
    )
    mock_db.save_xrocket_invoice.assert_awaited_once_with(
        "901", 101, "2.2", "GRAM", "https://t.me/xrocket?start=inv_901"
    )
    mock_show.assert_awaited_once()


async def test_xrocket_check_gram_underpaid(monkeypatch):
    mock_client = SimpleNamespace(
        get_invoice=AsyncMock(
            return_value={
                "id": "902",
                "priceAmount": "2",
                "priceCurrency": "GRAM",
                "status": "paid",
            }
        )
    )
    mock_db = SimpleNamespace(
        get_xrocket_invoice=AsyncMock(
            return_value=("902", 201, "2", "GRAM", "active", "https://t.me/xrocket?start=inv_902")
        ),
        check_user_exists_privilege=AsyncMock(return_value=False),
        complete_payment=AsyncMock(return_value="granted"),
        update_xrocket_invoice_status=AsyncMock(),
    )
    mock_cb_answer = AsyncMock()

    monkeypatch.setattr(main, "xrocket_client", mock_client)
    monkeypatch.setattr(main, "db", mock_db, raising=False)
    monkeypatch.setattr(main, "show", AsyncMock())
    monkeypatch.setattr(main, "safe_cb_answer", mock_cb_answer)

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=201),
        data="xrocket_check:902",
        message=SimpleNamespace(chat=SimpleNamespace(id=201)),
    )
    await main.xrocket_check(cb)

    mock_db.complete_payment.assert_not_awaited()
    mock_db.update_xrocket_invoice_status.assert_not_awaited()
    assert "меньше требуемой" in mock_cb_answer.call_args[0][1]
