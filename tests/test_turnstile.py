from __future__ import annotations

import pytest

import browser
import turnstile_solver
from turnstile_solver import (
    TurnstileError,
    TurnstileResult,
    TurnstileSolver,
    apply_turnstile_token,
    detect_turnstile,
    solve_for_page,
)

SITEKEY = "0x4AAAAAAA_test_sitekey_12345"


class DetectPage:
    url = "https://example.com/login"

    def __init__(self, info=None):
        self.info = info or {}
        self.calls = 0
        self.context = object()

    async def evaluate(self, script, arg=None):
        self.calls += 1
        return self.info


class ApplyPage:
    url = "https://example.com/login"

    def __init__(self):
        self.arg = None

    async def evaluate(self, script, arg=None):
        self.arg = arg
        return True


class FakeRoute:
    def __init__(self):
        self.body = ""

    async def fulfill(self, **kwargs):
        self.body = kwargs.get("body", "")


class FakeLocator:
    def __init__(self):
        self.clicks = 0

    @property
    def first(self):
        return self

    async def click(self, timeout=0):
        self.clicks += 1


class FakePage:
    url = "https://example.com/login"

    def __init__(self, tokens=("", "token-123")):
        self.tokens = list(tokens)
        self.closed = False
        self.route_handler = None
        self.route_predicate = None
        self.goto_url = ""
        self.locator_obj = FakeLocator()

    async def route(self, predicate, handler):
        self.route_predicate = predicate
        self.route_handler = handler

    async def goto(self, url, **kwargs):
        self.goto_url = url
        assert self.route_predicate(url)
        await self.route_handler(FakeRoute())

    async def wait_for_selector(self, selector, **kwargs):
        return None

    async def eval_on_selector(self, selector, script, **kwargs):
        return None

    async def input_value(self, selector, timeout=0):
        return self.tokens.pop(0) if self.tokens else ""

    async def evaluate(self, script, arg=None):
        return ""

    def locator(self, selector):
        return self.locator_obj

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


async def test_detect_accepts_explicit_sitekey_without_dom_scan():
    page = DetectPage({"sitekeys": ["0x4WRONG"]})
    challenge = await detect_turnstile(page, sitekey=SITEKEY, action="login")
    assert challenge is not None
    assert challenge.sitekey == SITEKEY
    assert challenge.action == "login"
    assert challenge.url == "https://example.com/login"
    assert page.calls == 0


async def test_detect_keeps_explicit_sitekey_but_reads_action():
    page = DetectPage({"sitekeys": ["0x4WRONG"], "actions": ["register"]})
    challenge = await detect_turnstile(page, sitekey=SITEKEY)
    assert challenge is not None
    assert challenge.sitekey == SITEKEY
    assert challenge.action == "register"
    assert page.calls == 1


async def test_detect_reads_sitekey_and_action_from_page():
    page = DetectPage(
        {
            "sitekeys": ["", SITEKEY],
            "actions": ["register"],
            "url": "https://example.com/signup",
        }
    )
    challenge = await detect_turnstile(page)
    assert challenge == turnstile_solver.TurnstileChallenge(
        sitekey=SITEKEY,
        url="https://example.com/signup",
        action="register",
    )


async def test_detect_returns_none_when_page_has_no_widget():
    assert await detect_turnstile(DetectPage({"sitekeys": []})) is None
    assert await detect_turnstile(DetectPage({})) is None


async def test_apply_token_sets_hidden_field_and_callbacks():
    page = ApplyPage()
    assert await apply_turnstile_token(page, "abc") is True
    assert page.arg == {"token": "abc", "selector": turnstile_solver.TOKEN_INPUT}
    assert await apply_turnstile_token(page, "   ") is False


async def test_solver_reuses_context_and_closes_only_its_page():
    page = FakePage()
    context = FakeContext(page)
    solver = TurnstileSolver(context=context, timeout=2.0, max_attempts=2, poll_interval=0.01)

    result = await solver.solve(
        "https://example.com/login",
        SITEKEY,
        action="login",
        cdata="extra",
    )

    assert result.is_solved is True
    assert result.token == "token-123"
    assert result.status == "success"
    assert page.closed is True
    assert context.closed is False
    assert page.goto_url == "https://example.com/login"
    assert page.locator_obj.clicks >= 1


async def test_solver_returns_failure_for_bad_url_without_touching_context():
    page = FakePage()
    context = FakeContext(page)
    solver = TurnstileSolver(context=context, timeout=1.0, max_attempts=1)

    result = await solver.solve("not-a-url", SITEKEY)

    assert result.status == "failure"
    assert "http" in (result.reason or "")
    assert page.closed is False
    assert context.closed is False


class NoopSolver:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def solve(self, **kwargs):
        return TurnstileResult(None, 0.1, "failure", "boom")


async def test_solve_for_page_is_non_fatal_by_default(monkeypatch):
    page = DetectPage({"sitekeys": [SITEKEY], "url": "https://example.com/login"})
    monkeypatch.setattr(turnstile_solver, "TurnstileSolver", NoopSolver)

    assert await solve_for_page(page) is None


async def test_solve_for_page_strict_raises(monkeypatch):
    page = DetectPage({"sitekeys": [SITEKEY], "url": "https://example.com/login"})
    monkeypatch.setattr(turnstile_solver, "TurnstileSolver", NoopSolver)

    with pytest.raises(TurnstileError, match="boom"):
        await solve_for_page(page, strict=True)


async def test_browser_wrapper_passes_options(monkeypatch):
    captured = {}

    async def fake_solve(page, **kwargs):
        captured["page"] = page
        captured.update(kwargs)
        return "token"

    monkeypatch.setattr(browser, "solve_for_page", fake_solve)
    page = object()

    assert await browser.solve_turnstile(page, sitekey=SITEKEY, action="login") == "token"
    assert captured["page"] is page
    assert captured["sitekey"] == SITEKEY
    assert captured["action"] == "login"
    assert captured["url"] is None
    assert captured["cdata"] is None
    assert captured["apply"] is True
    assert captured["strict"] is False


async def test_browser_challenge_wrapper_swallows_page_errors():
    class BrokenPage:
        url = "https://example.com"

        async def evaluate(self, script):
            raise RuntimeError("page is busy")

    assert await browser.turnstile_challenge(BrokenPage()) is None


async def test_browser_service_turnstile(monkeypatch):
    captured = {}

    async def fake_solve(page, **kwargs):
        captured["page"] = page
        captured.update(kwargs)
        return "token-from-service"

    monkeypatch.setattr(browser, "solve_turnstile", fake_solve)
    svc = browser.BrowserService()
    page = object()
    assert await svc.turnstile(page, sitekey="abc", action="login") == "token-from-service"
    assert captured["page"] is page
    assert captured["sitekey"] == "abc"
    assert captured["action"] == "login"


def test_proxy_config_accepts_strings_and_objects():
    assert turnstile_solver._proxy_config("") is None
    assert turnstile_solver._proxy_config("http://1.2.3.4:8080") == {
        "server": "http://1.2.3.4:8080"
    }
    assert turnstile_solver._proxy_config("socks5h://user:pass@1.2.3.4:1080") == {
        "server": "socks5://1.2.3.4:1080",
        "username": "user",
        "password": "pass",
    }

    class ProxyObject:
        scheme = "socks5h"
        host = "1.2.3.4"
        port = 1080
        username = "u"
        password = "p"

    assert turnstile_solver._proxy_config(ProxyObject()) == {
        "server": "socks5://1.2.3.4:1080",
        "username": "u",
        "password": "p",
    }
