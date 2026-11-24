from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import random
import socket
import struct
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import aiohttp

__all__ = [
    "Proxy",
    "ProxyHealth",
    "ProxyPool",
    "SocksConnector",
    "parse_proxy",
    "resolve_pool",
    "load_proxies",
    "detect_proxy",
    "connector_for",
    "request_proxy",
    "make_connector",
]

HTTP_SCHEMES = frozenset({"http", "https"})
SOCKS4_SCHEMES = frozenset({"socks4", "socks4a"})
SOCKS5_SCHEMES = frozenset({"socks5", "socks5h", "socks"})
SOCKS_SCHEMES = SOCKS4_SCHEMES | SOCKS5_SCHEMES
AUTO_SCHEMES = frozenset({"auto", ""})
KNOWN_SCHEMES = HTTP_SCHEMES | SOCKS_SCHEMES | AUTO_SCHEMES

_DEFAULT_PORTS = {"http": 8080, "https": 8443, "socks4": 1080, "socks4a": 1080, "socks5": 1080}


@dataclass(frozen=True, slots=True)
class Proxy:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    scheme: str = "auto"

    @property
    def is_http(self) -> bool:
        return self.scheme in HTTP_SCHEMES

    @property
    def is_socks(self) -> bool:
        return self.scheme in SOCKS_SCHEMES

    @property
    def is_socks4(self) -> bool:
        return self.scheme in SOCKS4_SCHEMES

    @property
    def is_socks5(self) -> bool:
        return self.scheme in SOCKS5_SCHEMES

    @property
    def needs_detection(self) -> bool:
        return self.scheme in AUTO_SCHEMES

    @property
    def url(self) -> str:
        auth = ""
        if self.username:
            auth = f"{self.username}:{self.password or ''}@"
        scheme = "http" if self.needs_detection else self.scheme
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{auth}{host}:{self.port}"

    def with_scheme(self, scheme: str) -> Proxy:
        return Proxy(self.host, self.port, self.username, self.password, scheme)

    def as_socks(self) -> Proxy:
        return self.with_scheme("socks5")

    def as_http(self) -> Proxy:
        return self.with_scheme("http")

    def anonymized(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{' (auth)' if self.username else ''}"

    def __str__(self) -> str:
        return self.url

    def __repr__(self) -> str:
        return f"<Proxy {self.scheme}://{self.host}:{self.port}>"


def _split_auth(authority: str) -> tuple[str, str, int]:
    parts = urlsplit(f"//{authority}")
    if not parts.hostname:
        raise ValueError(f"proxy has no host: {authority!r}")
    return parts.hostname, parts.username or "", parts.port or 0


def parse_proxy(value: str | Proxy) -> Proxy:
    if isinstance(value, Proxy):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("empty proxy")

    if "://" in text:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if scheme not in KNOWN_SCHEMES:
            raise ValueError(f"unsupported proxy scheme {scheme!r}")
        if not parts.hostname:
            raise ValueError(f"proxy has no host: {value!r}")
        return Proxy(
            host=parts.hostname,
            port=parts.port or _DEFAULT_PORTS.get(scheme, 1080),
            username=unquote(parts.username) if parts.username else None,
            password=unquote(parts.password) if parts.password else None,
            scheme=scheme,
        )

    fields = text.split(":")
    if len(fields) == 5 and fields[0].lower() in KNOWN_SCHEMES:
        scheme, host, port, username, password = fields
        return Proxy(host, int(port), username, password, scheme.lower())
    if len(fields) == 4:
        host, port, username, password = fields
        return Proxy(host=host, port=int(port), username=username, password=password)
    if len(fields) == 2:
        return Proxy(host=fields[0], port=int(fields[1]))
    if len(fields) == 1 and fields[0]:
        return Proxy(host=fields[0], port=1080)
    raise ValueError(
        f"cannot parse proxy {value!r}; expected scheme://[user:pass@]host:port, "
        "socks5:host:port:user:pass or host:port:user:pass"
    )


def load_proxies(source: str | Path | Sequence[str]) -> list[Proxy]:
    if isinstance(source, (list, tuple)):
        return [parse_proxy(item) for item in source]
    path = Path(str(source)).expanduser()
    if path.is_file():
        entries: list[Proxy] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                entries.append(parse_proxy(line.replace(",", "\t")))
        if not entries:
            raise ValueError(f"{path} contains no proxies")
        return entries
    return [parse_proxy(str(source))]


def resolve_pool(
    value: (
        Proxy
        | str
        | Sequence[Proxy | str]
        | None
        | Path
    ),
) -> list[Proxy]:
    if value is None:
        return []
    if isinstance(value, Proxy):
        return [value]
    if isinstance(value, (list, tuple)):
        items = [p if isinstance(p, str) else p.url for p in value]
        return [parse_proxy(item) for item in items]
    return load_proxies(str(value))


async def detect_proxy(proxy: Proxy | str, *, timeout: float = 10.0) -> Proxy:
    parsed = parse_proxy(proxy)
    if not parsed.needs_detection:
        return parsed
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.host, parsed.port), timeout
        )
    except (OSError, asyncio.TimeoutError):
        return parsed.as_socks()
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(1), timeout)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return parsed.as_socks()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if head == b"\x05":
        return parsed.with_scheme("socks5")
    if head == b"\x00":
        return parsed.with_scheme("socks4")
    if head in (b"H", b"h"):
        return parsed.as_http()
    return parsed.as_socks()


