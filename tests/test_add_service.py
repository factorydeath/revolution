from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("add_service", ROOT / "tools" / "add_service.py")
add_service = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(add_service)

SITEKEY = "ysc1_test0123456789abcdef"


def _ns(**overrides) -> argparse.Namespace:
    base = dict(
        name="Cafe_RU_SMS",
        country="RU",
        method="POST",
        url="https://cafe.example/api/send",
        header=[],
        body='{"phone":"{{RAW}}","captcha":"{{YANDEX}}"}',
        content_type="application/json",
        api_ok="",
        api_error="",
        chain="",
        page="",
        sitekey="",
        host="",
        href="",
        yandex_ttl=0.0,
        yandex_cipher="",
        probe=False,
        proxy="",
        timeout=1.0,
        services="services.json",
        replace=False,
        write=False,
        dry_run=True,
        no_backup=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _settings(**overrides):
    values = dict(sitekey=SITEKEY, host="cafe.example", href="https://cafe.example/login")
    values.update(overrides)
    return add_service.YandexCaptcha(**values)


def test_parse_headers_keeps_order_and_values():
    headers = add_service.parse_headers(["Origin: https://a.example", "X-Trace: 1:2"])
    assert list(headers) == ["Origin", "X-Trace"]
    assert headers["X-Trace"] == "1:2"


@pytest.mark.parametrize("bad", ["Origin", "", " : value"])
def test_parse_headers_rejects_malformed(bad):
    with pytest.raises(add_service.AddError):
        add_service.parse_headers([bad])


def test_build_entry_keeps_file_field_order():
    entry = add_service.build_entry(_ns(), _settings())
    assert list(entry) == [
        "name",
        "country",
        "method",
        "url",
        "headers",
        "body",
        "content_type",
        "needs_prepare",
        "has_phone",
        "yandex_captcha",
    ]
    assert entry["yandex_captcha"] == {
        "sitekey": SITEKEY,
        "host": "cafe.example",
        "href": "https://cafe.example/login",
    }


def test_build_entry_is_a_valid_bot_template():
    entry = add_service.build_entry(_ns(), _settings())
    template = add_service.Template.from_raw(entry)
    assert template.yandex_captcha is not None
    assert template.yandex_captcha.sitekey == SITEKEY


def test_build_entry_omits_empty_optional_fields():
    entry = add_service.build_entry(
        _ns(header=["Origin: https://a"], body="phone={{RAW}}"), None
    )
    assert "yandex_captcha" not in entry
    assert "chain" not in entry
    assert "api_ok" not in entry


def test_yandex_settings_require_a_placeholder():
    entry = add_service.build_entry(_ns(body='{"phone":"{{RAW}}"}'), _settings())
    with pytest.raises(add_service.AddError, match="плейсхолдер"):
        add_service.warn_unused_token(entry)


def test_placeholder_requires_settings():
    entry = add_service.build_entry(_ns(), None)
    with pytest.raises(add_service.AddError, match="--page"):
        add_service.require_captcha_config(entry)


def test_chain_with_captcha_is_rejected_before_writing(tmp_path):
    args = _ns(
        chain="kyivstar",
        sitekey=SITEKEY,
        host="cafe.example",
        services=str(tmp_path / "services.json"),
    )
    with pytest.raises(add_service.AddError, match="chain"):
        asyncio.run(add_service.run(args))
    assert not args.services or not pathlib.Path(args.services).exists()


def test_merge_appends_new_service():
    entries = [{"name": "A_RU_SMS"}]
    merged, replaced = add_service.merge_entry(entries, {"name": "B_RU_SMS"}, replace=False)
    assert [item["name"] for item in merged] == ["A_RU_SMS", "B_RU_SMS"]
    assert replaced is False
    assert [item["name"] for item in entries] == ["A_RU_SMS"]


def test_merge_requires_replace_for_existing_name():
    with pytest.raises(add_service.AddError, match="--replace"):
        add_service.merge_entry([{"name": "A_RU_SMS"}], {"name": "A_RU_SMS"}, replace=False)


def test_merge_replaces_in_place():
    entries = [{"name": "A_RU_SMS", "url": "old"}, {"name": "B_RU_SMS"}]
    merged, replaced = add_service.merge_entry(
        entries, {"name": "A_RU_SMS", "url": "new"}, replace=True
    )
    assert replaced is True
    assert merged[0]["url"] == "new"
    assert [item["name"] for item in merged] == ["A_RU_SMS", "B_RU_SMS"]


def test_write_services_is_atomic_and_keeps_format(tmp_path):
    path = tmp_path / "services.json"
    original = [{"name": "A_RU_SMS"}]
    path.write_text(json.dumps(original, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    backup = add_service.write_services(path, original + [{"name": "B_RU_SMS"}], backup=True)

    assert backup is not None and backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    text = path.read_text(encoding="utf-8")
    assert json.loads(text)[-1]["name"] == "B_RU_SMS"
    assert text.splitlines()[1] == " {"
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_services_roundtrips_project_format(tmp_path):
    path = tmp_path / "services.json"
    entries = [{"name": "A_RU_SMS", "headers": {"Origin": "https://a"}}]
    expected = json.dumps(entries, ensure_ascii=False, indent=1) + "\n"
    add_service.write_services(path, entries, backup=False)
    assert path.read_text(encoding="utf-8") == expected
