from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import warnings
from pathlib import Path
from typing import Any, Iterable

import aiohttp

logger = logging.getLogger(__name__)

__all__ = ["Proxy", "ProxyPool", "load_proxies", "parse_proxy"]

DEFAULT_SCHEME = "http"
SOCKS_SCHEMES = ("socks5", "socks5h", "socks4", "socks4a")


class Proxy:
    __slots__ = (
        "scheme",
        "host",
        "port",
        "username",
        "password",
        "country",
        "url",
        "proxy_url",
        "auth",
    )

    def __init__(
        self,
        scheme: str,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        country: str = "",
    ) -> None:
        self.scheme = scheme
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.country = country.upper().strip() if country else ""
        self.url = f"{scheme}://{host}:{port}"
        self.proxy_url = (
            f"{scheme}://{username}:{password}@{host}:{port}"
            if username
            else self.url
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.auth = (
                aiohttp.BasicAuth(username, password) if username and not self.is_socks else None
            )

    @property
    def is_socks(self) -> bool:
        return self.scheme in SOCKS_SCHEMES

    @property
    def key(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"

    def request_kwargs(self) -> dict:
        if self.is_socks:
            return {}
        kwargs = {"proxy": self.proxy_url}
        if self.auth is not None:
            kwargs["proxy_auth"] = self.auth
        return kwargs

    def __repr__(self) -> str:
        if self.country:
            return f"<Proxy {self.label} [{self.country}]>"
        return f"<Proxy {self.label}>"


def parse_proxy(line: Any, country: str = "") -> Proxy | None:
    if isinstance(line, dict):
        raw_val = line.get("proxy") or line.get("url") or line.get("server") or ""
        c_val = line.get("country") or country
        return parse_proxy(str(raw_val), country=str(c_val))

    raw = (str(line) if line is not None else "").strip().strip('"').strip("'")
    if not raw or raw.startswith("#"):
        return None

    c = country.upper().strip() if country else ""
    if "#" in raw:
        raw_part, _, comment = raw.partition("#")
        raw = raw_part.strip()
        comment_tag = comment.strip().upper()
        if not c and comment_tag in ("RU", "UA", "KZ", "BY"):
            c = comment_tag

    scheme = DEFAULT_SCHEME
    if "://" in raw:
        scheme, _, raw = raw.partition("://")
        scheme = scheme.lower().strip()
        if scheme not in SOCKS_SCHEMES:
            scheme = DEFAULT_SCHEME

    username = password = ""
    if "@" in raw:
        creds, _, raw = raw.rpartition("@")
        if ":" in creds:
            username, _, password = creds.partition(":")
        else:
            username = creds

    parts = raw.split(":")
    if len(parts) == 2:
        host, port_raw = parts
    elif len(parts) == 3:
        host, port_raw, username = parts
    elif len(parts) == 4:
        host, port_raw, username, password = parts
    else:
        return None

    host = host.strip()
    if not host:
        return None
    try:
        port = int(port_raw.strip())
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None

    return Proxy(scheme, host, port, username.strip(), password.strip(), country=c)


def load_proxies(path: str | Path) -> list[Proxy]:
    file_path = Path(path)
    try:
        content = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("proxies: файл не найден: %s", file_path)
        return []
    except OSError as exc:
        logger.warning("proxies: не прочитать %s: %r", file_path, exc)
        return []

    content_clean = content.strip()
    if not content_clean:
        return []

    entries: list[Proxy] = []
    seen: set[tuple[str, str]] = set()

    is_json = file_path.suffix.lower() == ".json" or content_clean.startswith(("{", "["))
    if is_json:
        try:
            data = json.loads(content_clean)
            if isinstance(data, dict):
                for country_key, items in data.items():
                    country = str(country_key).upper().strip()
                    if isinstance(items, list):
                        for item in items:
                            entry = parse_proxy(item, country=country)
                            if entry is None:
                                continue
                            if (entry.key, entry.country) in seen:
                                continue
                            seen.add((entry.key, entry.country))
                            entries.append(entry)
                    elif isinstance(items, (str, dict)):
                        entry = parse_proxy(items, country=country)
                        if entry is not None and (entry.key, entry.country) not in seen:
                            seen.add((entry.key, entry.country))
                            entries.append(entry)
                return entries
            elif isinstance(data, list):
                for item in data:
                    entry = parse_proxy(item)
                    if entry is None:
                        continue
                    if (entry.key, entry.country) in seen:
                        continue
                    seen.add((entry.key, entry.country))
                    entries.append(entry)
                return entries
        except json.JSONDecodeError as exc:
            logger.warning("proxies: %s битый JSON: %r, пробуем как plain text", file_path, exc)

    lines = content.splitlines()
    for number, line in enumerate(lines, start=1):
        entry = parse_proxy(line)
        if entry is None:
            if line.strip() and not line.strip().startswith("#"):
                logger.warning("proxies: строка %d пропущена: %r", number, line[:40])
            continue
        if (entry.key, entry.country) in seen:
            continue
        seen.add((entry.key, entry.country))
        entries.append(entry)
    return entries


class ProxyPool:
    def __init__(
        self,
        entries: Iterable[Proxy],
        session: aiohttp.ClientSession | None = None,
        *,
        cooldown: float = 60.0,
        threshold: int = 3,
        socks_limit: int = 16,
    ) -> None:
        self._entries = list(entries)
        self._session = session
        self._socks_sessions: dict[str, aiohttp.ClientSession] = {}
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._warned_at: dict[str, float] = {}
        self._rng = random.Random()
        self._cooldown = max(1.0, float(cooldown))
        self._threshold = max(1, int(threshold))
        self._socks_limit = max(1, int(socks_limit))
        self._socks_lock = asyncio.Lock()
        self._closing_tasks: set[asyncio.Task] = set()
        self._closed = False

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def update_entries(self, entries: Iterable[Proxy]) -> None:
        new_entries = list(entries)
        new_keys = {e.key for e in new_entries}
        for key in list(self._socks_sessions.keys()):
            if key not in new_keys:
                sess = self._socks_sessions.pop(key, None)
                if sess is not None and not sess.closed:
                    try:
                        task = asyncio.create_task(sess.close())
                        self._closing_tasks.add(task)
                        task.add_done_callback(self._closing_tasks.discard)
                    except Exception:
                        pass
        for key in list(self._cooldown_until.keys()):
            if key not in new_keys:
                self._cooldown_until.pop(key, None)
        for key in list(self._failures.keys()):
            if key not in new_keys:
                self._failures.pop(key, None)
        for key in list(self._warned_at.keys()):
            if key not in new_keys:
                self._warned_at.pop(key, None)
        self._entries = new_entries

    def healthy(self, country: str | None = None) -> list[Proxy]:
        now = time.monotonic()
        if country:
            c = country.upper().strip()
            entries = [entry for entry in self._entries if entry.country == c]
        else:
            entries = self._entries
        return [
            entry
            for entry in entries
            if self._cooldown_until.get(entry.key, 0.0) <= now
        ]

    def pick(self, country: str | None = None) -> Proxy | None:
        if not self._entries:
            return None
        if country:
            c = country.upper().strip()
            country_entries = [entry for entry in self._entries if entry.country == c]
            if not country_entries:
                return None
            now = time.monotonic()
            alive = [
                entry
                for entry in country_entries
                if self._cooldown_until.get(entry.key, 0.0) <= now
            ]
            return self._rng.choice(alive or country_entries)
        alive = self.healthy()
        return self._rng.choice(alive or self._entries)

    def entries_for(self, country: str) -> list[Proxy]:
        c = country.upper().strip()
        return [entry for entry in self._entries if entry.country == c]

    def report(self, proxy: Proxy | None, ok: bool) -> None:
        if proxy is None:
            return
        if ok:
            self._failures.pop(proxy.key, None)
            self._cooldown_until.pop(proxy.key, None)
            return
        failures = self._failures.get(proxy.key, 0) + 1
        if failures >= self._threshold:
            self._failures.pop(proxy.key, None)
            now = time.monotonic()
            self._cooldown_until[proxy.key] = now + self._cooldown
            if now - self._warned_at.get(proxy.key, 0.0) >= self._cooldown:
                self._warned_at[proxy.key] = now
                logger.warning(
                    "proxy %s: %d ошибок подряд, пауза %.0fс",
                    proxy.label,
                    failures,
                    self._cooldown,
                )
        else:
            self._failures[proxy.key] = failures

    async def connection(
        self, proxy: Proxy | None
    ) -> tuple[aiohttp.ClientSession | None, dict]:
        if proxy is None:
            return self._session, {}
        if proxy.is_socks:
            session = await self._socks_session(proxy)
            return session, {}
        return self._session, proxy.request_kwargs()

    async def _socks_session(self, proxy: Proxy) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("proxy pool is closed")
        session = self._socks_sessions.get(proxy.key)
        if session is not None and not session.closed:
            return session
        async with self._socks_lock:
            if self._closed:
                raise RuntimeError("proxy pool is closed")
            session = self._socks_sessions.get(proxy.key)
            if session is not None and not session.closed:
                return session
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(
                proxy.proxy_url,
                limit=self._socks_limit,
                ssl=False,
                rdns=True,
            )
            timeout = getattr(self._session, "timeout", None)
            session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                auto_decompress=True,
                max_line_size=32768,
                max_field_size=32768,
            )
            self._socks_sessions[proxy.key] = session
            return session

    def stats(self) -> dict:
        now = time.monotonic()
        cooling = sum(
            1 for until in self._cooldown_until.values() if until > now
        )
        countries: dict[str, dict[str, int]] = {}
        for entry in self._entries:
            c = entry.country or "OTHER"
            if c not in countries:
                countries[c] = {"total": 0, "cooling": 0, "alive": 0}
            countries[c]["total"] += 1
            if self._cooldown_until.get(entry.key, 0.0) > now:
                countries[c]["cooling"] += 1
            else:
                countries[c]["alive"] += 1

        result = {
            "total": len(self._entries),
            "cooling": cooling,
            "alive": len(self._entries) - cooling,
        }
        if any(entry.country for entry in self._entries):
            result["by_country"] = countries
        return result

    async def close(self) -> None:
        self._closed = True
        async with self._socks_lock:
            sessions = list(self._socks_sessions.values())
            self._socks_sessions.clear()
        for session in sessions:
            try:
                if not session.closed:
                    await session.close()
            except Exception:
                logger.debug("proxy session close failed", exc_info=True)
        if self._closing_tasks:
            pending = [t for t in self._closing_tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._closing_tasks.clear()
