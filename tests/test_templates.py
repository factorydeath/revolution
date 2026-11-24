from __future__ import annotations

import json

import pytest

import attack as tpl_mod
from attack import (
    PREPARE_TOKENS,
    Template,
    TemplateError,
    build_jobs,
    fireable,
    known_tokens,
    load_templates,
    make_formatter,
    parse_templates,
    unresolved_tokens,
)

@pytest.fixture(scope="module")
def templates():
    return load_templates(strict=False)

def test_dataset_loads(templates):
    assert len(templates) >= 50
    assert {t.country for t in templates} == {"RU", "UA"}
    assert {t.method for t in templates} <= {"GET", "POST"}

def test_fireable_counts(templates):
    assert len(fireable(templates, "RU")) == sum(
        1
        for t in templates
        if t.country == "RU"
        and (t.has_phone or t.prepare)
        and not (t.needs_prepare and not t.prepare)
    )
    assert len(fireable(templates, "UA")) >= 15
    assert fireable(templates, "KZ") == []

def test_every_token_is_known(templates):
    for template in templates:
        blob = "\x00".join(
            [template.url, template.body]
            + [f"{k}\x00{v}" for k, v in template.headers.items()]
        )
        allowed = known_tokens() | tpl_mod.DYNAMIC_TOKENS | (
            PREPARE_TOKENS if template.prepare else set()
        )
        if template.recaptcha_v3 and not template.chain:
            allowed.add("RECAPTCHA")
        if template.yandex_captcha:
            allowed.update(tpl_mod.YANDEX_TOKENS)
        if template.csrf:
            allowed.add("CSRF")
        assert not (unresolved_tokens(blob) - allowed), template.name

@pytest.mark.parametrize(
    "digits,country",
    [("79161234567", "RU"), ("380671234567", "UA")],
)
def test_build_jobs_resolves_all_tokens(templates, digits, country):
    prepare_tokens = {name: "test" for name in PREPARE_TOKENS}
    jobs, broken = build_jobs(
        fireable(templates, country),
        make_formatter(digits, prepare_tokens),
    )
    assert jobs, "нет ни одного готового запроса"
    assert broken == []
    dynamic = set(tpl_mod.DYNAMIC_TOKENS)
    for job in jobs:
        leftover = unresolved_tokens(job.url) | unresolved_tokens(job.body)
        for _, value in job.headers:
            leftover |= unresolved_tokens(value)
        if job.csrf is not None:
            leftover.discard("CSRF")
        if job.recaptcha_v3 is not None:
            leftover.discard("RECAPTCHA")
        if job.yandex_captcha is not None:
            leftover -= tpl_mod.YANDEX_TOKENS
        assert not (leftover - dynamic), job.name

def test_build_job_rotates_user_agent_out(templates):
    template = next(t for t in templates if t.headers)
    job = tpl_mod.build_job(template, make_formatter("79161234567"))
    assert all(key.lower() != "user-agent" for key, _ in job.headers)

def test_invalid_record_rejected():
    with pytest.raises(TemplateError):
        Template.from_raw({"name": "x", "country": "RU", "method": "POST", "url": "NaN"})
    with pytest.raises(TemplateError):
        Template.from_raw({"name": "x", "country": "ZZ", "method": "POST", "url": "http://a"})
    with pytest.raises(TemplateError):
        Template.from_raw(
            {"name": "x", "country": "RU", "method": "POST", "url": "http://a", "body": "{{NOPE}}"}
        )

def test_prepare_template_requires_known_provider():
    base = {"name": "x", "country": "RU", "method": "POST", "url": "https://a"}
    with pytest.raises(TemplateError):
        Template.from_raw({**base, "needs_prepare": True})
    with pytest.raises(TemplateError):
        Template.from_raw({**base, "prepare": "nope"})
    with pytest.raises(TemplateError):
        Template.from_raw({**base, "body": "{{CHALLENGE}}"})

    template = Template.from_raw(
        {**base, "needs_prepare": True, "prepare": "beeline", "body": "{{CHALLENGE}}"}
    )
    assert fireable([template], "RU") == [template]
    job = tpl_mod.build_job(template, make_formatter("79161234567", {"CHALLENGE": "abc"}))
    assert job.body == "abc"

