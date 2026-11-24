from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import aiohttp

from .exceptions import (
    CaptchaRejectedError,
    CaptchaTaskError,
    ChallengeRequiredError,
    ProtocolError,
    TransportError,
)
from .fingerprint import Profile
from .models import CaptchaTask, PowChallenge, SolveResult
from .pow import PowSolver
from .proxy import (
    Proxy,
    ProxyPool,
    SocksConnector,
    detect_proxy,
    request_proxy,
)

log = logging.getLogger("yandexcaptcha")

DEFAULT_ENDPOINT = "https://smartcaptcha.yandexcloud.net/check"
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/157.0.0.0 Safari/537.36"
)

_LOCALE_MAP = {
    "ru": "ru-RU",
    "en": "en-US",
    "tr": "tr-TR",
    "kk": "kk-KZ",
    "uz": "uz-UZ",
    "uk": "uk-UA",
    "be": "be-BY",
    "de": "de-DE",
    "fr": "fr-FR",
    "es": "es-ES",
}

INTERACTIVE_TYPES = frozenset({"silhouette", "image", "text", "audio", "video"})
EMPTY_TDATA = (
    '{"pointerPositions":[],"clickPositions":[],'
    '"keyboard":{"total":0,"copy":0,"paste":0}}'
)


class YandexCaptchaSolver:
    def __init__(
        self,
        sitekey: str,
        host: str,
        href: str | None = None,
        *,
        lang: str = "ru",
        test: bool = False,
        webview: bool = False,
        metadata: str | None = None,
        user_agent: str | None = None,
        profile: Profile | Mapping[str, str] | str | None = None,
        randomize: bool = False,
        proxy: Proxy | str | Sequence[Proxy | str] | None = None,
        timeout: float = 15.0,
        retries: int = 3,
        retry_backoff: float = 0.35,
        min_delay: float = 0.0,
        max_delay: float = 0.0,
        pow_workers: int | None = None,
        connector_limit: int = 0,
        session: aiohttp.ClientSession | None = None,
        verify_ssl: bool = True,
        endpoint: str = DEFAULT_ENDPOINT,
        rng: random.Random | None = None,
    ) -> None:
        if not sitekey:
            raise ValueError("sitekey is required")
        if not host:
            raise ValueError("host is required")

        self.sitekey = sitekey
        self.host = host
        self.href = href or f"https://{host}/"
        self.lang = lang
        self.test = bool(test)
        self.webview = bool(webview)
        self.metadata = metadata
        self.user_agent = user_agent or DEFAULT_UA
        self.retries = max(1, int(retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.min_delay = max(0.0, float(min_delay))
        self.max_delay = max(self.min_delay, float(max_delay))
        self._proxy_pool = ProxyPool.from_any(proxy)
        self.proxy = self._proxy_pool[0] if self._proxy_pool else None
        self.timeout = float(timeout)
        self.verify_ssl = bool(verify_ssl)
        self.endpoint = endpoint.rstrip("/")
        self.connector_limit = int(connector_limit)
        self._rng = rng or random.Random()
        self._profile = self._coerce_profile(profile)
        if randomize:
            self._profile.randomize(self._rng)
        self._pow = PowSolver(workers=pow_workers)
        self._session = session
        self._owns_session = session is None
        self._url = self._build_url()
        self._headers = self._build_headers()

    @classmethod
    async def from_url(
        cls, url: str, *, session: aiohttp.ClientSession | None = None, **options: Any
    ) -> YandexCaptchaSolver:
        from .discover import discover

        info = await discover(url, session=session)
        return cls(
            sitekey=info.sitekey,
            host=info.host,
            href=info.href,
            session=session,
            **options,
        )

    @property
    def session(self) -> aiohttp.ClientSession | None:
        return self._session

    def _coerce_profile(self, profile: Profile | Mapping[str, str] | str | None) -> Profile:
        if profile is None:
            return Profile.reference()
        if isinstance(profile, Profile):
            return profile
        if isinstance(profile, str):
            if os.path.exists(profile):
                return Profile.from_json(profile)
            return Profile.from_rdata(profile)
        reference = Profile.reference()
        reference.update(**profile)
        return reference

    def _build_url(self) -> str:
        params = {"host": self.host, "sitekey": self.sitekey, "href": self.href}
        if self.metadata:
            params["metadata"] = self.metadata
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return f"{self.endpoint}?{query}"

    def _build_headers(self) -> dict[str, str]:
        locale = _LOCALE_MAP.get(self.lang, f"{self.lang}-{self.lang.upper()}")
        return {
            "Accept": "*/*",
            "Accept-Language": f"{locale},{self.lang};q=0.6",
            "Cache-Control": "no-cache",
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": "https://smartcaptcha.yandexcloud.net",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Storage-Access": "none",
            "User-Agent": self.user_agent,
            "sec-ch-ua": '"Not-A.Brand";v="99", "Chromium";v="157", "Google Chrome";v="157"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if (
            self._session is not None
            and not self._session.closed
            and not self._owns_session
            and self.proxy is not None
            and not self.proxy.is_http
        ):
            self._session = None
        if self._session is None or self._session.closed:
            connector_kwargs: dict[str, Any] = {
                "limit": self.connector_limit,
                "ttl_dns_cache": 300,
            }
            if not self.verify_ssl:
                connector_kwargs["ssl"] = False
            proxy = self.proxy
            if proxy is not None and proxy.needs_detection:
                proxy = await detect_proxy(proxy, timeout=min(self.timeout, 10.0))
                self.proxy = proxy
            if proxy is not None and proxy.is_socks:
                connector_kwargs.pop("ttl_dns_cache", None)
                connector: aiohttp.TCPConnector = SocksConnector(proxy, **connector_kwargs)
            else:
                connector = aiohttp.TCPConnector(**connector_kwargs)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers=None,
            )
            self._owns_session = True
        return self._session

    async def _post(self, body: str) -> dict[str, Any]:
        session = await self._ensure_session()
        data = body.encode("utf-8")
        proxy_url = request_proxy(self.proxy)
        try:
            async with session.post(
                self._url,
                data=data,
                headers=self._headers,
                proxy=proxy_url,
            ) as resp:
                raw = await resp.read()
                if resp.status != 200:
                    raise TransportError(
                        f"check endpoint returned HTTP {resp.status}: {raw[:200]!r}",
                        status=resp.status,
                    )
        except asyncio.TimeoutError as exc:
            raise TransportError(f"request timed out after {self.timeout:.1f}s") from exc
        except aiohttp.ClientError as exc:
            raise TransportError(f"network error: {exc}") from exc

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ProtocolError(f"non-JSON response: {raw[:200]!r}") from exc
        if not isinstance(payload, dict):
            raise ProtocolError(f"unexpected response type: {type(payload).__name__}")
        return payload

    @staticmethod
    def _quote(value: Any) -> str:
        return urllib.parse.quote(str(value), safe="")

    def _build_task_body(self) -> str:
        return f"sitekey={self._quote(self.sitekey)}"

    def _next_delay(self) -> float:
        if self.max_delay <= 0:
            return 0.0
        return self._rng.uniform(self.min_delay, self.max_delay)

    def _build_picasso(self) -> str:
        items = []
        for rounds, factor, blur in ((5, 1.5, 50), (10, 2, 70)):
            items.append(
                {
                    "seed": self._rng.randrange(1000),
                    "rounds": rounds,
                    "width": 300,
                    "height": 300,
                    "fontSizeFactor": factor,
                    "maxShadowBlur": blur,
                    "result": os.urandom(16).hex(),
                    "calcTime": self._rng.randrange(15, 90),
                }
            )
        raw = json.dumps(items, separators=(",", ":")).encode()
        return base64.b64encode(raw).decode()

    def _build_submit_body(self, task: CaptchaTask, nonce: str, calc_ms: int) -> str:
        pdata = base64.b64encode(
            json.dumps(
                {
                    "powNonce": nonce,
                    "powCalcTime": calc_ms,
                    "powPrefix": task.pow.prefix,
                },
                separators=(",", ":"),
            ).encode()
        ).decode()

        fields = {
            "rdata": self._profile.to_rdata(),
            "key": task.captcha_key,
            "pdata": pdata,
            "picasso": self._build_picasso(),
            "sitekey": self.sitekey,
            "lang": self.lang,
            "test": "true" if self.test else "false",
            "webview": "true" if self.webview else "false",
            "tdata": base64.b64encode(EMPTY_TDATA.encode()).decode(),
        }
        return "&".join(f"{k}={self._quote(v)}" for k, v in fields.items())

    @staticmethod
    def _parse_task(payload: dict[str, Any]) -> CaptchaTask:
        captcha = payload.get("captcha")
        pow_data = payload.get("pow")
        if not isinstance(captcha, dict) or not isinstance(pow_data, dict):
            raise CaptchaTaskError(f"malformed task payload: {payload!r}")
        key = captcha.get("key")
        prefix = pow_data.get("prefix")
        if not key or not prefix:
            raise CaptchaTaskError(f"task is missing key/prefix: {payload!r}")
        return CaptchaTask(
            captcha_key=str(key),
            captcha_type=str(captcha.get("type", "")),
            unique_key=str(payload.get("unique_key", "")),
            pow=PowChallenge(
                prefix=str(prefix), complexity=int(pow_data.get("complexity", 0))
            ),
            raw=payload,
        )

    async def _fetch_task(self) -> CaptchaTask:
        payload = await self._post(self._build_task_body())
        status = payload.get("status")
        if status == "ok":
            raise CaptchaTaskError(f"unexpected immediate success: {payload!r}")
        if status != "failed":
            raise CaptchaTaskError(f"unexpected task status {status!r}: {payload!r}")
        task = self._parse_task(payload)
        log.debug(
            "task %s type=%s complexity=%s",
            task.unique_key,
            task.captcha_type,
            task.pow.complexity,
        )
        return task

    async def solve_detailed(self) -> SolveResult:
        started = time.perf_counter()
        last_payload: dict[str, Any] = {}

        for attempt in range(1, self.retries + 1):
            try:
                task = await self._fetch_task()
            except TransportError:
                if attempt >= self.retries:
                    raise
                await asyncio.sleep(self.retry_backoff * attempt)
                continue

            solution = await self._pow.solve(task.pow.prefix, task.pow.complexity)

            delay = self._next_delay()
            if delay:
                await asyncio.sleep(delay)

            try:
                payload = await self._post(
                    self._build_submit_body(task, solution.nonce, solution.elapsed_ms)
                )
            except TransportError:
                if attempt >= self.retries:
                    raise
                await asyncio.sleep(self.retry_backoff * attempt)
                continue

            last_payload = payload
            status = payload.get("status")

            if status == "ok":
                token = payload.get("spravka")
                if not isinstance(token, str) or not token:
                    raise ProtocolError(f"success without spravka: {payload!r}")
                elapsed = time.perf_counter() - started
                log.debug(
                    "solved in %.0f ms after %d attempt(s)",
                    elapsed * 1000,
                    attempt,
                )
                return SolveResult(
                    token=token,
                    elapsed=elapsed,
                    attempts=attempt,
                    task=task,
                    raw=payload,
                )

            captcha = payload.get("captcha")
            if isinstance(captcha, dict) and captcha.get("type") in INTERACTIVE_TYPES:
                raise ChallengeRequiredError(captcha, payload)

            if status != "failed":
                raise ProtocolError(f"unexpected submit status {status!r}: {payload!r}")

            if attempt < self.retries:
                await asyncio.sleep(self.retry_backoff * attempt)

        raise CaptchaRejectedError(
            f"captcha was not solved after {self.retries} attempt(s)", last_payload
        )

    async def solve(self) -> str:
        return (await self.solve_detailed()).token

    def __repr__(self) -> str:
        return (
            f"<YandexCaptchaSolver sitekey={self.sitekey[:12]}… "
            f"host={self.host!r} retries={self.retries}>"
        )

    async def aclose(self) -> None:
        await self._pow.aclose()
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> YandexCaptchaSolver:
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def solve_sync(self) -> str:
        return asyncio.run(self._solve_and_close())

    async def _solve_and_close(self) -> str:
        try:
            return await self.solve()
        finally:
            await self.aclose()


async def solve_many(
    jobs: Sequence[Mapping[str, Any]],
    *,
    concurrency: int = 8,
    session: aiohttp.ClientSession | None = None,
    progress: Callable[[int, SolveResult | BaseException], None] | None = None,
    proxy_strategy: str = "round-robin",
    **defaults: Any,
) -> list[SolveResult | BaseException]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    pool: ProxyPool | None
    if isinstance(defaults.get("proxy"), ProxyPool):
        pool = defaults["proxy"]
    else:
        pool = ProxyPool.from_any(defaults.get("proxy"), strategy=proxy_strategy)
    if pool is not None:
        defaults = {**defaults, "proxy": None}
        session = None

    owns_session = session is None and pool is None
    if session is None and pool is None:
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=300),
            timeout=aiohttp.ClientTimeout(total=defaults.get("timeout", 15.0)),
        )

    async def attempt(index: int, job: Mapping[str, Any]) -> SolveResult:
        order: list[Proxy | None] = list(pool.order(index)) if pool is not None else [None]
        last: BaseException | None = None
        for proxy in order:
            config = {**defaults, **job}
            if proxy is not None:
                config["proxy"] = proxy
            if session is not None:
                config["session"] = session
            solver = YandexCaptchaSolver(**config)
            try:
                return await solver.solve_detailed()
            except TransportError as exc:
                last = exc
                if pool is not None and proxy is not None:
                    pool.mark_dead(proxy)
                continue
            finally:
                await solver.aclose()
        assert last is not None
        raise last

    async def run(index: int, job: Mapping[str, Any]) -> SolveResult:
        async with semaphore:
            return await attempt(index, job)

    async def tracked(index: int, job: Mapping[str, Any]) -> SolveResult | BaseException:
        try:
            result: SolveResult | BaseException = await run(index, job)
        except BaseException as exc:
            result = exc
        if progress is not None:
            progress(index, result)
        return result

    try:
        gathered = await asyncio.gather(
            *(tracked(i, job) for i, job in enumerate(jobs)), return_exceptions=True
        )
        return list(gathered)
    finally:
        if owns_session and session is not None:
            await session.close()
