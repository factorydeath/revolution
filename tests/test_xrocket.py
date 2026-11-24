from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from xrocket import XRocketClient, XRocketError


class MockResponse:
    def __init__(self, status: int, data: dict):
        self.status = status
        self._data = data

    async def text(self):
        return json.dumps(self._data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass


@pytest.mark.asyncio
async def test_xrocket_create_invoice_success():
    client = XRocketClient(api_key="test-key")
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.post.return_value = MockResponse(
        201,
        {
            "id": 12345,
            "priceAmount": "1.5",
            "priceCurrency": "USDT",
            "status": "active",
            "links": {"telegramBotLink": "https://t.me/xrocket?start=inv_12345"},
        },
    )
    client._session = mock_session

    res = await client.create_invoice(1.5, "USDT", user_id=42)
    assert res["id"] == "12345"
    assert res["priceAmount"] == "1.5"
    assert res["priceCurrency"] == "USDT"
    assert res["pay_url"] == "https://t.me/xrocket?start=inv_12345"
    assert res["status"] == "active"


@pytest.mark.asyncio
async def test_xrocket_create_invoice_error():
    client = XRocketClient(api_key="test-key")
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.post.return_value = MockResponse(
        400,
        {"message": "Invalid currency"},
    )
    client._session = mock_session

    with pytest.raises(XRocketError):
        await client.create_invoice(1.5, "INVALID")


@pytest.mark.asyncio
async def test_xrocket_get_invoice_success():
    client = XRocketClient(api_key="test-key")
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.get.return_value = MockResponse(
        200,
        {
            "id": 12345,
            "priceAmount": "1.5",
            "priceCurrency": "USDT",
            "status": "paid",
            "links": {"telegramBotLink": "https://t.me/xrocket?start=inv_12345"},
        },
    )
    client._session = mock_session

    res = await client.get_invoice("12345")
    assert res["id"] == "12345"
    assert res["status"] == "paid"
    assert res["priceAmount"] == "1.5"


@pytest.mark.asyncio
async def test_xrocket_close_on_request_error_closes_owned_session():
    client = XRocketClient(api_key="test-key")
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    mock_session.post.side_effect = RuntimeError("network down")
    client._session = mock_session
    client._own_session = True

    with pytest.raises(XRocketError):
        await client.create_invoice(1.5, "USDT")

    mock_session.close.assert_awaited_once()
    assert client._session is None
    assert client._own_session is False


@pytest.mark.asyncio
async def test_xrocket_close():
    client = XRocketClient(api_key="test-key")
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    client._session = mock_session
    client._own_session = True

    await client.close()
    mock_session.close.assert_awaited_once()


def test_api_key_is_not_hardcoded_in_config():
    import pathlib

    source = pathlib.Path(__file__).resolve().parent.parent.joinpath("config.py").read_text(
        encoding="utf-8"
    )
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in source
