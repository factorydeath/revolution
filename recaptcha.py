from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from pypasser.captcha import recaptcha

_solver_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="recaptcha-solver")


class RecaptchaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecaptchaV3:
    anchor: str
    ttl: float = 0.0

    @classmethod
    def from_raw(cls, raw: Any) -> "RecaptchaV3":
        if isinstance(raw, str):
            anchor, ttl = raw.strip(), 0.0
        elif isinstance(raw, dict):
            anchor = str(raw.get("anchor") or raw.get("anchor_url") or "").strip()
            try:
                ttl = float(raw.get("ttl", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("recaptcha_v3.ttl должен быть числом") from exc
        else:
            raise ValueError("recaptcha_v3 должен быть anchor URL или объектом")
        if not anchor.startswith("https://www.google.com/recaptcha/"):
            raise ValueError("recaptcha_v3: нужен полный Google anchor URL")
        if not (0 <= ttl <= 110):
            raise ValueError("recaptcha_v3.ttl должен быть от 0 до 110 секунд")
        return cls(anchor=anchor, ttl=ttl)


class RecaptchaCache:
    _MAX_CACHE_SIZE = 64
    
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self._values: dict[str, tuple[float, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _prune_expired(self) -> None:
        """Удаляет истекшие записи"""
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._values.items() if exp <= now]
        for key in expired:
            self._values.pop(key, None)
            self._locks.pop(key, None)
    
    def _prune_oldest(self) -> None:
        """Удаляет старейшие записи если кеш переполнен"""
        if len(self._values) <= self._MAX_CACHE_SIZE:
            return
        # Сортируем по времени истечения и удаляем треть старейших
        sorted_items = sorted(self._values.items(), key=lambda x: x[1][0])
        to_remove = len(self._values) - self._MAX_CACHE_SIZE
        for key, _ in sorted_items[:to_remove]:
            self._values.pop(key, None)
            self._locks.pop(key, None)

    async def token(
        self,
        service: str,
        settings: RecaptchaV3,
        proxy: str | None,
    ) -> str:
        now = time.monotonic()
        cached = self._values.get(service)
        if cached is not None and cached[0] > now:
            return cached[1]

        lock = self._locks.setdefault(service, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._values.get(service)
            if cached is not None and cached[0] > now:
                return cached[1]
            try:
                if proxy:
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        _solver_executor,
                        lambda: recaptcha.v3.solve(
                            anchor_url=settings.anchor,
                            proxy=proxy,
                            timeout=self.timeout,
                        ),
                    )
                else:
                    result = await recaptcha.v3.asolve(
                        anchor_url=settings.anchor,
                        proxy=None,
                        timeout=self.timeout,
                    )
            except Exception as exc:
                raise RecaptchaError(f"PyPasser: {exc}") from exc

            token = result.response_token
            if not result.is_solved or not isinstance(token, str) or not token.strip():
                raise RecaptchaError("PyPasser вернул пустой токен")
            token = token.strip()
            if settings.ttl > 0:
                now = time.monotonic()
                # Очистка истекших перед добавлением новой записи
                self._prune_expired()
                if len(self._values) >= self._MAX_CACHE_SIZE:
                    self._prune_oldest()
                self._values[service] = (now + settings.ttl, token)
            else:
                self._values.pop(service, None)
            return token
