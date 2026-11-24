from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any

import aiohttp

from .discover import SiteKeyInfo, discover, host_of, normalize_url
from .models import SolveResult
from .solver import YandexCaptchaSolver, solve_many

__all__ = [
    "resolve_target",
    "solve",
    "solve_detailed",
    "solve_sync",
    "solve_detailed_sync",
    "solve_many_for_url",
    "SyncSolver",
]


async def resolve_target(
    sitekey: str | None = None,
    host: str | None = None,
    href: str | None = None,
    *,
    url: str | None = None,
    session: aiohttp.ClientSession | None = None,
    discover_options: Mapping[str, Any] | None = None,
) -> tuple[str, str, str]:
    if url:
        page = normalize_url(url)
        if sitekey:
            return sitekey, host or host_of(page), page
        info = await discover(page, session=session, **(discover_options or {}))
        return info.sitekey, host or info.host, href or info.href
    if sitekey and host:
        return sitekey, host, href or f"https://{host}/"
    raise ValueError(
        "provide either url= or both sitekey= and host= "
        "(a bare host works too: url='example.com')"
    )


def _solver_kwargs(options: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in options.items()
        if v is not None and k not in {"session", "sitekey", "host", "href", "url"}
    }


async def solve(
    sitekey: str | None = None,
    host: str | None = None,
    href: str | None = None,
    *,
    url: str | None = None,
    session: aiohttp.ClientSession | None = None,
    **options: Any,
) -> str:
    result = await solve_detailed(
        sitekey, host, href, url=url, session=session, **options
    )
    return result.token


async def solve_detailed(
    sitekey: str | None = None,
    host: str | None = None,
    href: str | None = None,
    *,
    url: str | None = None,
    session: aiohttp.ClientSession | None = None,
    **options: Any,
) -> SolveResult:
    owns_session = False
    if url and not sitekey and session is None:
        session = aiohttp.ClientSession()
        owns_session = True

    try:
        if url and not sitekey:
            info = await discover(url, session=session, proxy=options.get("proxy"))
            sitekey, host, href = info.sitekey, info.host, href or info.href
        elif url:
            href = href or normalize_url(url)
            host = host or host_of(url)

        if not sitekey or not host:
            raise ValueError("provide either url= or both sitekey= and host=")

        solver = YandexCaptchaSolver(
            sitekey=sitekey,
            host=host,
            href=href,
            session=session,
            **_solver_kwargs(options),
        )
        try:
            return await solver.solve_detailed()
        finally:
            await solver.aclose()
    finally:
        if owns_session and session is not None:
            await session.close()


def solve_sync(
    sitekey: str | None = None,
    host: str | None = None,
    href: str | None = None,
    *,
    url: str | None = None,
    **options: Any,
) -> str:
    return asyncio.run(solve(sitekey, host, href, url=url, **options))


def solve_detailed_sync(
    sitekey: str | None = None,
    host: str | None = None,
    href: str | None = None,
    *,
    url: str | None = None,
    **options: Any,
) -> SolveResult:
    return asyncio.run(solve_detailed(sitekey, host, href, url=url, **options))


async def solve_many_for_url(
    url: str,
    count: int = 1,
    *,
    concurrency: int = 8,
    session: aiohttp.ClientSession | None = None,
    **options: Any,
) -> list[SolveResult | BaseException]:
    owns_session = session is None
    if session is None:
        session = aiohttp.ClientSession()
    try:
        info = await discover(url, session=session, proxy=options.get("proxy"))
        jobs = [{"sitekey": info.sitekey} for _ in range(max(1, count))]
        return await solve_many(
            jobs,
            concurrency=concurrency,
            session=session,
            host=info.host,
            href=info.href,
            **options,
        )
    finally:
        if owns_session:
            await session.close()


class SyncSolver:
    def __init__(
        self,
        sitekey: str | None = None,
        host: str | None = None,
        href: str | None = None,
        *,
        url: str | None = None,
        **options: Any,
    ) -> None:
        self._target = {"sitekey": sitekey, "host": host, "href": href, "url": url}
        self._options = _solver_kwargs(options)
        self._solver: YandexCaptchaSolver | None = None
        self._closed = False
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="yandexcaptcha-loop", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def _call(self, coro: Any, timeout: float | None = None) -> Any:
        if self._closed:
            coro.close()
            raise RuntimeError("SyncSolver is closed")
        self._ready.wait(timeout=5.0)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _client(self) -> YandexCaptchaSolver:
        if self._solver is None:
            sitekey, host, href = await resolve_target(
                self._target["sitekey"],
                self._target["host"],
                self._target["href"],
                url=self._target["url"],
            )
            self._solver = YandexCaptchaSolver(
                sitekey=sitekey, host=host, href=href, **self._options
            )
        return self._solver

    async def _solve_detailed(self) -> SolveResult:
        return await (await self._client()).solve_detailed()

    async def _solve_many(
        self, count: int, concurrency: int
    ) -> list[SolveResult | BaseException]:
        solver = await self._client()
        session = await solver._ensure_session()
        jobs = [{"sitekey": solver.sitekey} for _ in range(max(1, count))]
        return await solve_many(
            jobs,
            concurrency=concurrency,
            session=session,
            host=solver.host,
            href=solver.href,
            **self._options,
        )

    def solve_detailed(self) -> SolveResult:
        return self._call(self._solve_detailed())

    def solve(self) -> str:
        return self.solve_detailed().token

    def tokens(self, count: int = 1, *, concurrency: int = 8) -> list[str]:
        return [
            r.token
            for r in self.solve_many(count, concurrency=concurrency)
            if isinstance(r, SolveResult)
        ]

    def solve_many(
        self, count: int = 1, *, concurrency: int = 8
    ) -> list[SolveResult | BaseException]:
        return self._call(self._solve_many(count, concurrency))

    def discover(self) -> SiteKeyInfo:
        if self._target["url"]:
            return self._call(discover(self._target["url"], proxy=self._options.get("proxy")))
        sitekey = self._target["sitekey"]
        host = self._target["host"]
        if not sitekey or not host:
            raise ValueError("SyncSolver has no target")
        href = self._target["href"] or f"https://{host}/"
        return SiteKeyInfo(
            sitekey=sitekey, url=href, href=href, host=host, source="explicit"
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._solver is not None:
                asyncio.run_coroutine_threadsafe(
                    self._solver.aclose(), self._loop
                ).result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    def __enter__(self) -> SyncSolver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        target = self._target.get("url") or self._target.get("host") or self._target.get("sitekey")
        return f"<SyncSolver {target!r} {state}>"
