from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import attack
import yandex_captcha
from yandexcaptcha import ChallengeRequiredError
from yandex_captcha import YandexCaptcha, YandexCaptchaCache, YandexCaptchaError

SITEKEY = "ysc1_test0123456789abcdef"
HOST = "example.com"

def test_settings_from_mapping():
    settings = YandexCaptcha.from_raw(
        {"sitekey": SITEKEY, "host": HOST, "ttl": 45, "cipher": "B"}
    )
    assert settings == YandexCaptcha(
        sitekey=SITEKEY, host=HOST, ttl=45, cipher="b"
    )

def test_settings_accept_page_url():
    settings = YandexCaptcha.from_raw("https://example.com/login")
    assert settings.page_url == "https://example.com/login"
    assert not settings.sitekey

@pytest.mark.parametrize(
    "raw,message",
    [
        ({"sitekey": SITEKEY}, "нужен host"),
        ({"host": HOST}, "нужен sitekey"),
        ({"sitekey": "test-key", "host": HOST}, "ysc1_"),
        ({"sitekey": SITEKEY, "host": HOST, "ttl": 500}, "ttl"),
        ({"sitekey": SITEKEY, "host": HOST, "cipher": "c"}, "cipher"),
        ("not-a-url", "URL"),
        (42, "объект"),
    ],
)
def test_settings_reject_bad_input(raw, message):
    with pytest.raises(ValueError, match=message):
        YandexCaptcha.from_raw(raw)

def test_template_accepts_smartcaptcha():
    template = attack.Template.from_raw(
        {
            "name": "Example_RU_SMS",
            "country": "RU",
            "url": "https://example.com/send",
            "body": '{"phone":"{{PLUS}}","captcha":"{{YANDEX}}"}',
            "yandex_captcha": {"sitekey": SITEKEY, "host": HOST},
        }
    )
    job = attack.build_job(template, attack.make_formatter("79161234567"))
    assert job.yandex_captcha == YandexCaptcha(SITEKEY, HOST)
    assert "{{YANDEX}}" in job.body
    assert "{{PLUS}}" not in job.body

def test_smartcaptcha_placeholder_requires_config():
    with pytest.raises(attack.TemplateError, match="неизвестные токены"):
        attack.Template.from_raw(
            {
                "name": "Example_RU_SMS",
                "country": "RU",
                "url": "https://example.com/send",
                "body": "{{YANDEX_CAPTCHA}}",
            }
        )

def test_smartcaptcha_rejects_chains():
    with pytest.raises(attack.TemplateError, match="одиночном запросе"):
        attack.Template.from_raw(
            {
                "name": "Example_RU_SMS",
                "country": "RU",
                "url": "https://example.com/send",
                "body": "{{RAW}}",
                "chain": "multiplex",
                "yandex_captcha": {"sitekey": SITEKEY, "host": HOST},
            }
        )

def test_template_rejects_broken_settings():
    with pytest.raises(attack.TemplateError, match="yandex_captcha"):
        attack.Template.from_raw(
            {
                "name": "Example_RU_SMS",
                "country": "RU",
                "url": "https://example.com/send",
                "yandex_captcha": {"sitekey": SITEKEY},
            }
        )

class FakeSolver:
    instances: list["FakeSolver"] = []
    tokens = ["token-1", "token-2", "token-3"]
    error: BaseException | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = 0
        self.entered = 0
        self.closed = False
        FakeSolver.instances.append(self)

    async def __aenter__(self):
        self.entered += 1
        return self

    async def solve_detailed(self):
        self.calls += 1
        if FakeSolver.error is not None:
            raise FakeSolver.error
        token = FakeSolver.tokens[min(self.calls - 1, len(FakeSolver.tokens) - 1)]
        return SimpleNamespace(token=f"  {token}  ")

    async def aclose(self):
        self.closed = True

@pytest.fixture
def fake_solver(monkeypatch):
    FakeSolver.instances = []
    FakeSolver.error = None
    monkeypatch.setattr(yandex_captcha, "YandexCaptchaSolver", FakeSolver)
    return FakeSolver

async def test_cache_solves_every_time_by_default(fake_solver):
    cache = YandexCaptchaCache(timeout=10)
    settings = YandexCaptcha(SITEKEY, HOST)
    assert await cache.token("svc", settings, None) == "token-1"
    assert await cache.token("svc", settings, None) == "token-2"
    solver = fake_solver.instances[0]
    assert solver.calls == 2
    assert solver.entered == 1
    await cache.aclose()
    assert solver.closed

async def test_cache_reuses_token_with_ttl(fake_solver):
    cache = YandexCaptchaCache(timeout=10)
    settings = YandexCaptcha(SITEKEY, HOST, ttl=60)
    assert await cache.token("svc", settings, None) == "token-1"
    assert await cache.token("svc", settings, None) == "token-1"
    assert fake_solver.instances[0].calls == 1

async def test_cache_never_reuses_token_across_proxy_ips(fake_solver):
    class ProxySolver(FakeSolver):
        async def solve_detailed(self):
            self.calls += 1
            return SimpleNamespace(token=self.kwargs["proxy"])

    yandex_captcha.YandexCaptchaSolver = ProxySolver
    cache = YandexCaptchaCache(timeout=10)
    settings = YandexCaptcha(SITEKEY, HOST, ttl=60)
    first = "http://1.1.1.1:8080"
    second = "http://2.2.2.2:8080"
    assert await cache.token("svc", settings, first) == first
    assert await cache.token("svc", settings, second) == second
    assert await cache.token("svc", settings, first) == first
    assert [solver.calls for solver in fake_solver.instances] == [1, 1]

