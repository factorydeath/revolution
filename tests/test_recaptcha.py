from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import attack
import recaptcha
from recaptcha import RecaptchaCache, RecaptchaError, RecaptchaV3


ANCHOR = (
    "https://www.google.com/recaptcha/api2/anchor?ar=1&k=site-key"
    "&co=aHR0cHM6Ly9leGFtcGxlLmNvbQ&size=invisible"
)


def test_recaptcha_template_accepts_anchor_and_replaces_token():
    template = attack.Template.from_raw(
        {
            "name": "Example_RU_SMS",
            "country": "RU",
            "url": "https://example.com/send",
            "body": '{"phone":"{{PLUS}}","recaptchaToken":"{{RECAPTCHA}}"}',
            "recaptcha_v3": {"anchor": ANCHOR, "ttl": 60},
        }
    )
    job = attack.build_job(template, attack.make_formatter("79161234567"))
    assert job.recaptcha_v3 == RecaptchaV3(ANCHOR, 60)
    assert "{{PLUS}}" not in job.body
    assert "{{RECAPTCHA}}" in job.body


def test_recaptcha_placeholder_requires_anchor():
    with pytest.raises(attack.TemplateError, match="неизвестные токены"):
        attack.Template.from_raw(
            {
                "name": "Example_RU_SMS",
                "country": "RU",
                "url": "https://example.com/send",
                "body": "{{RECAPTCHA}}",
            }
        )


async def test_recaptcha_cache_reuses_unexpired_token(monkeypatch):
    solve = AsyncMock(
        return_value=SimpleNamespace(is_solved=True, response_token="  first-token  ")
    )
    monkeypatch.setattr(recaptcha.recaptcha.v3, "asolve", solve)
    cache = RecaptchaCache(timeout=4)

    settings = RecaptchaV3.from_raw({"anchor": ANCHOR, "ttl": 60})
    assert await cache.token("example", settings, None) == "first-token"
    assert await cache.token("example", settings, None) == "first-token"
    solve.assert_awaited_once_with(anchor_url=ANCHOR, proxy=None, timeout=4)


async def test_recaptcha_cache_creates_fresh_token_by_default(monkeypatch):
    solve = Mock(
        side_effect=[
            SimpleNamespace(is_solved=True, response_token="first"),
            SimpleNamespace(is_solved=True, response_token="second"),
        ]
    )
    monkeypatch.setattr(recaptcha.recaptcha.v3, "solve", solve)
    cache = RecaptchaCache(timeout=4)
    settings = RecaptchaV3.from_raw(ANCHOR)

    assert await cache.token("example", settings, "http://proxy:8080") == "first"
    assert await cache.token("example", settings, "http://proxy:8080") == "second"
    assert solve.call_count == 2
    assert not cache._values


async def test_recaptcha_cache_uses_async_solver_without_proxy(monkeypatch):
    solve = AsyncMock(
        return_value=SimpleNamespace(is_solved=True, response_token="token")
    )
    monkeypatch.setattr(recaptcha.recaptcha.v3, "asolve", solve)
    cache = RecaptchaCache(timeout=4)

    assert await cache.token("example", RecaptchaV3.from_raw(ANCHOR), None) == "token"
    solve.assert_awaited_once_with(anchor_url=ANCHOR, proxy=None, timeout=4)


async def test_recaptcha_cache_rejects_empty_response(monkeypatch):
    solve = AsyncMock(return_value=SimpleNamespace(is_solved=False, response_token=""))
    monkeypatch.setattr(recaptcha.recaptcha.v3, "asolve", solve)
    cache = RecaptchaCache(timeout=4)
    with pytest.raises(RecaptchaError, match="пустой"):
        await cache.token("example", RecaptchaV3.from_raw(ANCHOR), None)


async def test_worker_injects_token_into_target_request():
    mock_session = SimpleNamespace(timeout=SimpleNamespace(sock_read=1.0))
    job = attack.build_job(
        attack.Template.from_raw(
            {
                "name": "Example_RU_SMS",
                "country": "RU",
                "url": "https://example.com/send",
                "headers": {"X-Captcha": "{{RECAPTCHA}}"},
                "body": '{"phone":"{{PLUS}}","token":"{{RECAPTCHA}}"}',
                "recaptcha_v3": ANCHOR,
            }
        ),
        attack.make_formatter("79161234567"),
    )
    attack_obj = attack.Attack(
        "79161234567", [job], 30, session=mock_session, workers=1
    )
    attack_obj._recaptcha.token = AsyncMock(return_value="captcha-token")
    attack_obj.deadline = attack.time.monotonic() + 30
    context = SimpleNamespace(
        status=200,
        headers={},
        content=SimpleNamespace(
            read=AsyncMock(return_value=b""),
        ),
        close=Mock(),
    )
    context.__aenter__ = AsyncMock(return_value=context)
    context.__aexit__ = AsyncMock(return_value=None)
    def fake_request(*args, **kwargs):
        attack_obj.stop(by_user=True)
        return context

    mock_session.request = Mock(side_effect=fake_request)

    await attack_obj._worker(job, 0)

    kwargs = mock_session.request.call_args.kwargs
    assert kwargs["data"] == b'{"phone":"+79161234567","token":"captcha-token"}'
    assert kwargs["headers"]["X-Captcha"] == "captcha-token"
    assert kwargs["url"] if "url" in kwargs else mock_session.request.call_args.args[1]
