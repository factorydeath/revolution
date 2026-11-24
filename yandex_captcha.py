from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from yandexcaptcha import (
    ChallengeRequiredError,
    Profile,
    YandexCaptchaSolver,
    discover,
)

logger = logging.getLogger(__name__)

__all__ = ["YandexCaptcha", "YandexCaptchaCache", "YandexCaptchaError"]


class YandexCaptchaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YandexCaptcha:
    sitekey: str = ""
    host: str = ""
    href: str = ""
    page_url: str = ""
    ttl: float = 0.0
    cipher: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "YandexCaptcha":
        if isinstance(raw, str):
            url = raw.strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError(
                    "yandex_captcha: строка должна быть URL страницы "
                    "или нужен объект {sitekey, host}"
                )
            return cls(page_url=url)

        if not isinstance(raw, dict):
            raise ValueError("yandex_captcha: нужен URL страницы или объект {sitekey, host}")

        sitekey = str(raw.get("sitekey") or "").strip()
        host = str(raw.get("host") or "").strip()
        href = str(raw.get("href") or "").strip()
        page_url = str(raw.get("url") or raw.get("page_url") or "").strip()
        cipher = str(raw.get("cipher") or "").strip().lower()
        try:
            ttl = float(raw.get("ttl", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("yandex_captcha.ttl должен быть числом") from exc

        if not (0 <= ttl <= 120):
            raise ValueError("yandex_captcha.ttl должен быть от 0 до 120 секунд")
        if cipher not in ("", "a", "b"):
            raise ValueError("yandex_captcha.cipher: только 'a' или 'b'")
        if not sitekey and not page_url:
            raise ValueError("yandex_captcha: нужен sitekey (с host) или url страницы")
        if sitekey and not host:
            raise ValueError("yandex_captcha: для sitekey нужен host")
        if sitekey and not sitekey.startswith(("ysc1_", "ysc2_")):
            raise ValueError("yandex_captcha: sitekey должен начинаться с ysc1_")
        if page_url and not page_url.startswith(("http://", "https://")):
            raise ValueError("yandex_captcha: url должен начинаться с http(s)://")

        return cls(
            sitekey=sitekey,
            host=host,
            href=href,
            page_url=page_url,
            ttl=ttl,
            cipher=cipher,
        )


class YandexCaptchaCache:
    def __init__(self, timeout: float = 25.0, *, retries: int = 2) -> None:
        self.timeout = max(5.0, float(timeout))
        self.retries = max(1, int(retries))
        self._values: dict[tuple[str, str], tuple[float, str]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._discover_locks: dict[str, asyncio.Lock] = {}
        self._resolved: dict[str, YandexCaptcha] = {}
        self._solvers: dict[tuple[str, str, str, str, str], YandexCaptchaSolver] = {}
        self._solver_locks: dict[tuple[str, str, str, str, str], asyncio.Lock] = {}
        self._closed = False

    async def token(
        self,
        service: str,
        settings: YandexCaptcha,
        proxy: str | None,
    ) -> str:
        if self._closed:
            raise YandexCaptchaError("кэш уже закрыт")

        resolved = await self._resolve(service, settings, proxy)
        if resolved.ttl <= 0:
            return await self._solve(resolved, proxy)

        cache_key = (service, proxy or "")
        now = time.monotonic()
        cached = self._values.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._values.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]
            token = await self._solve(resolved, proxy)
            self._values[cache_key] = (now + resolved.ttl, token)
            return token

    async def _resolve(
        self,
        service: str,
        settings: YandexCaptcha,
        proxy: str | None,
    ) -> YandexCaptcha:
        if settings.sitekey:
            return settings
        cached = self._resolved.get(service)
        if cached is not None:
            return cached

        lock = self._discover_locks.setdefault(service, asyncio.Lock())
        async with lock:
            cached = self._resolved.get(service)
            if cached is not None:
                return cached
            try:
                info = await asyncio.wait_for(
                    discover(settings.page_url, proxy=proxy or None),
                    self.timeout,
                )
            except asyncio.TimeoutError as exc:
                raise YandexCaptchaError(
                    f"sitekey не найден: {settings.page_url} не ответила "
                    f"за {self.timeout:.0f}с"
                ) from exc
            except Exception as exc:
                raise YandexCaptchaError(
                    f"sitekey не найден на {settings.page_url}: {exc}"
                ) from exc

            resolved = YandexCaptcha(
                sitekey=info.sitekey,
                host=info.host,
                href=info.href or settings.href,
                page_url=settings.page_url,
                ttl=settings.ttl,
                cipher=settings.cipher,
            )
            self._resolved[service] = resolved
            logger.info(
                "yandex captcha: %s -> sitekey %s (%s)",
                settings.page_url,
                info.sitekey[:16],
                info.source,
            )
            return resolved

    async def _solve(self, settings: YandexCaptcha, proxy: str | None) -> str:
        key = (
            settings.sitekey,
            settings.host,
            settings.href,
            settings.cipher,
            proxy or "",
        )
        last: BaseException | None = None
        for attempt in range(self.retries):
            solver = await self._solver(key, settings, proxy)
            try:
                result = await asyncio.wait_for(solver.solve_detailed(), self.timeout)
            except ChallengeRequiredError as exc:
                raise YandexCaptchaError(
                    f"IP помечен SmartCaptcha ({exc.kind}); нужен другой прокси"
                ) from exc
            except asyncio.TimeoutError as exc:
                last = exc
                await self._drop(key)
            except Exception as exc:
                last = exc
                await self._drop(key)
            else:
                token = result.token.strip()
                if token:
                    return token
                last = YandexCaptchaError("пустой токен")
                await self._drop(key)
            await asyncio.sleep(0.2 * (attempt + 1))

        raise YandexCaptchaError(f"SmartCaptcha не отдала токен: {last!r}")

    async def _solver(
        self,
        key: tuple[str, str, str, str, str],
        settings: YandexCaptcha,
        proxy: str | None,
    ) -> YandexCaptchaSolver:
        solver = self._solvers.get(key)
        if solver is not None:
            return solver
        lock = self._solver_locks.setdefault(key, asyncio.Lock())
        async with lock:
            solver = self._solvers.get(key)
            if solver is not None:
                return solver
            solver = YandexCaptchaSolver(
                sitekey=settings.sitekey,
                host=settings.host,
                href=settings.href or f"https://{settings.host}/",
                proxy=proxy or None,
                profile=Profile.reference(settings.cipher) if settings.cipher else None,
            )
            await solver.__aenter__()
            self._solvers[key] = solver
            return solver

    async def _drop(self, key: tuple[str, str, str, str, str]) -> None:
        solver = self._solvers.pop(key, None)
        if solver is None:
            return
        try:
            await solver.aclose()
        except Exception:
            logger.debug("yandex solver close failed", exc_info=True)

    async def aclose(self) -> None:
        self._closed = True
        solvers = list(self._solvers.values())
        self._solvers.clear()
        for solver in solvers:
            try:
                await solver.aclose()
            except Exception:
                logger.debug("yandex solver close failed", exc_info=True)