async def test_cache_single_flight_for_ttl(fake_solver):
    cache = YandexCaptchaCache(timeout=10)
    settings = YandexCaptcha(SITEKEY, HOST, ttl=60)
    results = await asyncio.gather(
        *(cache.token("svc", settings, None) for _ in range(5))
    )
    assert set(results) == {"token-1"}
    assert fake_solver.instances[0].calls == 1

async def test_cache_keeps_solver_per_proxy(fake_solver):
    cache = YandexCaptchaCache(timeout=10)
    settings = YandexCaptcha(SITEKEY, HOST)
    await cache.token("svc", settings, "socks5://1.2.3.4:1080")
    await cache.token("svc", settings, "socks5://1.2.3.4:1080")
    await cache.token("svc", settings, "socks5://5.6.7.8:1080")
    assert len(fake_solver.instances) == 2
    assert fake_solver.instances[0].calls == 2
    assert fake_solver.instances[1].kwargs["proxy"] == "socks5://5.6.7.8:1080"

async def test_cache_reports_flagged_ip(fake_solver):
    fake_solver.error = ChallengeRequiredError({"type": "silhouette"}, {})
    cache = YandexCaptchaCache(timeout=10, retries=3)
    with pytest.raises(YandexCaptchaError, match="другой прокси"):
        await cache.token("svc", YandexCaptcha(SITEKEY, HOST), None)
    assert fake_solver.instances[0].calls == 1

async def test_cache_retries_transport_errors(fake_solver, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    class FlakySolver(FakeSolver):
        attempts = 0

        async def solve_detailed(self):
            self.calls += 1
            FlakySolver.attempts += 1
            if FlakySolver.attempts == 1:
                raise TimeoutError("boom")
            return SimpleNamespace(token="late-token")

    yandex_captcha.YandexCaptchaSolver = FlakySolver
    cache = YandexCaptchaCache(timeout=10, retries=2)
    assert await cache.token("svc", YandexCaptcha(SITEKEY, HOST), None) == "late-token"
    assert fake_solver.instances[0].closed
    assert len(fake_solver.instances) == 2

async def test_cache_discovers_sitekey_once(fake_solver, monkeypatch):
    discover = AsyncMock(
        return_value=SimpleNamespace(
            sitekey=SITEKEY, host=HOST, href="https://example.com/", source="html"
        )
    )
    monkeypatch.setattr(yandex_captcha, "discover", discover)
    cache = YandexCaptchaCache(timeout=10)
    settings = YandexCaptcha.from_raw("https://example.com/login")
    await cache.token("svc", settings, None)
    await cache.token("svc", settings, None)
    discover.assert_awaited_once_with("https://example.com/login", proxy=None)

async def test_cache_discovery_failure_is_wrapped(monkeypatch):
    discover = AsyncMock(side_effect=RuntimeError("no sitekey"))
    monkeypatch.setattr(yandex_captcha, "discover", discover)
    cache = YandexCaptchaCache(timeout=10)
    with pytest.raises(YandexCaptchaError, match="sitekey"):
        await cache.token("svc", YandexCaptcha.from_raw("https://example.com/"), None)

async def test_owned_socks_session_is_reused(monkeypatch):
    import yandexcaptcha.solver as solver_mod

    created: list = []

    class FakeSession:
        closed = False

        def __init__(self, **kwargs):
            created.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(solver_mod.aiohttp, "ClientSession", FakeSession)
    solver = solver_mod.YandexCaptchaSolver(
        sitekey=SITEKEY, host=HOST, proxy="socks5://1.2.3.4:1080"
    )
    try:
        first = await solver._ensure_session()
        second = await solver._ensure_session()
        assert first is second
        assert len(created) == 1
    finally:
        await solver.aclose()
    assert first.closed

async def test_borrowed_session_is_replaced_but_not_closed(monkeypatch):
    import yandexcaptcha.solver as solver_mod

    class FakeSession:
        closed = False

        def __init__(self, **kwargs):
            pass

        async def close(self):
            self.closed = True

    borrowed = FakeSession()
    solver = solver_mod.YandexCaptchaSolver(
        sitekey=SITEKEY, host=HOST, session=borrowed, proxy="socks5://1.2.3.4:1080"
    )
    try:
        session = await solver._ensure_session()
        assert session is not borrowed
        assert not borrowed.closed
    finally:
        await solver.aclose()

class _Content:
    content_length = None

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, _limit: int) -> bytes:
        return self._body

    def at_eof(self) -> bool:
        return True

class _Response:
    content_length = None

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self.content = _Content(body)
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def close(self) -> None:
        pass

class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

async def test_kyivstar_chain_logs_in_before_otp():
    session = _Session([_Response(200), _Response(204)])

    status, body = await attack.CHAINS["kyivstar"](
        session, "380671234567", {"Origin": "https://tv.kyivstar.ua"}, None
    )
    assert status == 204 and body == b""
    assert len(session.calls) == 2
    login, otp = session.calls
    assert login["url"].endswith("/api/v1/authentication/login")
    assert "password=anonymous" in login["data"].decode()
    assert otp["url"].endswith("/api/v2/otp")
    assert otp["json"] == {"phoneNumber": "380671234567", "language": "RU"}
    assert otp["headers"]["Origin"] == "https://tv.kyivstar.ua"
    assert "User-Agent" in otp["headers"]

async def test_kyivstar_chain_stops_when_login_fails():
    session = _Session([_Response(500)])
    status, body = await attack.CHAINS["kyivstar"](
        session, "380671234567", {}, None
    )
    assert status == 500 and body == b""
    assert len(session.calls) == 1
