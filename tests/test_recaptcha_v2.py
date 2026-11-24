from __future__ import annotations

import asyncio

import pytest

import browser
import recaptcha_v2
from pypasser.exceptions import IpBlock
from pypasser.structs import SolverResult
from recaptcha_v2 import (
    RecaptchaBlocked,
    RecaptchaV2Error,
    RecaptchaV2Timeout,
    apply_recaptcha_token,
    detect_recaptcha_v2,
    solve_v2_for_page,
)

SITEKEY = "6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-"


class DetectPage:
    url = "https://example.com/login"

    def __init__(self, info=None):
        self.info = info or {}
        self.calls = 0

    async def evaluate(self, script, arg=None):
        self.calls += 1
        return self.info


class ApplyPage:
    url = "https://example.com/login"

    def __init__(self, result=True, info=None):
        self.info = info or {"sitekeys": [SITEKEY]}
        self.arg = None
        self.result = result

    async def evaluate(self, script, arg=None):
        if arg is None:
            return self.info
        self.arg = arg
        return self.result


async def solver_result(solved=True, token="token-123"):
    return SolverResult(is_solved=solved, response_token=token)


async def test_detect_accepts_explicit_sitekey_without_dom_scan():
    page = DetectPage({"sitekeys": ["0x4WRONG"]})
    challenge = await detect_recaptcha_v2(page, sitekey=SITEKEY)
    assert challenge is not None
    assert challenge.sitekey == SITEKEY
    assert challenge.url == "https://example.com/login"
    assert page.calls == 0


async def test_detect_reads_sitekey_from_page():
    page = DetectPage({"sitekeys": ["", SITEKEY], "url": "https://example.com/signup"})
    challenge = await detect_recaptcha_v2(page)
    assert challenge == recaptcha_v2.RecaptchaV2Challenge(
        sitekey=SITEKEY,
        url="https://example.com/signup",
    )
    assert page.calls == 1


async def test_detect_accepts_anchor_frame_without_sitekey():
    page = DetectPage(
        {
            "anchors": ["https://www.google.com/recaptcha/api2/anchor?k=x"],
            "url": "https://example.com/login",
        }
    )
    challenge = await detect_recaptcha_v2(page)
    assert challenge is not None
    assert challenge.sitekey is None
    assert challenge.url == "https://example.com/login"


async def test_detect_ignores_invisible_widget_without_anchor():
    page = DetectPage({"sitekeys": [], "anchors": []})
    assert await detect_recaptcha_v2(page) is None


async def test_apply_token_sets_hidden_field_and_callbacks():
    page = ApplyPage()
    assert await apply_recaptcha_token(page, "abc") is True
    assert page.arg == {"token": "abc", "selector": recaptcha_v2.RECAPTCHA_INPUT}
    assert await apply_recaptcha_token(page, "   ") is False


async def test_apply_token_returns_false_when_no_field_exists():
    assert await apply_recaptcha_token(ApplyPage(result=False), "abc") is False


async def test_solve_returns_token_and_applies_it():
    page = ApplyPage()
    calls = []

    async def solver(target):
        calls.append(target)
        return await solver_result()

    assert await solve_v2_for_page(page, solver=solver) == "token-123"
    assert calls == [page]
    assert page.arg == {"token": "token-123", "selector": recaptcha_v2.RECAPTCHA_INPUT}


async def test_solve_can_skip_token_application():
    page = ApplyPage()

    async def solver(target):
        return await solver_result()

    assert await solve_v2_for_page(page, solver=solver, apply=False) == "token-123"
    assert page.arg is None


async def test_solve_is_non_fatal_when_unsolved():
    page = DetectPage({"sitekeys": [SITEKEY]})

    async def solver(target):
        return SolverResult(is_solved=False, response_token=None)

    assert await solve_v2_for_page(page, solver=solver) is None


async def test_solve_strict_raises_on_unsolved():
    page = DetectPage({"sitekeys": [SITEKEY]})

    async def solver(target):
        return SolverResult(is_solved=False, response_token=None)

    with pytest.raises(RecaptchaV2Timeout):
        await solve_v2_for_page(page, solver=solver, strict=True)


async def test_solve_maps_ip_block():
    page = DetectPage({"sitekeys": [SITEKEY]})

    async def solver(target):
        raise IpBlock("blocked")

    assert await solve_v2_for_page(page, solver=solver) is None
    with pytest.raises(RecaptchaBlocked, match="IP заблокирован"):
        await solve_v2_for_page(page, solver=solver, strict=True)


async def test_solve_maps_total_timeout():
    page = DetectPage({"sitekeys": [SITEKEY]})

    async def solver(target):
        await asyncio.sleep(1.0)
        return await solver_result()

    assert await solve_v2_for_page(page, solver=solver, timeout=0.3) is None
    with pytest.raises(RecaptchaV2Timeout, match="не уложилась"):
        await solve_v2_for_page(page, solver=solver, timeout=0.3, strict=True)


async def test_solve_returns_none_without_widget():
    page = DetectPage({"sitekeys": [], "anchors": []})
    called = []

    async def solver(target):
        called.append(target)
        return await solver_result()

    assert await solve_v2_for_page(page, solver=solver) is None
    assert called == []


async def test_browser_wrapper_passes_options(monkeypatch):
    captured = {}

    async def fake_solve(page, **kwargs):
        captured["page"] = page
        captured.update(kwargs)
        return "token"

    monkeypatch.setattr(browser, "solve_v2_for_page", fake_solve)
    page = object()

    assert await browser.solve_recaptcha_v2(page, sitekey=SITEKEY, attempts=5) == "token"
    assert captured["page"] is page
    assert captured["sitekey"] == SITEKEY
    assert captured["attempts"] == 5
    assert captured["play"] is False
    assert captured["backend"] == "patchright"
    assert captured["apply"] is True
    assert captured["strict"] is False


async def test_browser_challenge_wrapper_swallows_page_errors():
    class BrokenPage:
        url = "https://example.com"

        async def evaluate(self, script):
            raise RuntimeError("page is busy")

    assert await browser.recaptcha_challenge(BrokenPage()) is None


async def test_browser_service_recaptcha_v2(monkeypatch):
    captured = {}

    async def fake_solve(page, **kwargs):
        captured["page"] = page
        captured.update(kwargs)
        return "token-from-service"

    monkeypatch.setattr(browser, "solve_recaptcha_v2", fake_solve)
    svc = browser.BrowserService()
    page = object()
    assert await svc.recaptcha_v2(page, sitekey="abc", attempts=2) == "token-from-service"
    assert captured["page"] is page
    assert captured["sitekey"] == "abc"
    assert captured["attempts"] == 2
