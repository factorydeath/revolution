from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import attack
import main
from attack import TemplateError
from yandex_captcha import YandexCaptcha


SITEKEY = "ysc1_test0123456789abcdef"


def _tpl(name: str, **kwargs) -> attack.Template:
    return attack.Template(name=name, country="RU", method="POST", url="https://e/", **kwargs)


def _manager(templates: list, error: BaseException | None = None):
    class Manager:
        def __init__(self) -> None:
            self.templates = templates

        def reload_templates(self, path=None, *, strict=False):
            if error is not None:
                raise error
            self.templates = templates
            return templates

        def reload_proxies(self, path=None):
            return 0

    return Manager()


def _cb():
    return SimpleNamespace(message=object(), from_user=SimpleNamespace(id=1))


def test_services_text_counts_captchas_and_chains():
    text = main.services_text(
        [
            _tpl("A_RU_SMS"),
            _tpl("B_RU_SMS", yandex_captcha=YandexCaptcha(SITEKEY, "e.example")),
            attack.Template(
                name="C_UA_SMS", country="UA", method="POST", url="https://e/", chain="kyivstar"
            ),
        ]
    )
    assert "Yandex SmartCaptcha" in text
    assert "B_RU_SMS" in text
    assert "kyivstar" not in text
    assert "3" in text


def test_services_kb_has_reload_button():
    data = [button.callback_data for row in main.services_kb().inline_keyboard for button in row]
    assert "admin_reload" in data
    assert "admin_open" in data


async def test_admin_open_clears_hidden_form_state(monkeypatch):
    state = SimpleNamespace(clear=AsyncMock())
    monkeypatch.setattr(main, "_check_cb", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "safe_cb_answer", AsyncMock())
    monkeypatch.setattr(main, "admin_panel_text", AsyncMock(return_value="panel"))
    monkeypatch.setattr(main, "show", AsyncMock())

    await main.admin_open(_cb(), state)

    state.clear.assert_awaited_once()


async def test_admin_reload_applies_new_list(monkeypatch):
    templates = [_tpl("New_RU_SMS", yandex_captcha=YandexCaptcha(SITEKEY, "e.example"))]
    shown = AsyncMock()
    monkeypatch.setattr(main, "manager", _manager(templates), raising=False)
    monkeypatch.setattr(main, "_check_cb", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "safe_cb_answer", AsyncMock())
    monkeypatch.setattr(main, "show", shown)

    await main.admin_reload(_cb())

    rendered = shown.await_args.args[1]
    assert "New_RU_SMS" in rendered
    assert "не применён" not in rendered


async def test_admin_reload_keeps_working_list_on_error(monkeypatch):
    templates = [_tpl("Old_RU_SMS")]
    shown = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(
        main, "manager", _manager(templates, error=TemplateError("битый JSON")), raising=False
    )
    monkeypatch.setattr(main, "_check_cb", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "safe_cb_answer", answer)
    monkeypatch.setattr(main, "show", shown)

    await main.admin_reload(_cb())

    rendered = shown.await_args.args[1]
    assert "не применён" in rendered
    assert "битый JSON" in rendered
    assert answer.await_args.kwargs["show_alert"] is True


async def test_cmd_reload_requires_admin(monkeypatch):
    checked = AsyncMock(return_value=False)
    reloaded = AsyncMock()
    monkeypatch.setattr(main, "_check_msg", checked)
    monkeypatch.setattr(main, "manager", SimpleNamespace(reload_templates=reloaded), raising=False)
    await main.cmd_reload(SimpleNamespace(chat=SimpleNamespace(id=1)))
    reloaded.assert_not_called()


async def test_cmd_reload_reports_broken_file(monkeypatch):
    say = AsyncMock()
    monkeypatch.setattr(main, "bot", object(), raising=False)
    monkeypatch.setattr(main, "_check_msg", AsyncMock(return_value=True))
    monkeypatch.setattr(
        main, "manager", _manager([_tpl("Old_RU_SMS")], error=TemplateError("дубли")), raising=False
    )
    monkeypatch.setattr(main, "say", say)
    await main.cmd_reload(SimpleNamespace(chat=SimpleNamespace(id=1)))
    assert "не применён" in say.await_args.args[2]


def test_services_text_counts_browser_services():
    text = main.services_text(
        [_tpl("A_RU_SMS"), attack.Template(name="B_UA_SMS", country="UA", method="POST", url="https://e/")],
        browser_services={"tbank": lambda: None, "silpo": lambda: None},
        browser_countries={"tbank": "RU", "silpo": "UA"},
    )
    assert "Всего" in text
    assert "4" in text
    assert "RU" in text
    assert "2 (1 + 1 вип)" in text
    assert "UA" in text
    assert "2 (1 + 1 вип)" in text
    assert "HTTP / API" in text
    assert "Вип сервисы (Browser)" in text
    assert "silpo, tbank" in text


def test_services_text_without_browser_services():
    text = main.services_text(
        [_tpl("A_RU_SMS")],
        browser_services={},
        browser_countries={},
    )
    assert "Всего" in text
    assert "1" in text
    assert "Вип сервисы (Browser)" not in text


async def test_admin_panel_text_shows_services(monkeypatch):
    mock_db = SimpleNamespace(dashboard_stats=AsyncMock(return_value=(10, 2, 0, 1, 5, 8)))
    monkeypatch.setattr(main, "db", mock_db)
    monkeypatch.setattr(main, "runtime", SimpleNamespace(total_active=lambda: 0))
    monkeypatch.setattr(main, "manager", SimpleNamespace(templates=[_tpl("A")]), raising=False)
    panel = await main.admin_panel_text()
    assert "Сервисы" in panel


async def test_attack_manager_browser_properties():
    mgr = attack.AttackManager([])
    assert isinstance(mgr.browser_services, dict)
    assert isinstance(mgr.browser_service_countries, dict)
    assert mgr.total_services_count == len(mgr.browser_services)
    h = mgr.health()
    assert "browser_services" in h
    assert "total_services" in h
    await mgr.session.close()


def test_vip_offer_text_counts_browser_services():
    text = main.vip_offer_text(
        browser_services={"tbank": lambda: None, "silpo": lambda: None},
        browser_countries={"tbank": "RU", "silpo": "UA"},
    )
    assert "Дополнительные сервисы" in text
    assert "+1 сервисов на RU и +1 на Украину" in text


def test_vip_offer_text_defaults_include_emulation_counts():
    text = main.vip_offer_text()
    assert "Дополнительные сервисы" in text
    b_ru, b_ua = main.browser_services_counts()
    assert f"+{b_ru} сервисов на RU и +{b_ua} на Украину" in text
    assert "100 звёзд" in text
    assert "2 USDT" in text


def test_vip_offer_text_without_browser_services():
    text = main.vip_offer_text(
        browser_services={},
        browser_countries={},
    )
    assert "Дополнительные сервисы" in text
    assert "Да" in text
