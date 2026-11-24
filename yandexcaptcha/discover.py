from __future__ import annotations

import asyncio
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .proxy import Proxy, connector_for, detect_proxy, parse_proxy, request_proxy

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/157.0.0.0 Safari/537.36"
)

SITEKEY_RE = re.compile(r"\bysc[01]_[A-Za-z0-9_\-]{12,}")

_DATA_ATTR_RE = re.compile(
    r"""data-sitekey\s*=\s*["']?([A-Za-z0-9_\-]{10,})""", re.IGNORECASE
)
_URL_PARAM_RE = re.compile(
    r"""(?:[?&]|["'])sitekey=([A-Za-z0-9_\-]{10,})""", re.IGNORECASE
)
_SCRIPT_SRC_RE = re.compile(
    r"""<script[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_CAPTCHAISH_RE = re.compile(r"captcha|smartcaptcha|recaptcha", re.IGNORECASE)


class DiscoveryError(Exception):
    pass


@dataclass(slots=True)
class SiteKeyInfo:
    sitekey: str
    url: str
    href: str
    host: str
    source: str = "html"
    scanned: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "sitekey": self.sitekey,
            "url": self.url,
            "href": self.href,
            "host": self.host,
            "source": self.source,
            "scanned": list(self.scanned),
        }


def host_of(url: str) -> str:
    parsed = urllib.parse.urlsplit(url if "//" in url else f"//{url}")
    host = parsed.hostname or parsed.netloc or url
    return host.strip().strip("/")


def normalize_url(url: str) -> str:
    if "//" not in url:
        url = f"https://{url}"
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme or "https"
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def find_sitekey(html: str) -> tuple[str, str] | None:
    match = _DATA_ATTR_RE.search(html)
    if match and SITEKEY_RE.fullmatch(match.group(1)):
        return match.group(1), "attribute"

    match = _URL_PARAM_RE.search(html)
    if match and SITEKEY_RE.fullmatch(match.group(1)):
        return match.group(1), "url-param"

    match = SITEKEY_RE.search(html)
    if match:
        return match.group(0), "html"
    return None


def find_script_urls(html: str, page_url: str) -> list[str]:
    base = urllib.parse.urlsplit(page_url)
    out: list[str] = []
    for raw in _SCRIPT_SRC_RE.findall(html):
        src = raw.strip()
        if not src or src.startswith(("data:", "blob:")):
            continue
        absolute = urllib.parse.urljoin(page_url, src)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        same_origin = parts.netloc == base.netloc
        if not (same_origin or _CAPTCHAISH_RE.search(absolute)):
            continue
        if absolute not in out:
            out.append(absolute)
    out.sort(key=lambda u: 0 if _CAPTCHAISH_RE.search(u) else 1)
    return out


def _decode(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    charset = match.group(1) if match else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


async def discover(
    url: str,
    *,
    session: aiohttp.ClientSession | None = None,
    deep: bool = True,
    timeout: float = 15.0,
    user_agent: str | None = None,
    proxy: Proxy | str | None = None,
    max_scripts: int = 12,
    max_script_bytes: int = 4_000_000,
    verify_ssl: bool = True,
) -> SiteKeyInfo:
    page_url = normalize_url(url)

    proxy_obj = parse_proxy(proxy) if proxy is not None else None
    if proxy_obj is not None and proxy_obj.needs_detection:
        proxy_obj = await detect_proxy(proxy_obj, timeout=min(timeout, 10.0))
    proxy_url = request_proxy(proxy_obj)

    owns = session is None
    if session is None:
        connector_kwargs: dict[str, Any] = {}
        if not verify_ssl:
            connector_kwargs["ssl"] = False
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            connector=connector_for(proxy_obj, **connector_kwargs),
        )

    headers = {
        "User-Agent": user_agent or DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }

    scanned: list[str] = []
    try:
        async with session.get(
            page_url, headers=headers, proxy=proxy_url, allow_redirects=True
        ) as response:
            response.raise_for_status()
            html = _decode(await response.read(), response.headers.get("Content-Type", ""))
            final_url = str(response.url)

        found = find_sitekey(html)
        if found:
            return SiteKeyInfo(
                sitekey=found[0],
                url=final_url,
                href=final_url,
                host=host_of(final_url),
                source=found[1],
            )

        if not deep:
            raise DiscoveryError(f"no SmartCaptcha sitekey in {final_url}")

        budget = max_script_bytes
        for script in find_script_urls(html, final_url):
            if len(scanned) >= max_scripts or budget <= 0:
                break
            try:
                async with session.get(
                    script, headers={**headers, "Accept": "*/*"}, proxy=proxy_url
                ) as response:
                    if response.status != 200:
                        continue
                    raw = await response.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            budget -= len(raw)
            scanned.append(script)
            match = SITEKEY_RE.search(_decode(raw, "utf-8"))
            if match:
                return SiteKeyInfo(
                    sitekey=match.group(0),
                    url=final_url,
                    href=final_url,
                    host=host_of(final_url),
                    source=f"script:{script}",
                    scanned=scanned,
                )

        raise DiscoveryError(
            f"no SmartCaptcha sitekey in {final_url} or in {len(scanned)} scanned script(s)"
        )
    finally:
        if owns:
            await session.close()


def discover_sync(url: str, **kwargs: object) -> SiteKeyInfo:
    return asyncio.run(discover(url, **kwargs))


async def discover_many(
    urls: Iterable[str],
    *,
    concurrency: int = 5,
    session: aiohttp.ClientSession | None = None,
    **kwargs: object,
) -> list[SiteKeyInfo | BaseException]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    owns = session is None
    if session is None:
        session = aiohttp.ClientSession()

    async def run(target: str) -> SiteKeyInfo:
        async with semaphore:
            return await discover(target, session=session, **kwargs)

    try:
        return await asyncio.gather(*(run(u) for u in urls), return_exceptions=True)
    finally:
        if owns:
            await session.close()