class SocksConnector(aiohttp.TCPConnector):
    def __init__(self, proxy: Proxy | str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._proxy = parse_proxy(proxy)
        self._detect_lock = asyncio.Lock()

    @property
    def proxy(self) -> Proxy:
        return self._proxy

    async def _detect(self, timeout: float) -> None:
        if not self._proxy.needs_detection:
            return
        async with self._detect_lock:
            if self._proxy.needs_detection:
                self._proxy = await detect_proxy(self._proxy, timeout=timeout)
                if self._proxy.needs_detection:
                    self._proxy = self._proxy.as_socks()

    async def _open_socket(self, host: str, port: int, timeout: float = 10.0) -> socket.socket:
        await self._detect(timeout)
        proxy = self._proxy
        if proxy.is_http:
            raise ConnectionError(
                f"{proxy.anonymized()} is an HTTP proxy, not SOCKS; pass it as "
                "request-level proxy= instead of a connector"
            )
        loop = self._loop
        infos = await loop.getaddrinfo(proxy.host, proxy.port, type=socket.SOCK_STREAM)
        if not infos:
            raise OSError(f"cannot resolve proxy host {proxy.host}")
        family, socktype, proto, _canon, address = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, address)
            if proxy.is_socks5:
                await self._socks5(loop, sock, host, port)
            else:
                await self._socks4(loop, sock, host, port)
        except BaseException:
            sock.close()
            raise
        return sock

    async def _socks5(
        self, loop: asyncio.AbstractEventLoop, sock: socket.socket, host: str, port: int
    ) -> None:
        proxy = self._proxy
        use_auth = bool(proxy.username)
        await loop.sock_sendall(sock, b"\x05\x01" + (b"\x02" if use_auth else b"\x00"))
        version, method = await _recv_exact(loop, sock, 2)
        if version != 5:
            raise ConnectionError(f"SOCKS5 proxy spoke version {version}")
        if method == 0x02:
            user = (proxy.username or "").encode()
            password = (proxy.password or "").encode()
            if len(user) > 255 or len(password) > 255:
                raise ValueError("SOCKS5 credentials are limited to 255 bytes")
            await loop.sock_sendall(
                sock, b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password
            )
            _auth_version, status = await _recv_exact(loop, sock, 2)
            if status != 0:
                raise ConnectionError("SOCKS5 authentication rejected")
        elif method != 0x00:
            raise ConnectionError(f"SOCKS5 proxy requires method {method}")

        target = _socks5_address(host)
        await loop.sock_sendall(sock, b"\x05\x01\x00" + target + struct.pack("!H", port))

        _ver, reply, _rsv, atyp = await _recv_exact(loop, sock, 4)
        if reply != 0:
            raise ConnectionError(_SOCKS5_ERRORS.get(reply, f"SOCKS5 reply {reply}"))
        if atyp == 0x01:
            await _recv_exact(loop, sock, 6)
        elif atyp == 0x04:
            await _recv_exact(loop, sock, 18)
        elif atyp == 0x03:
            length = (await _recv_exact(loop, sock, 1))[0]
            await _recv_exact(loop, sock, length + 2)
        else:
            raise ConnectionError(f"unexpected SOCKS5 address type {atyp}")

    async def _socks4(
        self, loop: asyncio.AbstractEventLoop, sock: socket.socket, host: str, port: int
    ) -> None:
        proxy = self._proxy
        user = (proxy.username or "").encode()
        if len(user) > 255:
            raise ValueError("SOCKS4 user id is limited to 255 bytes")
        try:
            packed = ipaddress.IPv4Address(host).packed
            tail = b""
        except ValueError:
            if proxy.scheme == "socks4a":
                packed = b"\x00\x00\x00\x01"
                tail = host.encode("idna") + b"\x00"
            else:
                packed = await self._resolve_ipv4(loop, host)
                tail = b""
        request = struct.pack("!BBH", 4, 1, port) + packed + user + b"\x00" + tail
        await loop.sock_sendall(sock, request)
        reply = await _recv_exact(loop, sock, 8)
        if reply[1] != 0x5A:
            raise ConnectionError(_SOCKS4_ERRORS.get(reply[1], f"SOCKS4 reply {reply[1]}"))

    async def _resolve_ipv4(self, loop: asyncio.AbstractEventLoop, host: str) -> bytes:
        try:
            infos = await loop.getaddrinfo(
                host, 0, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
        except socket.gaierror:
            infos = []
        for info in infos:
            return ipaddress.IPv4Address(info[4][0]).packed
        raise ConnectionError(
            f"SOCKS4 cannot resolve {host!r} to IPv4; use socks4a:// or socks5://"
        )

    async def _create_connection(
        self, req: Any, traces: Any, timeout: Any
    ) -> Any:
        host = req.url.raw_host or req.host
        port = req.port or (443 if req.is_ssl() else 80)
        budget = _connect_budget(timeout)
        try:
            sock = await asyncio.wait_for(
                self._open_socket(host, port, min(budget, 10.0)), budget
            )
        except (OSError, asyncio.TimeoutError, ConnectionError, ValueError) as exc:
            raise aiohttp.ClientConnectorError(
                req.connection_key, OSError(str(exc))
            ) from exc

        ssl_context = self._get_ssl_context(req)
        server_hostname = (req.server_hostname or host).rstrip(".") if ssl_context else None
        try:
            transport, protocol = await self._wrap_existing_connection(
                self._factory,
                req=req,
                timeout=timeout,
                sock=sock,
                ssl=ssl_context,
                server_hostname=server_hostname,
            )
        except BaseException:
            sock.close()
            raise
        fingerprint = self._get_fingerprint(req)
        if fingerprint and transport is not None:
            fingerprint.check(transport)
        return protocol


async def _recv_exact(loop: asyncio.AbstractEventLoop, sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = await loop.sock_recv(sock, size - len(chunks))
        if not chunk:
            raise ConnectionError("SOCKS proxy closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)


def _socks5_address(host: str) -> bytes:
    try:
        packed = ipaddress.ip_address(host).packed
    except ValueError:
        encoded = host.encode("idna")
        if len(encoded) > 255:
            raise ValueError("hostname too long for SOCKS5") from None
        return b"\x03" + bytes([len(encoded)]) + encoded
    return (b"\x01" if len(packed) == 4 else b"\x04") + packed


_SOCKS5_ERRORS = {
    1: "SOCKS5: general failure",
    2: "SOCKS5: connection not allowed by ruleset",
    3: "SOCKS5: network unreachable",
    4: "SOCKS5: host unreachable",
    5: "SOCKS5: connection refused",
    6: "SOCKS5: TTL expired",
    7: "SOCKS5: command not supported",
    8: "SOCKS5: address type not supported",
}

_SOCKS4_ERRORS = {
    0x5B: "SOCKS4: request rejected or failed",
    0x5C: "SOCKS4: identd unreachable",
    0x5D: "SOCKS4: identd mismatch",
}


def _connect_budget(timeout: Any) -> float:
    value = getattr(timeout, "sock_connect", None) or getattr(timeout, "total", None)
    return float(value) if value else 30.0


def connector_for(proxy: Proxy | str | None, **kwargs: Any) -> aiohttp.TCPConnector:
    if proxy is None:
        return aiohttp.TCPConnector(**kwargs)
    parsed = parse_proxy(proxy)
    if parsed.is_socks or parsed.needs_detection:
        return SocksConnector(parsed, **kwargs)
    return aiohttp.TCPConnector(**kwargs)


def request_proxy(proxy: Proxy | str | None) -> str | None:
    if proxy is None:
        return None
    parsed = parse_proxy(proxy)
    return parsed.url if parsed.is_http else None


def make_connector(
    proxy: Proxy | str | None, *, limit: int = 0, ssl: Any = None, **kwargs: Any
) -> aiohttp.TCPConnector | None:
    if proxy is None:
        return None
    parsed = parse_proxy(proxy)
    if not (parsed.is_socks or parsed.needs_detection):
        return None
    return SocksConnector(parsed, limit=limit, ssl=ssl, **kwargs)


@dataclass(slots=True)
class ProxyHealth:
    proxy: Proxy
    ok: bool
    ip: str | None = None
    latency: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proxy": self.proxy.anonymized(),
            "ok": self.ok,
            "ip": self.ip,
            "latency_ms": round(self.latency * 1000) if self.latency else None,
            "error": self.error,
        }


@dataclass
class ProxyPool:
    proxies: list[Proxy] = field(default_factory=list)
    strategy: str = "round-robin"
    _cursor: int = 0
    _rng: random.Random = field(default_factory=random.Random, repr=False)
    _dead: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if self.strategy not in {"round-robin", "random", "sticky"}:
            raise ValueError(f"unknown strategy {self.strategy!r}")
        self.proxies = [parse_proxy(p) for p in self.proxies]

    def __len__(self) -> int:
        return len(self.proxies)

    def __iter__(self) -> Iterator[Proxy]:
        return iter(self.proxies)

    def __getitem__(self, index: int) -> Proxy:
        return self.proxies[index]

    def __bool__(self) -> bool:
        return bool(self.proxies)

    @classmethod
    def from_any(
        cls, value: Any, strategy: str = "round-robin"
    ) -> ProxyPool | None:
        entries = resolve_pool(value)
        if not entries:
            return None
        return cls(entries, strategy)

    def mark_dead(self, proxy: Proxy) -> None:
        self._dead.add(proxy.url)

    def mark_alive(self, proxy: Proxy) -> None:
        self._dead.discard(proxy.url)

    def alive(self) -> list[Proxy]:
        live = [p for p in self.proxies if p.url not in self._dead]
        return live or list(self.proxies)

    def order(self, index: int = 0) -> list[Proxy]:
        pool = self.alive()
        if len(pool) <= 1:
            return pool
        if self.strategy == "sticky":
            return pool
        if self.strategy == "random":
            return self._rng.sample(pool, len(pool))
        start = (index + self._cursor) % len(pool)
        return pool[start:] + pool[:start]

    def next(self) -> Proxy:
        pool = self.alive()
        if not pool:
            raise ValueError("proxy pool is empty")
        if self.strategy == "random":
            return self._rng.choice(pool)
        proxy = pool[self._cursor % len(pool)]
        self._cursor += 1
        return proxy

    async def check(
        self, *, timeout: float = 15.0, url: str = "https://api.ipify.org"
    ) -> list[ProxyHealth]:
        return list(
            await asyncio.gather(*(probe_proxy(p, timeout=timeout, url=url) for p in self.proxies))
        )


async def probe_proxy(
    proxy: Proxy | str, *, timeout: float = 15.0, url: str = "https://api.ipify.org"
) -> ProxyHealth:
    parsed = parse_proxy(proxy)
    if parsed.needs_detection:
        parsed = await detect_proxy(parsed, timeout=min(timeout, 10.0))
    started = time.perf_counter()
    connector = connector_for(parsed, ssl=False if parsed.is_http else None)
    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session, session.get(url, proxy=request_proxy(parsed)) as response:
            body = (await response.text()).strip()
            latency = time.perf_counter() - started
            ok = response.status == 200 and bool(body)
            return ProxyHealth(
                parsed, ok=ok, ip=body if ok else None, latency=latency,
                error=None if ok else f"HTTP {response.status}",
            )
    except Exception as exc:
        return ProxyHealth(parsed, ok=False, error=f"{type(exc).__name__}: {exc}")
