from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

logger = logging.getLogger("xrocket")

class XRocketError(Exception):
    pass

class XRocketClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://pay.api.xrocket.exchange",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._own_session = False
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=15.0),
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "User-Agent": "Revolution-Bot/1.0",
                        },
                    )
                    self._own_session = True
        return self._session

    async def _drop_own_session(self) -> None:
        async with self._session_lock:
            session, owned = self._session, self._own_session
            self._session = None
            self._own_session = False
        if owned and session is not None and not session.closed:
            try:
                await session.close()
            except Exception as exc:
                logger.debug("xrocket session close failed: %r", exc)

    async def create_invoice(
        self,
        amount: str | float,
        currency: str = "USDT",
        *,
        description: str = "VIP доступ Revolution",
        user_id: int | None = None,
        expires_in_sec: int = 3600,
    ) -> dict[str, Any]:
        session = await self._get_session()
        url = f"{self.base_url}/api/v1/invoices"
        payload: dict[str, Any] = {
            "priceAmount": str(amount),
            "priceCurrency": currency.upper().strip(),
            "description": description[:1000],
            "expiresIn": max(300, int(expires_in_sec)) * 1000,
        }
        if user_id is not None:
            payload["customer"] = {"telegramId": str(user_id)}
            payload["clientInvoiceId"] = f"vip_{user_id}_{int(time.time())}"

        try:
            async with session.post(url, json=payload) as resp:
                text = await resp.text()
                if resp.status not in (200, 201):
                    logger.error("xrocket create_invoice failed [%d]: %s", resp.status, text)
                    raise XRocketError(f"HTTP {resp.status}: {text}")
                data = json.loads(text)
        except asyncio.CancelledError:
            await self._drop_own_session()
            raise
        except Exception as exc:
            await self._drop_own_session()
            if not isinstance(exc, XRocketError):
                logger.error("xrocket create_invoice request error: %r", exc)
                raise XRocketError(f"Ошибка сети xRocket: {exc}") from exc
            raise

        inv_id = str(data.get("id"))
        links = data.get("links") or {}
        pay_url = (
            links.get("telegramBotLink")
            or links.get("botTelegramUrl")
            or links.get("miniAppPayUrl")
            or links.get("webPayUrl")
            or f"https://t.me/xrocket?start=inv_{inv_id}"
        )
        return {
            "id": inv_id,
            "priceAmount": str(data.get("priceAmount", amount)),
            "priceCurrency": str(data.get("priceCurrency", currency)),
            "status": str(data.get("status", "active")),
            "pay_url": pay_url,
            "raw": data,
        }

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        session = await self._get_session()
        url = f"{self.base_url}/api/v1/invoice?invoiceId={invoice_id}"
        try:
            async with session.get(url) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("xrocket get_invoice failed [%d]: %s", resp.status, text)
                    raise XRocketError(f"HTTP {resp.status}: {text}")
                data = json.loads(text)
        except asyncio.CancelledError:
            await self._drop_own_session()
            raise
        except Exception as exc:
            await self._drop_own_session()
            if not isinstance(exc, XRocketError):
                logger.error("xrocket get_invoice request error: %r", exc)
                raise XRocketError(f"Ошибка сети xRocket: {exc}") from exc
            raise

        links = data.get("links") or {}
        inv_id = str(data.get("id", invoice_id))
        pay_url = (
            links.get("telegramBotLink")
            or links.get("botTelegramUrl")
            or links.get("miniAppPayUrl")
            or links.get("webPayUrl")
            or f"https://t.me/xrocket?start=inv_{inv_id}"
        )
        return {
            "id": inv_id,
            "priceAmount": str(data.get("priceAmount", "")),
            "priceCurrency": str(data.get("priceCurrency", "")),
            "status": str(data.get("status", "unknown")),
            "pay_url": pay_url,
            "raw": data,
        }

    async def close(self) -> None:
        if self._own_session:
            await self._drop_own_session()