def test_api_error_marker_reaches_job():
    template = Template.from_raw(
        {
            "name": "x",
            "country": "RU",
            "method": "POST",
            "url": "https://a",
            "api_error": '"code":"X"',
        }
    )
    job = tpl_mod.build_job(template, make_formatter("79161234567"))
    assert job.api_error == '"code":"X"'
    assert job.api_ok == ""

def test_chain_reaches_job():
    template = Template.from_raw(
        {
            "name": "chain",
            "country": "RU",
            "method": "POST",
            "url": "https://id.raiffeisen.ru/api/raif-id/otp/sms/send",
            "chain": "raiffeisen",
        }
    )
    assert fireable([template], "RU") == [template]
    job = tpl_mod.build_job(template, make_formatter("79161234567"))
    assert job.chain == "raiffeisen"

def test_greenmoney_chain_template_reaches_job():
    template = Template.from_raw(
        {
            "name": "GreenMoney_RU_SMS",
            "country": "RU",
            "method": "POST",
            "url": "https://api.greenmoney.ru/api/Account/RegistrationByPhone/Confirm",
            "chain": "greenmoney",
            "api_ok": "RepeatSend",
        }
    )
    assert fireable([template], "RU") == [template]
    job = tpl_mod.build_job(template, make_formatter("79161234567"))
    assert job.chain == "greenmoney"
    assert job.api_ok == "RepeatSend"

def test_invalid_chain_rejected():
    base = {"name": "x", "country": "RU", "method": "POST", "url": "https://a"}
    with pytest.raises(TemplateError):
        Template.from_raw({**base, "chain": "nope"})
    with pytest.raises(TemplateError):
        Template.from_raw({**base, "chain": "raiffeisen", "prepare": "beeline"})
    with pytest.raises(TemplateError):
        Template.from_raw({**base, "chain": "raiffeisen", "has_phone": False})

def test_broken_records_are_skipped_not_fatal():
    raw = [
        {"name": "good", "country": "RU", "method": "GET", "url": "http://a/{{PLUS}}"},
        {"name": "bad", "country": "RU", "method": "GET", "url": None},
    ]
    parsed = parse_templates(raw, strict=False)
    assert [t.name for t in parsed] == ["good"]

def test_duplicates_rejected():
    raw = [
        {"name": "dup", "country": "RU", "method": "GET", "url": "http://a/{{PLUS}}"},
        {"name": "dup", "country": "RU", "method": "GET", "url": "http://b/{{PLUS}}"},
    ]
    with pytest.raises(TemplateError):
        parse_templates(raw, strict=True)

def test_load_missing_file():
    with pytest.raises(TemplateError):
        load_templates("/nonexistent/services.json", strict=True)

def test_services_json_is_valid_json():
    raw = json.loads(tpl_mod.DEFAULT_SERVICES_PATH.read_text(encoding="utf-8"))
    assert (
        isinstance(raw, list)
        or (isinstance(raw, dict) and "free" in raw and "vip" in raw)
    ) and raw

def test_free_and_vip_sections_separation(templates):
    raw = json.loads(tpl_mod.DEFAULT_SERVICES_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and "free" in raw and "vip" in raw
    free_names = {item["name"] for item in raw["free"]}
    vip_names = {item["name"] for item in raw["vip"]}
    assert not (free_names & vip_names), "Имена сервисов не должны пересекаться в free и vip"
    free_ru = fireable(templates, "RU", is_vip=False)
    free_ua = fireable(templates, "UA", is_vip=False)
    assert len(free_ru) == 30
    assert len(free_ua) == 30
    assert all(t.name in free_names for t in free_ru)
    assert all(t.name in free_names for t in free_ua)
    all_ru = fireable(templates, "RU", is_vip=True)
    all_ua = fireable(templates, "UA", is_vip=True)
    assert len(all_ru) == 55
    assert len(all_ua) == 66

def test_new_hars_services_present(templates):
    names = {t.name for t in templates}
    expected = {"Ktc_UA_SMS", "Helsi_UA_SMS", "DniproM_UA_SMS", "Pidbir_UA_SMS", "Mafia_UA_SMS"}
    assert expected.issubset(names)
