from __future__ import annotations

import asyncio
import json

import pytest

import attack
from attack import AttackManager, TemplateError
from yandex_captcha import YandexCaptcha

SITEKEY = "ysc1_test0123456789abcdef"


def _write(tmp_path, entries):
    path = tmp_path / "services.json"
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return path


def _entry(name: str) -> dict:
    return {
        "name": name,
        "country": "RU",
        "method": "POST",
        "url": "https://example.com/send",
        "body": "phone={{RAW}}",
    }


async def test_reload_templates_swaps_list(tmp_path):
    path = _write(tmp_path, [_entry("One_RU_SMS")])
    manager = AttackManager([], proxy="", timeout=3, delay_ms=0, max_workers=1)
    try:
        assert manager.templates == []
        loaded = manager.reload_templates(path)
        assert [tpl.name for tpl in loaded] == ["One_RU_SMS"]
        assert [tpl.name for tpl in manager.templates] == ["One_RU_SMS"]

        path.write_text(
            json.dumps([_entry("Two_RU_SMS"), _entry("Three_RU_SMS")], indent=1),
            encoding="utf-8",
        )
        manager.reload_templates(path)
        assert [tpl.name for tpl in manager.templates] == ["Two_RU_SMS", "Three_RU_SMS"]
    finally:
        await manager.shutdown()


async def test_reload_keeps_old_list_on_broken_file(tmp_path):
    path = _write(tmp_path, [_entry("One_RU_SMS")])
    manager = AttackManager([], proxy="", timeout=3, delay_ms=0, max_workers=1)
    try:
        manager.reload_templates(path)
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(TemplateError):
            manager.reload_templates(path)
        assert [tpl.name for tpl in manager.templates] == ["One_RU_SMS"]
    finally:
        await manager.shutdown()


async def test_reload_rejects_duplicate_names(tmp_path):
    path = _write(tmp_path, [_entry("One_RU_SMS"), _entry("One_RU_SMS")])
    manager = AttackManager([], proxy="", timeout=3, delay_ms=0, max_workers=1)
    try:
        with pytest.raises(TemplateError, match="дубли"):
            manager.reload_templates(path)
        assert manager.templates == []
    finally:
        await manager.shutdown()


async def test_reloaded_yandex_template_is_fireable(tmp_path):
    entry = {
        **_entry("Cafe_RU_SMS"),
        "body": '{"phone":"{{RAW}}","captcha":"{{YANDEX}}"}',
        "yandex_captcha": {"sitekey": SITEKEY, "host": "example.com"},
    }
    manager = AttackManager([], proxy="", timeout=3, delay_ms=0, max_workers=1)
    try:
        manager.reload_templates(_write(tmp_path, [entry]))
        fireable = attack.fireable(manager.templates, "RU")
        assert len(fireable) == 1
        assert fireable[0].yandex_captcha == YandexCaptcha(SITEKEY, "example.com")
    finally:
        await manager.shutdown()


async def test_auto_check_and_reload_services_and_proxies(tmp_path):
    services_path = _write(tmp_path, [_entry("Initial_RU_SMS")])
    proxies_path = tmp_path / "proxies.txt"
    proxies_path.write_text("1.1.1.1:8080\n", encoding="utf-8")

    manager = AttackManager([], proxy="", timeout=3, delay_ms=0, max_workers=1)
    try:
        manager._services_path = services_path
        manager._proxies_path = proxies_path
        manager._services_mtime = services_path.stat().st_mtime_ns
        manager._proxies_mtime = proxies_path.stat().st_mtime_ns
        manager.reload_templates(services_path)
        manager.reload_proxies(proxies_path)

        assert len(manager.templates) == 1
        assert len(manager.pool) == 1

        import time
        time.sleep(0.01)
        _write(tmp_path, [_entry("Initial_RU_SMS"), _entry("Added_RU_SMS")])
        proxies_path.write_text("1.1.1.1:8080\n2.2.2.2:8080\n", encoding="utf-8")

        s_changed, p_changed = manager.check_and_reload()
        assert s_changed is True
        assert p_changed is True
        assert len(manager.templates) == 2
        assert len(manager.pool) == 2
        assert {p.label for p in manager.pool._entries} == {"1.1.1.1:8080", "2.2.2.2:8080"}

        s_changed2, p_changed2 = manager.check_and_reload()
        assert s_changed2 is False
        assert p_changed2 is False
    finally:
        await manager.shutdown()
