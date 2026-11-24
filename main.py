from __future__ import annotations

import asyncio
import datetime
import importlib
import json
import logging
import logging.handlers
import math
import os
import re
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Awaitable, Callable

import phonenumbers
from phonenumbers import NumberParseException

import config
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ErrorEvent,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    TelegramObject,
    Update,
)
from ui import (
    DANGER,
    EFFECT_HEART,
    PRIMARY,
    SUCCESS,
    btn,
    bullets,
    card,
    e,
    esc,
    h,
    fields,
    quote,
    sheet,
    rich,
    say,
    show,
    table,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

from attack import Attack, AttackManager, Busy, EngineError, TemplateError, load_templates
from db import Database
from proxies import load_proxies
from xrocket import XRocketClient, XRocketError

logger = logging.getLogger("revolution")

db: Database | None = None
bot: Bot | None = None
manager: AttackManager | None = None
runtime: "AttackRuntime | None" = None
xrocket_client: XRocketClient = XRocketClient(config.xrocket_api_key, config.xrocket_api_url)

PHONE_REGIONS = ("RU", "UA")

def normalize_phone(raw: str) -> str | None:
    for region in PHONE_REGIONS:
        try:
            parsed = phonenumbers.parse((raw or "").strip(), region)
        except NumberParseException:
            continue
        if (
            phonenumbers.is_valid_number(parsed)
            and phonenumbers.region_code_for_number(parsed) in PHONE_REGIONS
        ):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            ).lstrip("+")
    return None

def display_phone(phone: str) -> str:
    try:
        parsed = phonenumbers.parse("+" + phone, None)
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except NumberParseException:
        return "+" + phone

def phone_country(phone: str) -> str:
    return "UA" if phone.startswith("380") else "RU"

def is_fake_number(phone: str) -> bool:
    national = phone[3:] if phone.startswith("380") else phone[1:]
    if not national:
        return True
    if len(set(national)) == 1:
        return True
    if re.search(r"([^9])\1{5,}", national):
        return True
    if re.search(r"(.)\1{6,}", national):
        return True
    asc = "".join(str(i % 10) for i in range(len(national)))
    asc2 = "".join(str((i + 1) % 10) for i in range(len(national)))
    if national in (asc, asc[::-1], asc2, asc2[::-1]):
        return True
    if national in ("1234567890", "0987654321", "123456789", "987654321"):
        return True
    if re.fullmatch(r"(\d{2,3})\1+", national) and len(national) >= 6:
        return True
    return False

def split_phone_candidates(raw_text: str) -> list[str]:
    groups = re.split(r"[\n,;]+", raw_text or "")
    candidates: list[str] = []

    for group in groups:
        buffer = ""
        for token in group.split():
            if not token:
                continue
            if token.startswith("+") and buffer:
                candidates.append(buffer)
                buffer = token
            elif buffer and len(re.sub(r"\D", "", buffer)) >= 11:
                candidates.append(buffer)
                buffer = token
            else:
                buffer = f"{buffer} {token}".strip() if buffer else token
        if buffer:
            candidates.append(buffer)
    return [item for item in candidates if item]

phone_hint = (
    "<b>Форматы целевых номеров:</b>\n"
    "▫️ Россия: <code>+79163845120</code> или <code>89163845120</code>\n"
    "▫️ Украина: <code>+380673920382</code>\n"
    "<i>Знаки +, пробелы, дефисы и скобки нормализуются автоматически</i>"
)

try:
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
except Exception:
    MSK = datetime.timezone(datetime.timedelta(hours=3))

def fmt_dur(sec: int) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return str(sec)
    if sec >= 3600:
        hours, rest = divmod(sec, 3600)
        minutes = rest // 60
        return f"{hours} ч" + (f" {minutes} мин" if minutes else "")
    if sec >= 60:
        minutes, seconds = divmod(sec, 60)
        return f"{minutes} мин" + (f" {seconds} сек" if seconds else "")
    return f"{sec} сек"

def format_dt(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        clean_ts = ts.strip().replace("Z", "+00:00")
        moment = datetime.datetime.fromisoformat(clean_ts)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")
    except Exception:
        return esc(ts)

def ban_text() -> str:
    return card(
        f"{e('ban')} Доступ заблокирован",
        "Ваш аккаунт заблокирован администратором. Запуск атак и доступ к функциям платформы ограничены.\n\n"
        "Если вы считаете, что блокировка ошибочна, свяжитесь с поддержкой.",
        footer=f"{e('info')} Статус: доступ закрыт",
    )

def menu_caption(first_name: str | None = None) -> str:
    name = esc(first_name) if first_name else "друг"
    return (
        h(f"{e('sword')} Revolution — Стресс-тестирование")
        + quote(
            f"{e('spark')} Привет, {name}! Бюджетный сервис для стресс-тестирования телефонных номеров.\n"
            "Удобный запуск, стабильная работа пула и честные доступные тарифы.\n"
            f"Направления: {e('ru')} Россия (+7) и {e('ua')} Украина (+380)"
        )
        + table(
            ["Параметр", "Базовый тариф (Free)"],
            [
                ["Регионы", "Россия (+7) и Украина (+380)"],
                ["Длительность", f"от 10 сек до {fmt_dur(config.default_max_duration)} ({config.default_max_duration}с)"],
                ["Параллельно", f"{config.default_max_active} активная сессия"],
                ["Суточный лимит", f"{config.default_daily_limit} запусков в день"],
            ],
        )
        + f"\n<footer>{e('crown')} Доступный VIP: до {fmt_dur(config.vip_max_duration)}, "
        f"{config.vip_max_active} слотов, {config.vip_daily_limit} запусков/сутки, "
        "скорость х2 и защита личного номера</footer>"
    )

def terms_text() -> str:
    return (
        h(f"{e('doc')} Правила использования сервиса")
        + quote("Перед началом работы подтвердите согласие с условиями использования сервиса.")
        + bullets(
            [
                "Сервис является бюджетным инструментом для проверки устойчивости телефонных номеров и телеком-инфраструктуры к входящему потоку SMS и звонков.",
                "Мы делаем упор на стабильность отправки по пулу сервисов, удобство и доступную стоимость без скрытых переплат.",
                "Целевой номер вы указываете самостоятельно и несёте полную ответственность за правомерность тестирования.",
                "Честная прозрачность: сторонние шлюзы и операторы могут фильтровать трафик, поэтому гарантируется стабильность нашей отправки, а не 100% приём каждым оператором.",
            ]
        )
        + f"\n<footer>{e('info')} Нажмите «Принять условия» для перехода в главное меню</footer>"
    )

def browser_services_counts(
    browser_services: dict[str, Any] | None = None,
    browser_countries: dict[str, str] | None = None,
) -> tuple[int, int]:
    if browser_services is None:
        try:
            import browser
            browser_services = getattr(browser, "SERVICES", {})
            browser_countries = getattr(browser, "SERVICE_COUNTRIES", {})
        except Exception:
            browser_services = {}
            browser_countries = {}
    elif browser_countries is None:
        try:
            import browser
            browser_countries = getattr(browser, "SERVICE_COUNTRIES", {})
        except Exception:
            browser_countries = {}

    ru_count = sum(
        1 for name, fn in browser_services.items()
        if (browser_countries.get(name) or getattr(fn, "country", "RU")) == "RU"
    )
    ua_count = sum(
        1 for name, fn in browser_services.items()
        if (browser_countries.get(name) or getattr(fn, "country", "")) == "UA"
    )
    return ru_count, ua_count

def vip_offer_text(
    browser_services: dict[str, Any] | None = None,
    browser_countries: dict[str, str] | None = None,
    templates: list | None = None,
) -> str:
    b_ru, b_ua = browser_services_counts(browser_services, browser_countries)
    if templates is None:
        try:
            from attack import load_templates
            templates = load_templates(strict=False)
        except Exception:
            templates = []

    tpl_ru = sum(1 for tpl in templates if getattr(tpl, "country", "") == "RU")
    tpl_ua = sum(1 for tpl in templates if getattr(tpl, "country", "") == "UA")

    total_ua_vip = (tpl_ua or 63) + b_ua
    total_ru_vip = (tpl_ru or 50) + b_ru
    free_ua = min(config.free_request_services_limit, tpl_ua or 30)
    free_ru = min(config.free_request_services_limit, tpl_ru or 30)
    locked_ua = total_ua_vip - free_ua
    locked_ru = total_ru_vip - free_ru

    if b_ru and b_ua:
        new_services_vip = f"+{b_ru} сервисов на RU и +{b_ua} на Украину"
    elif b_ru:
        new_services_vip = f"+{b_ru} сервисов на RU"
    elif b_ua:
        new_services_vip = f"+{b_ua} сервисов на Украину"
    else:
        new_services_vip = "Да"

    return (
        h(f"{e('crown')} VIP на 30 дней")
        + quote(
            f"Доступный апгрейд для регулярных проверок: на бесплатном тарифе доступно {free_ua} сервисов. "
            f"С подпиской VIP открывается полный пул из {total_ua_vip} сервисов на UA и {total_ru_vip} на RU, "
            f"браузерная эмуляция сложных шлюзов, удвоенная скорость отправки и расширенные лимиты."
        )
        + table(
            ["Параметр", "Сейчас (Free)", "С VIP"],
            [
                [
                    f"{e('bolt')} Одновременно",
                    f"{config.default_max_active} слот",
                    f"{config.vip_max_active} слотов",
                ],
                [
                    f"{e('clock')} Одна атака",
                    f"до {fmt_dur(config.default_max_duration)}",
                    f"до {fmt_dur(config.vip_max_duration)}",
                ],
                [
                    f"{e('calendar')} В сутки",
                    f"{config.default_daily_limit}",
                    f"{config.vip_daily_limit}",
                ],
                [
                    f"{e('fire')} Скорость",
                    "Базовая",
                    "В 2 раза выше (х2)",
                ],
                [
                    f"{e('spark')} Сервисы UA",
                    f"{free_ua} сервисов",
                    f"Все {total_ua_vip} (+{locked_ua})",
                ],
                [
                    f"{e('spark')} Сервисы RU",
                    f"{free_ru} сервисов",
                    f"Все {total_ru_vip} (+{locked_ru})",
                ],
                [f"{e('spark')} Дополнительные сервисы", "Нет", new_services_vip],
                [f"{e('shield')} Защита номера", "Нет", "Да"],
            ],
        )
        + f"\n<footer>{e('star')} {config.vip_price} звёзд или {config.xrocket_vip_price_usdt} USDT / {config.xrocket_vip_price_gram} GRAM в месяц</footer>"
    )

def attack_started_text(
    phone: str,
    duration: int,
    end_time: str,
    speed: str,
    services_count: int | None = None,
    is_vip: bool = False,
) -> str:
    rows = [
        ["Номер цели", f"<code>{esc(phone)}</code>"],
        ["Длительность", f"{fmt_dur(duration)} ({duration} сек)"],
        ["Завершение", esc(end_time)],
        ["Тарифный план", esc(speed)],
    ]
    if services_count is not None and services_count > 0:
        if is_vip:
            srv_label = f"🔥 {services_count} (максимум + вип сервисы)"
        else:
            srv_label = f"{services_count} (Базовый пакет)"
        rows.append(["Сервисов в работе", srv_label])
    footer = (
        f"\n<footer>{e('crown')} Доступно 30 сервисов. Разблокировать все 67+ сервисов и вип сервисы — в тарифе VIP</footer>"
        if not is_vip
        else f"\n<footer>{e('spark')} Задействован максимальный пул сервисов и вип сервисы браузера</footer>"
    )
    return (
        h(f"{e('bolt')} Поехали — стресс-тест запущен")
        + quote("Сессия активна: идёт стабильная отправка запросов. Вы можете остановить её досрочно кнопкой ниже.")
        + table(["Параметр", "Значение"], rows)
        + footer
    )

def attack_finished_text(phone: str, duration: int) -> str:
    rows = [
        ["Номер цели", f"<code>{esc(phone)}</code>"],
        ["Время работы", f"{fmt_dur(duration)} ({duration} сек)"],
    ]
    return (
        h(f"{e('ok')} Сессия завершена")
        + quote("Таймер сессии истёк или стресс-тест был остановлен вручную. Отправка запросов прекращена.")
        + table(["Параметр", "Значение"], rows)
        + f"\n<footer>{e('clock')} Завершено в {datetime.datetime.now(MSK).strftime('%H:%M:%S МСК')}</footer>"
    )

def speed_tag(has_privilege: bool, max_duration: int) -> str:
    if max_duration not in (config.default_max_duration, config.vip_max_duration):
        return "свои лимиты"
    return "VIP (х2 скорость)" if has_privilege else "Базовый"

def history_line(index: int, phone: str, duration: int, timestamp: str, stopped: int) -> str:
    flag = e("ua") if phone_country(phone) == "UA" else e("ru")
    try:
        moment = datetime.datetime.fromisoformat(timestamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        short_dt = moment.astimezone(MSK).strftime("%d.%m %H:%M")
    except Exception:
        short_dt = esc((timestamp or "")[:16])
    mark = "Остановлена руками" if stopped else "Дошла до конца"
    return sheet(
        [
            (f"{index}. Номер", f"{flag} <code>{esc(phone)}</code>"),
            ("Сколько", fmt_dur(duration)),
            ("Когда", short_dt),
            ("Итог", mark),
        ]
    )

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn("В меню", "menu", icon_name="back")]]
    )

def vip_activated_kb(invite_link: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if invite_link:
        rows.append([btn("Приватный VIP-форум", url=invite_link, icon_name="vip")])
    rows.append([btn("В меню", "menu", icon_name="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def vip_success_card(
    invite_link: str | None = None,
    title: str | None = None,
) -> str:
    rows = [
        ["Статус", "VIP (30 дней)"],
        ["Сервисы", "Все (UA / RU) + Browser"],
        ["Одновременно", f"до {config.vip_max_active} слотов"],
        ["Длительность", f"до {fmt_dur(config.vip_max_duration)} ({config.vip_max_duration} сек)"],
        ["В сутки", f"{config.vip_daily_limit} запусков"],
        ["Скорость", "Увеличена в 2 раза (х2)"],
        ["Защита номера", "Бесплатно в разделе «Защита»"],
    ]
    community_html = ""
    if invite_link:
        community_html = (
            f"\n\n{e('spark')} <b>Приватное VIP-сообщество:</b>\n"
            f"Ваша индивидуальная ссылка в закрытый форум с новостями и обновлениями:\n"
            f"👉 <a href=\"{invite_link}\">{invite_link}</a>\n"
            f"<i>(Ссылка одноразовая и выключается сразу после входа)</i>"
        )
    return (
        h(title or f"{e('crown')} VIP включён")
        + quote("Оплата успешно получена. Все ограничения сняты на 30 дней: расширенные слоты и стабильная скорость.")
        + table(["Параметр", "Значение"], rows)
        + community_html
        + f"\n<footer>{e('spark')} VIP на 30 дней активирован</footer>"
    )

def terms_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn("Принять условия", "accept", icon_name="accept", style=SUCCESS)]]
    )

def sub_card() -> str:
    return card(
        f"{e('bell')} Обязательная подписка",
        "Для использования сервиса стресс-тестирования необходимо подтвердить подписку на официальный канал платформы.\n\n"
        f"В канале публикуются новости, обновления сервисов и важные объявления: <b>{config.channel_id}</b>\n\n"
        "После подписки нажмите кнопку «Проверить подписку» ниже.",
        footer=f"{e('info')} Доступ ко всем функциям откроется сразу после проверки",
    )

def sub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Подписаться на канал", url=config.channel_url, icon_name="channel")],
            [btn("Проверить подписку", "check_sub", icon_name="accept", style=SUCCESS)],
        ]
    )

def _build_main_kb(has_vip: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn("Запустить стресс-тест", "order", icon_name="order", style=PRIMARY)],
        [
            btn("Профиль", "profile", icon_name="profile"),
            btn("Защита", "protect", icon_name="protect"),
        ],
        [
            btn("История", "history", icon_name="history"),
            btn("Канал сервиса", url=config.channel_url, icon_name="channel"),
        ],
    ]
    if not has_vip:
        rows.append([btn("Тариф VIP", "buy_privilege", icon_name="vip", style=PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def main_kb(user_id: int) -> InlineKeyboardMarkup:
    try:
        has_vip = await db.check_user_exists_privilege(user_id)
    except Exception:
        has_vip = False
    return _build_main_kb(has_vip)

def protect_kb(is_vip: bool, has_protected: bool) -> InlineKeyboardMarkup:
    if not is_vip:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    btn(
                        f"VIP · {config.vip_price} звёзд / {config.xrocket_vip_price_usdt}$",
                        "buy_privilege",
                        icon_name="vip",
                        style=PRIMARY,
                    )
                ],
                [btn("В меню", "menu", icon_name="back")],
            ]
        )
    action = (
        btn("Снять защиту", "protect_remove", icon_name="remove", style=DANGER)
        if has_protected
        else btn("Добавить номер", "protect_add", icon_name="add", style=PRIMARY)
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[[action], [btn("В меню", "menu", icon_name="back")]]
    )

def admin_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            btn("Юзеры", "admin_users:1", icon_name="users"),
            btn("VIP", "admin_vip:1", icon_name="vip"),
        ],
        [
            btn("Выдать VIP", "admin_action:grant", icon_name="grant", style=PRIMARY),
            btn("Забрать VIP", "admin_action:revoke", icon_name="revoke"),
        ],
        [
            btn("Бан", "admin_action:ban", icon_name="ban", style=DANGER),
            btn("Разбан", "admin_action:unban", icon_name="unban"),
        ],
        [btn("Статистика", "admin_stats", icon_name="stats")],
        [btn("Сервисы", "admin_services", icon_name="doc")],
        [btn("Поиск", "admin_search", icon_name="search")],
        [btn("Рассылка", "admin_broadcast", icon_name="broadcast")],
        [btn("Закрыть", "admin_close", icon_name="close", style=DANGER)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_user_kb(user_id: int, has_vip: bool, is_banned: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                "Забрать VIP" if has_vip else "Выдать VIP",
                f"admin_toggle_vip:{user_id}",
                icon_name="revoke" if has_vip else "grant",
                style=DANGER if has_vip else PRIMARY,
            )
        ],
        [
            btn("Лимиты", f"admin_limits:{user_id}", icon_name="limits"),
            btn("Сбросить", f"admin_limits_off:{user_id}", icon_name="reset"),
        ],
        [
            btn(
                "Разбанить" if is_banned else "Забанить",
                f"admin_toggle_ban:{user_id}",
                icon_name="unban" if is_banned else "ban",
                style=PRIMARY if is_banned else DANGER,
            )
        ],
        [btn("В панель", "admin_open", icon_name="panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn("В панель", "admin_open", icon_name="panel")]]
    )

def stop_kb(attack_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Остановить атаку", f"stop:{attack_id}", icon_name="stop", style=DANGER)]
        ]
    )

def payment_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn(
                    f"Оплатить {config.vip_price} звёзд",
                    icon_name="pay",
                    style=PRIMARY,
                    pay=True,
                )
            ]
        ]
    )

def vip_methods_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            btn(
                f"Telegram Stars · {config.vip_price} звёзд",
                "pay_stars",
                icon_name="pay",
                style=PRIMARY,
            )
        ]
    ]
    if config.xrocket_enabled:
        rows.append(
            [
                btn(
                    "xRocket · USDT / GRAM (TON, TRON)",
                    "pay_xrocket_menu",
                    icon_name="star",
                    style=SUCCESS,
                )
            ]
        )
    rows.append([btn("Назад в меню", "menu", icon_name="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def xrocket_price(currency: str) -> float | int:
    return config.xrocket_vip_prices.get(currency.upper().strip(), config.xrocket_vip_price_usdt)

def xrocket_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn(
                    f"USDT ({config.xrocket_vip_price_usdt} USDT) · TON / TRON",
                    "xrocket_create:USDT",
                    icon_name="coin",
                    style=PRIMARY,
                )
            ],
            [
                btn(
                    f"GRAM ({config.xrocket_vip_price_gram} GRAM) · TON",
                    "xrocket_create:GRAM",
                    icon_name="gem",
                    style=PRIMARY,
                )
            ],
            [btn("Назад к способам оплаты", "buy_privilege", icon_name="back")],
        ]
    )

def xrocket_invoice_kb(pay_url: str, invoice_id: str) -> InlineKeyboardMarkup:
    effective_url = (pay_url or "").strip() or f"https://t.me/xrocket?start=inv_{invoice_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Оплатить в xRocket", url=effective_url, icon_name="pay")],
            [btn("Проверить транзакцию", f"xrocket_check:{invoice_id}", icon_name="accept", style=SUCCESS)],
            [btn("Выбрать другую валюту", "pay_xrocket_menu", icon_name="back")],
        ]
    )

_TRANSIENT_MARKERS = (
    "query is too old",
    "query id is invalid",
    "response timeout expired",
    "request timeout error",
    "message is not modified",
    "message to edit not found",
    "message to delete not found",
    "bot was blocked by the user",
    "user is deactivated",
    "retry after",
    "too many requests",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
)

def _is_benign(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "message is not modified" in text
        or "message to edit not found" in text
        or "query is too old" in text
        or "response timeout expired" in text
    )

def is_transient_update_error(exc: BaseException) -> bool:
    try:
        text = f"{type(exc).__name__}: {exc}".lower()
    except Exception:
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)

async def _retry_after_delay(exc: TelegramRetryAfter) -> None:
    try:
        await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 1)
    except Exception:
        pass

async def safe_answer(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    try:
        return await message.answer(text, reply_markup=reply_markup)
    except TelegramRetryAfter as exc:
        await _retry_after_delay(exc)
        try:
            return await message.answer(text, reply_markup=reply_markup)
        except Exception:
            return None
    except Exception:
        return None

async def safe_cb_answer(
    cb: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    for attempt in (1, 2):
        try:
            if text:
                await cb.answer(text, show_alert=show_alert)
            else:
                await cb.answer()
            return
        except TelegramRetryAfter as exc:
            if attempt == 1:
                await _retry_after_delay(exc)
                continue
            return
        except Exception:
            return

def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids

async def resolve_limits(user_id: int) -> tuple[int, int]:
    try:
        override = await db.get_user_override(user_id)
        if override:
            return int(override[0]), int(override[1])
        if await db.check_user_exists_privilege(user_id):
            return config.vip_max_active, config.vip_max_duration
        return config.default_max_active, config.default_max_duration
    except Exception:
        return config.default_max_active, config.default_max_duration

async def resolve_daily_limit(user_id: int) -> int:
    try:
        if await db.check_user_exists_privilege(user_id):
            return config.vip_daily_limit
        return config.default_daily_limit
    except Exception:
        return config.default_daily_limit

def person_fields(
    user_id: int,
    first_name: str | None = None,
    username: str | None = None,
    *,
    since: str | None = None,
    extra: list[tuple[str, str]] | None = None,
) -> str:
    pairs: list[tuple[str, str]] = [
        ("Имя", esc(first_name) if first_name else "Не указано"),
        ("Логин", f"@{esc(username)}" if username else "Не указан"),
        ("ID", f"<code>{user_id}</code>"),
    ]
    if since:
        pairs.append(("Регистрация", since))
    if extra:
        pairs.extend(extra)
    return sheet(pairs)

async def user_display(user_id: int, extra: list[tuple[str, str]] | None = None) -> str:
    user = await db.get_user_by_id(user_id)
    if not user:
        return person_fields(user_id, extra=extra)
    first_name, username = user
    return person_fields(user_id, first_name, username, extra=extra)

class AttackState(StatesGroup):
    waiting_phone = State()
    waiting_duration = State()

class ProtectState(StatesGroup):
    waiting_phone = State()

class AdminState(StatesGroup):
    waiting_user = State()
    waiting_broadcast_text = State()
    waiting_search_query = State()
    waiting_limits_input = State()

class AttackLimitReached(RuntimeError):
    pass

class DailyLimitReached(AttackLimitReached):
    pass

class DurationLimitReached(AttackLimitReached):
    pass

class ServerBusy(RuntimeError):
    pass

class LaunchFailed(RuntimeError):
    pass

@dataclass(slots=True)
class LaunchOutcome:
    attack_id: int
    phone: str
    services_count: int = 0
    is_vip: bool = False

class AttackRuntime:

    def __init__(self, manager: AttackManager, db: Database, bot: Bot | None = None) -> None:
        self.manager = manager
        self.db = db
        self.bot = bot
        self._slots: dict[int, int] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._attacks: dict[int, Attack] = {}
        self._manual_stop: set[int] = set()
        self._cancelled: set[int] = set()
        self._cancel_epochs: dict[int, int] = {}
        self._lock = asyncio.Lock()

    def active_for(self, user_id: int) -> int:
        return sum(1 for owner in self._slots.values() if owner == user_id)

    def total_active(self) -> int:
        return len(self._slots)

    def cancel_epoch(self, user_id: int) -> int:
        return self._cancel_epochs.get(user_id, 0)

    async def launch(
        self,
        *,
        user_id: int,
        phone: str,
        duration: int,
        has_privilege: bool,
        card: Message | None = None,
        chat_id: int | None = None,
        cancel_epoch: int | None = None,
    ) -> LaunchOutcome:
        if cancel_epoch is not None and self.cancel_epoch(user_id) != cancel_epoch:
            raise LaunchFailed("запуск отменён")
        max_active, max_duration = await resolve_limits(user_id)

        if duration < 10 or duration > max_duration:
            raise DurationLimitReached(
                f"длительность {duration} превышает лимит {max_duration}"
            )

        if hasattr(self.db, "check_user_exists_privilege"):
            try:
                is_vip = await self.db.check_user_exists_privilege(user_id)
            except Exception:
                is_vip = has_privilege
        else:
            is_vip = has_privilege

        daily_limit = config.vip_daily_limit if is_vip else config.default_daily_limit

        async with self._lock:
            if self.active_for(user_id) >= max_active:
                raise AttackLimitReached(f"лимит {max_active}")
            if hasattr(self.db, "get_daily_attacks"):
                try:
                    daily_used = await self.db.get_daily_attacks(user_id)
                except Exception:
                    daily_used = 0
                if daily_used >= daily_limit:
                    raise DailyLimitReached(f"суточный лимит {daily_limit}")
            if self.total_active() >= config.max_attacks:
                raise ServerBusy("сервер занят")
            attack_id = await self.db.add_attack(user_id, phone, duration)
            self._slots[attack_id] = user_id

        async def guard() -> bool:
            if attack_id in self._cancelled:
                return False
            if cancel_epoch is not None and self.cancel_epoch(user_id) != cancel_epoch:
                return False
            if await self.db.is_user_banned(user_id, fresh=True):
                return False
            return not await self.db.is_phone_protected(phone, fresh=True)

        vip_flag = bool(is_vip or has_privilege)
        workers = config.http_workers_vip if vip_flag else config.http_workers
        rate_limit = (
            config.actual_vip_rate_limit
            if vip_flag
            else config.actual_default_rate_limit
        )
        try:
            try:
                attack = await self.manager.start(
                    phone,
                    duration,
                    workers=workers,
                    rate_limit=rate_limit,
                    guard=guard,
                    emulation=vip_flag,
                    emul_workers=config.emul_workers,
                    is_vip=vip_flag,
                )
            except TypeError:
                try:
                    attack = await self.manager.start(
                        phone,
                        duration,
                        workers=workers,
                        rate_limit=rate_limit,
                        guard=guard,
                        emulation=vip_flag,
                        emul_workers=config.emul_workers,
                    )
                except TypeError:
                    attack = await self.manager.start(
                        phone, duration, workers=workers, guard=guard
                    )
        except Busy as exc:
            await self._release(attack_id, undo_count=True)
            raise ServerBusy(str(exc)) from exc
        except EngineError as exc:
            await self._release(attack_id, undo_count=True)
            raise LaunchFailed(str(exc)) from exc
        except Exception as exc:
            await self._release(attack_id, undo_count=True)
            raise LaunchFailed(repr(exc)) from exc

        epoch_changed = (
            cancel_epoch is not None and self.cancel_epoch(user_id) != cancel_epoch
        )
        if (
            attack_id in self._cancelled
            or epoch_changed
            or await self.db.is_user_banned(user_id, fresh=True)
        ):
            # Если в _cancelled или epoch изменился - это отмена пользователем
            # Только бан админом считается не пользовательской отменой
            attack.stop(by_user=(attack_id in self._cancelled or epoch_changed))
            await attack.wait()
            await self._release(attack_id, undo_count=True)
            raise LaunchFailed("запуск отменён")

        async with self._lock:
            if attack_id in self._cancelled:
                cancelled = True
            else:
                cancelled = False
                self._attacks[attack_id] = attack
        if cancelled:
            attack.stop(by_user=True)
            await attack.wait()
            await self._release(attack_id, undo_count=True)
            raise LaunchFailed("запуск отменён")

        try:
            await self.db.mark_started(attack_id)
        except Exception as exc:
            logger.warning("attack #%s: mark_started failed: %r", attack_id, exc)

        self._tasks[attack_id] = asyncio.create_task(
            self._watch(user_id, attack_id, attack, phone, duration, card, chat_id),
            name=f"watch-{attack_id}",
        )
        logger.info("user %s: attack %s (#%s) launched on +%s", user_id, attack.id, attack_id, phone)
        b_count = len(getattr(getattr(attack, "_browser", None), "services", {})) if getattr(attack, "_browser", None) else 0
        total_services = len(getattr(attack, "jobs", [])) + b_count
        return LaunchOutcome(
            attack_id=attack_id,
            phone=phone,
            services_count=total_services,
            is_vip=vip_flag,
        )

    async def stop(self, user_id: int, attack_id: int) -> bool:
        async with self._lock:
            if self._slots.get(attack_id) != user_id or attack_id in self._cancelled:
                return False
            self._manual_stop.add(attack_id)
            self._cancelled.add(attack_id)
            self._cancel_epochs[user_id] = self.cancel_epoch(user_id) + 1
        attack = self._attacks.get(attack_id)
        if attack is not None:
            attack.stop(by_user=True)
            await attack.wait()
        async with self._lock:
            self._slots.pop(attack_id, None)
        await self._mark_stopped(attack_id)
        return True

    async def stop_user(self, user_id: int) -> int:
        stopped = 0
        for attack_id in [aid for aid, owner in self._slots.items() if owner == user_id]:
            if await self.stop(user_id, attack_id):
                stopped += 1
        return stopped

    async def shutdown(self) -> None:
        for attack in list(self._attacks.values()):
            attack.stop(by_user=False)
        tasks = list(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
        self._slots.clear()
        self._attacks.clear()
        self._tasks.clear()
        self._cancelled.clear()
        self._manual_stop.clear()
        self._cancel_epochs.clear()

    async def _release(self, attack_id: int, undo_count: bool = False) -> None:
        async with self._lock:
            self._slots.pop(attack_id, None)
            self._cancelled.discard(attack_id)
            self._manual_stop.discard(attack_id)
        if undo_count:
            try:
                await self.db.undo_attack(attack_id)
            except Exception as exc:
                logger.warning("attack #%s: undo count failed: %r", attack_id, exc)
            return
        await self._mark_stopped(attack_id)

    async def _watch(
        self,
        user_id: int,
        attack_id: int,
        attack: Attack,
        phone: str,
        duration: int,
        card_message: Message | None,
        chat_id: int | None,
    ) -> None:
        try:
            await attack.wait()
        except asyncio.CancelledError:
            await self._mark_stopped(attack_id)
            raise
        finally:
            async with self._lock:
                self._slots.pop(attack_id, None)
                self._attacks.pop(attack_id, None)
                self._tasks.pop(attack_id, None)
                self._cancelled.discard(attack_id)

        manual = attack_id in self._manual_stop
        self._manual_stop.discard(attack_id)

        user_stopped = getattr(attack, "user_stopped", False)
        try:
            if user_stopped:
                await self.db.mark_stopped(attack_id)
            else:
                await self.db.mark_finished(attack_id)
        except Exception as exc:
            logger.warning("attack #%s: db finalize failed: %r", attack_id, exc)

        try:
            snapshot = attack.snapshot()
        except Exception as exc:
            logger.warning("attack #%s: snapshot failed: %r", attack_id, exc)
            snapshot = {}

        event_name = "stopped" if (manual or user_stopped) else "finished"
        logger.info(
            "attack #%s %s: sent=%s ok=%s unconfirmed=%s err=%s",
            attack_id,
            event_name,
            snapshot.get("sent"),
            snapshot.get("ok"),
            snapshot.get("unconfirmed", 0),
            snapshot.get("err"),
        )
        failures = snapshot.get("failures") or {}
        if failures:
            top = sorted(failures.items(), key=lambda item: item[1], reverse=True)[:5]
            logger.info(
                "attack #%s failures: %s",
                attack_id,
                ", ".join(f"{name}={count}" for name, count in top),
            )
        statuses = snapshot.get("statuses") or {}
        if statuses:
            ranked = sorted(statuses.items(), key=lambda item: item[1], reverse=True)[:6]
            logger.info(
                "attack #%s statuses: %s",
                attack_id,
                ", ".join(f"{code}={count}" for code, count in ranked),
            )

        if manual or user_stopped:
            return
        by_service = snapshot.get("by_service") or {}
        if by_service:
            ranked = sorted(
                by_service.items(),
                key=lambda item: sum(item[1].values()),
                reverse=True,
            )[:6]
            logger.info(
                "attack #%s services: %s",
                attack_id,
                ", ".join(
                    f"{name}="
                    + "/".join(
                        f"{code}:{count}"
                        for code, count in sorted(
                            codes.items(), key=lambda item: item[1], reverse=True
                        )[:4]
                    )
                    for name, codes in ranked
                ),
            )
        finished = attack_finished_text(display_phone(phone), duration)
        if card_message is None:
            if chat_id is None:
                return
            bot_inst = self.bot or bot
            if bot_inst is None:
                return
            try:
                await say(bot_inst, chat_id, finished, back_kb())
            except Exception as exc:
                logger.warning("attack #%s: say finish card failed: %r", attack_id, exc)
            return
        try:
            await show(card_message, finished, back_kb())
        except Exception:
            target_chat = chat_id or (card_message.chat.id if getattr(card_message, "chat", None) else None)
            if target_chat is not None:
                bot_inst = getattr(card_message, "bot", None) or self.bot or bot
                if bot_inst is not None:
                    try:
                        await say(bot_inst, target_chat, finished, back_kb())
                    except Exception as exc:
                        logger.warning("attack #%s: fallback say finish card failed: %r", attack_id, exc)

    async def _mark_stopped(self, attack_id: int) -> None:
        try:
            await self.db.mark_stopped(attack_id)
        except Exception as exc:
            logger.warning("attack #%s: db stop failed: %r", attack_id, exc)

def _extract_user(event: TelegramObject):
    user = getattr(event, "from_user", None)
    if user is not None:
        return user
    for attr in (
        "message",
        "edited_message",
        "callback_query",
        "pre_checkout_query",
        "chat_member",
        "my_chat_member",
    ):
        nested = getattr(event, attr, None)
        if nested is not None:
            user = getattr(nested, "from_user", None)
            if user is not None:
                return user
            user = getattr(nested, "new_chat_member", None)
            if user is not None and getattr(user, "user", None):
                return user.user
    return None

@dataclass(slots=True)
class _UserLock:
    lock: asyncio.Lock
    touched: float

_USER_LOCK_TTL = 30 * 60.0
_USER_LOCK_MAX = 1024
_user_locks: dict[int, _UserLock] = {}
_user_locks_guard = asyncio.Lock()

def _prune_user_locks(now: float) -> None:
    for uid, entry in list(_user_locks.items()):
        if entry.lock.locked() or getattr(entry.lock, "_waiters", None):
            continue
        if now - entry.touched > _USER_LOCK_TTL:
            _user_locks.pop(uid, None)
    if len(_user_locks) <= _USER_LOCK_MAX:
        return
    idle = sorted(
        (
            (entry.touched, uid)
            for uid, entry in _user_locks.items()
            if not entry.lock.locked() and not getattr(entry.lock, "_waiters", None)
        ),
    )
    for _touched, uid in idle[: len(_user_locks) - _USER_LOCK_MAX]:
        _user_locks.pop(uid, None)

async def _user_lock(user_id: int) -> asyncio.Lock:
    now = time.monotonic()
    entry = _user_locks.get(user_id)
    if entry is not None:
        entry.touched = now
        return entry.lock
    async with _user_locks_guard:
        entry = _user_locks.get(user_id)
        if entry is not None:
            entry.touched = now
            return entry.lock
        _prune_user_locks(now)
        entry = _UserLock(asyncio.Lock(), now)
        _user_locks[user_id] = entry
        return entry.lock

class AccessMiddleware(BaseMiddleware):

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user") or _extract_user(event)
        if user is None:
            return await handler(event, data)

        if not is_admin(user.id) and await db.is_user_banned(user.id):
            if isinstance(event, Update):
                inner = (
                    event.message
                    or event.edited_message
                    or event.callback_query
                    or event.pre_checkout_query
                )
            else:
                inner = event
            if isinstance(inner, PreCheckoutQuery):
                try:
                    await inner.answer(ok=False, error_message="Аккаунт заблокирован.")
                except Exception:
                    pass
                return
            if isinstance(inner, Message) and inner.successful_payment is not None:
                return await handler(event, data)
            if isinstance(inner, CallbackQuery):
                await safe_cb_answer(inner, "Аккаунт заблокирован.", show_alert=True)
                return
            text = getattr(inner, "text", "") or ""
            if text.startswith("/start"):
                return await handler(event, data)
            if isinstance(inner, Message):
                await say(bot, inner.chat.id, ban_text())
            return

        if isinstance(event, Update):
            if event.chat_member or event.my_chat_member:
                lock = await _user_lock(user.id)
                async with lock:
                    return await handler(event, data)
            inner = (
                event.message
                or event.edited_message
                or event.callback_query
                or event.pre_checkout_query
            )
        elif isinstance(event, ChatMemberUpdated):
            lock = await _user_lock(user.id)
            async with lock:
                return await handler(event, data)
        else:
            inner = event
        chat = getattr(inner, "chat", None)
        chat_type = getattr(chat, "type", None)
        if (
            not is_admin(user.id)
            and chat_type is not None
            and chat_type != "private"
        ):
            if isinstance(inner, CallbackQuery):
                await safe_cb_answer(inner, "Только в личном чате.", show_alert=True)
            return

        lock = await _user_lock(user.id)
        async with lock:
            return await handler(event, data)

async def global_error_handler(event: ErrorEvent) -> None:
    logger.error(
        "unhandled update error: %s",
        "".join(
            traceback.format_exception(
                type(event.exception), event.exception, event.exception.__traceback__
            )
        ),
    )
    if is_transient_update_error(event.exception):
        return
    try:
        update = event.update
        target = None
        if update.message is not None:
            target = update.message
        elif update.callback_query is not None and update.callback_query.message is not None:
            target = update.callback_query.message
        if target is not None:
            await say(
                bot,
                target.chat.id,
                card(
                    f"{e('warn')} Не вышло",
                    "Запрос сорвался. Нажми кнопку ещё раз.",
                ),
                back_kb(),
            )
    except Exception:
        pass

_sub_cache: dict[int, tuple[bool, float]] = {}
_sub_cache_ttl = 60.0
_MAX_SUB_CACHE = 4096

def _store_sub_cache(user_id: int, is_sub: bool, expires: float) -> None:
    if len(_sub_cache) >= _MAX_SUB_CACHE:
        now_t = time.monotonic()
        expired = [k for k, (_, exp) in _sub_cache.items() if exp <= now_t]
        for k in expired:
            _sub_cache.pop(k, None)
        if len(_sub_cache) >= _MAX_SUB_CACHE:
            for k in list(_sub_cache.keys())[:1024]:
                _sub_cache.pop(k, None)
    _sub_cache[user_id] = (is_sub, expires)

async def check_channel_subscription(
    bot_instance: Any, user_id: int, *, ignore_cache: bool = False
) -> bool:
    if not getattr(config, "force_channel_sub", True):
        return True
    if is_admin(user_id):
        return True
    channel = getattr(config, "channel_id", "@DevNoReply")
    if not channel:
        return True
    if bot_instance is None or not hasattr(bot_instance, "get_chat_member"):
        return True

    now = time.monotonic()
    if not ignore_cache:
        cached = _sub_cache.get(user_id)
        if cached is not None:
            is_sub, exp = cached
            if now < exp:
                return is_sub

    try:
        member = await bot_instance.get_chat_member(chat_id=channel, user_id=user_id)
        status = getattr(member, "status", None)
        is_sub = status in ("creator", "administrator", "member", "restricted")
        ttl = _sub_cache_ttl if is_sub else 10.0
        _store_sub_cache(user_id, is_sub, now + ttl)
        return is_sub
    except Exception as exc:
        msg = str(exc).lower()
        if "user not found" in msg or "participant" in msg:
            _store_sub_cache(user_id, False, now + 10.0)
            return False
        logger.warning(
            "Не удалось проверить подписку пользователя %s в канале %s: %s",
            user_id,
            channel,
            exc,
        )
        return True

async def ensure_subscribed(target: Message | CallbackQuery) -> bool:
    user = getattr(target, "from_user", None)
    if user is None:
        return True
    if is_admin(user.id):
        return True
    bot_inst = bot
    subscribed = await check_channel_subscription(bot_inst, user.id)
    if subscribed:
        return True

    if isinstance(target, CallbackQuery):
        await safe_cb_answer(target, "Для продолжения подпишитесь на канал @DevNoReply!", show_alert=True)
        if getattr(target, "message", None) is not None:
            await show(target.message, sub_card(), sub_kb(), sticker=config.sticker_welcome)
    else:
        chat = getattr(target, "chat", None)
        chat_id = getattr(chat, "id", None) if chat is not None else None
        if chat_id is not None:
            bot_to_use = bot_inst if bot_inst is not None else bot
            await say(bot_to_use, chat_id, sub_card(), sub_kb(), sticker=config.sticker_welcome)
    return False

async def check_sub_cb(cb: CallbackQuery, state: FSMContext) -> None:
    user_id = cb.from_user.id
    bot_inst = bot
    subscribed = await check_channel_subscription(bot_inst, user_id, ignore_cache=True)
    if not subscribed:
        await safe_cb_answer(
            cb,
            "Вы ещё не подписались на канал @DevNoReply! Подпишитесь и нажмите кнопку снова.",
            show_alert=True,
        )
        return
    await safe_cb_answer(cb, "Подписка подтверждена!", show_alert=True)
    if not await db.has_accepted_terms(user_id):
        await show(cb.message, terms_text(), terms_kb(), sticker=config.sticker_welcome)
        return
    await state.clear()
    await show(
        cb.message,
        menu_caption(cb.from_user.first_name),
        await main_kb(user_id),
        sticker=config.sticker_menu,
    )

async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user

    await db.add_user(user.id, user.first_name, user.username)

    if await db.is_user_banned(user.id):
        await say(bot, message.chat.id, ban_text())
        return

    if not await ensure_subscribed(message):
        return

    if not await db.has_accepted_terms(user.id):
        await say(bot, message.chat.id, terms_text(), terms_kb(), sticker=config.sticker_welcome)
        return

    await say(
        bot,
        message.chat.id,
        menu_caption(user.first_name),
        await main_kb(user.id),
        sticker=config.sticker_menu,
        message_effect_id=EFFECT_HEART,
    )

async def accept_terms(cb: CallbackQuery) -> None:
    if not await ensure_subscribed(cb):
        return
    await db.accept_terms(cb.from_user.id)
    await safe_cb_answer(cb, "Принято")
    await show(
        cb.message,
        menu_caption(cb.from_user.first_name),
        await main_kb(cb.from_user.id),
        sticker=config.sticker_menu,
        message_effect_id=EFFECT_HEART,
    )

async def open_menu(cb: CallbackQuery, state: FSMContext) -> None:
    if await db.is_user_banned(cb.from_user.id):
        await say(bot, cb.message.chat.id, ban_text())
        return
    if not await ensure_subscribed(cb):
        return
    await state.clear()
    await safe_cb_answer(cb)
    await show(
        cb.message,
        menu_caption(cb.from_user.first_name),
        await main_kb(cb.from_user.id),
        sticker=config.sticker_menu,
    )

def register_common(dp: Dispatcher) -> None:
    dp.errors.register(global_error_handler)
    dp.message.register(cmd_start, CommandStart())
    dp.callback_query.register(check_sub_cb, F.data == "check_sub")
    dp.callback_query.register(accept_terms, F.data == "accept")
    dp.callback_query.register(open_menu, F.data == "menu")

vip_payload = "buy_privilege"

async def issue_vip_community_link(bot_inst: Bot | None, user_id: int) -> str | None:
    if bot_inst is None or not getattr(config, "vip_community_chat_id", None):
        return None
    if not hasattr(bot_inst, "create_chat_invite_link"):
        return None
    try:
        if hasattr(db, "get_active_vip_invites_for_user"):
            old_links = await db.get_active_vip_invites_for_user(user_id)
            for old_link in old_links:
                try:
                    await bot_inst.revoke_chat_invite_link(
                        chat_id=config.vip_community_chat_id,
                        invite_link=old_link,
                    )
                except Exception:
                    pass
                if hasattr(db, "mark_vip_invite_link_revoked"):
                    await db.mark_vip_invite_link_revoked(old_link)

        invite = await bot_inst.create_chat_invite_link(
            chat_id=config.vip_community_chat_id,
            name=f"VIP_{user_id}",
            member_limit=1,
        )
        link = getattr(invite, "invite_link", str(invite))
        if hasattr(db, "save_vip_invite_link"):
            await db.save_vip_invite_link(user_id, link, config.vip_community_chat_id)
        logger.info("Generated single-use VIP invite link for user %s: %s", user_id, link)
        return link
    except Exception as exc:
        logger.warning(
            "Failed to create VIP community invite link for user %s (chat %s): %r",
            user_id,
            config.vip_community_chat_id,
            exc,
        )
        return None

async def get_or_create_vip_community_link(bot_inst: Bot | None, user_id: int) -> str | None:
    if hasattr(db, "has_user_joined_vip_community"):
        if await db.has_user_joined_vip_community(user_id):
            return None
    if hasattr(db, "get_active_vip_invites_for_user"):
        active = await db.get_active_vip_invites_for_user(user_id)
        if active:
            return active[0]
    return await issue_vip_community_link(bot_inst, user_id)

async def vip_chat_member_handler(event: ChatMemberUpdated, bot: Bot | None = None) -> None:
    target_chat = getattr(config, "vip_community_chat_id", None)
    if not target_chat:
        return
    event_chat_id = getattr(getattr(event, "chat", None), "id", None)
    if event_chat_id != target_chat:
        return

    old_member = getattr(event, "old_chat_member", None)
    new_member = getattr(event, "new_chat_member", None)

    old_status = getattr(old_member, "status", None)
    new_status = getattr(new_member, "status", None)

    if new_status not in ("member", "administrator", "creator", "restricted"):
        return
    if old_status in ("member", "administrator", "creator"):
        return

    bot_inst = bot or globals().get("bot")
    joined_user = getattr(new_member, "user", None)
    from_user = getattr(event, "from_user", None)
    user_id = (
        getattr(joined_user, "id", None)
        or getattr(from_user, "id", 0)
    )
    logger.info(
        "User %s (%s) joined VIP community %s (old=%s, new=%s)",
        user_id,
        getattr(joined_user, "username", None),
        event_chat_id,
        old_status,
        new_status,
    )

    revoked_links: set[str] = set()

    invite_obj = getattr(event, "invite_link", None)
    event_link = getattr(invite_obj, "invite_link", None) if invite_obj else None
    if event_link:
        revoked_links.add(event_link)
        if bot_inst and hasattr(bot_inst, "revoke_chat_invite_link"):
            try:
                await bot_inst.revoke_chat_invite_link(
                    chat_id=event_chat_id,
                    invite_link=event_link,
                )
                logger.info("Revoked invite link %s after user %s joined", event_link, user_id)
            except Exception as exc:
                logger.info("Revoke invite link %s on join returned: %r", event_link, exc)
        if hasattr(db, "mark_vip_invite_link_used"):
            await db.mark_vip_invite_link_used(event_link, user_id=user_id)

    if user_id and hasattr(db, "get_active_vip_invites_for_user"):
        active_links = await db.get_active_vip_invites_for_user(user_id)
        for link in active_links:
            if link not in revoked_links:
                revoked_links.add(link)
                if bot_inst and hasattr(bot_inst, "revoke_chat_invite_link"):
                    try:
                        await bot_inst.revoke_chat_invite_link(
                            chat_id=target_chat,
                            invite_link=link,
                        )
                        logger.info("Revoked active DB link %s for user %s on join", link, user_id)
                    except Exception as exc:
                        logger.info("Revoke active DB link %s returned: %r", link, exc)
                if hasattr(db, "mark_vip_invite_link_used"):
                    await db.mark_vip_invite_link_used(link, user_id=user_id)

async def buy_privilege(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_cb_answer(cb)

    if await db.check_user_exists_privilege(cb.from_user.id):
        user_id = cb.from_user.id
        exp = await db.get_user_privilege_expiry(user_id)
        exp_str = format_dt(exp) if exp else "активен"
        has_joined = (
            await db.has_user_joined_vip_community(user_id)
            if hasattr(db, "has_user_joined_vip_community")
            else False
        )
        invite_link = None
        community_note = ""
        if has_joined:
            community_note = f"\n\n{e('spark')} Вы уже состоите в приватном VIP-сообществе."
        else:
            invite_link = await get_or_create_vip_community_link(bot, user_id)
            if invite_link:
                community_note = (
                    f"\n\n{e('spark')} <b>Приватное VIP-сообщество:</b>\n"
                    f"Ваша индивидуальная ссылка:\n👉 <a href=\"{invite_link}\">{invite_link}</a>\n"
                    f"<i>(Ссылка одноразовая и выключается сразу после входа)</i>"
                )

        await say(
            bot,
            cb.message.chat.id,
            card(
                f"{e('crown')} VIP уже активен",
                f"Подписка активна до <b>{exp_str}</b>.\nПовторная оплата будет доступна после окончания срока действия."
                + community_note,
                footer=f"{e('info')} Статус: VIP (30 дней)",
            ),
            vip_activated_kb(invite_link),
            sticker=config.sticker_vip,
        )
        return

    caption = (
        vip_offer_text()
        + "\n\n"
        + quote(f"{e('coin')} Выберите способ оплаты: Telegram Stars (звёзды) или криптовалюта через xRocket.")
    )
    await show(cb.message, caption, vip_methods_kb(), sticker=config.sticker_vip)

async def pay_stars(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if await db.check_user_exists_privilege(cb.from_user.id):
        await safe_cb_answer(cb, "VIP уже включён!", show_alert=True)
        return
    await safe_cb_answer(cb)

    await say(bot, cb.message.chat.id, vip_offer_text(), sticker=config.sticker_vip)
    await cb.message.answer_invoice(
        title="Покупка VIP",
        description=f"VIP на 30 дней, {config.vip_price} звёзд",
        prices=[LabeledPrice(label="Звёзды", amount=config.vip_price)],
        provider_token="",
        payload=vip_payload,
        currency="XTR",
        reply_markup=payment_kb(),
    )

async def pay_xrocket_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if await db.check_user_exists_privilege(cb.from_user.id):
        await safe_cb_answer(cb, "VIP уже включён!", show_alert=True)
        return
    if not config.xrocket_enabled:
        await safe_cb_answer(cb, "Оплата криптовалютой временно недоступна.", show_alert=True)
        return
    await safe_cb_answer(cb)

    caption = card(
        f"{e('gem')} Оплата через xRocket",
        "Выберите криптовалюту для оплаты тарифа VIP:\n\n"
        "▫️ <b>USDT</b> — сети TON и TRON (TRC-20)\n"
        "▫️ <b>GRAM</b> — сеть TON\n\n"
        "Оплата осуществляется через официальный шлюз @xrocket. "
        "Активация происходит автоматически сразу после подтверждения транзакции сетью.",
        footer=f"{e('info')} Тариф VIP активируется на 30 дней",
    )
    await show(cb.message, caption, xrocket_menu_kb(), sticker=config.sticker_vip)

async def xrocket_create(cb: CallbackQuery) -> None:
    user_id = cb.from_user.id
    if not config.xrocket_enabled:
        await safe_cb_answer(cb, "Оплата криптовалютой временно недоступна.", show_alert=True)
        return
    if await db.check_user_exists_privilege(user_id):
        await safe_cb_answer(cb, "VIP уже включён!", show_alert=True)
        return

    currency = "USDT"
    if cb.data and ":" in cb.data:
        currency = cb.data.split(":", 1)[1].upper()
    if currency not in config.xrocket_vip_prices:
        currency = "USDT"

    amount = xrocket_price(currency)

    await safe_cb_answer(cb, "Создаю счёт…")
    try:
        inv = await xrocket_client.create_invoice(
            amount=amount,
            currency=currency,
            description="VIP доступ Revolution",
            user_id=user_id,
        )
    except Exception as exc:
        logger.error("user %s: failed to create xrocket invoice: %r", user_id, exc)
        await show(
            cb.message,
            card(
                f"{e('warn')} Ошибка создания счёта",
                f"Не удалось связаться с xRocket: {esc(exc)}\n\nПопробуйте позже или оплатите звёздами.",
            ),
            back_kb(),
        )
        return

    inv_id = inv["id"]
    pay_url = inv["pay_url"]
    await db.save_xrocket_invoice(inv_id, user_id, str(amount), currency, pay_url)

    networks_str = "TON, TRON (TRC-20)" if currency == "USDT" else "TON"
    caption = card(
        f"{e('crown')} Счёт xRocket готов",
        f"Сумма: <b>{amount} {currency}</b>\n"
        f"Сети: <b>{networks_str}</b>\n\n"
        f"1. Нажми «Оплатить в xRocket» и подтверди перевод в боте.\n"
        f"2. После оплаты нажми «Проверить оплату» (или бот сам обновит статус через несколько секунд).",
        footer=f"{e('clock')} Счёт действует 1 час",
    )
    await show(cb.message, caption, xrocket_invoice_kb(pay_url, inv_id))

async def xrocket_check(cb: CallbackQuery) -> None:
    user_id = cb.from_user.id
    inv_id = cb.data.split(":", 1)[1] if cb.data and ":" in cb.data else ""
    if not inv_id:
        await safe_cb_answer(cb, "Неверный идентификатор счёта", show_alert=True)
        return

    inv_record = await db.get_xrocket_invoice(inv_id)
    if not inv_record:
        await safe_cb_answer(cb, "Счёт не найден в базе данных", show_alert=True)
        return
    rec_inv_id, rec_user_id, rec_amount, rec_currency, rec_status, _rec_pay_url = inv_record
    if rec_user_id != user_id:
        await safe_cb_answer(cb, "Этот счёт был выставлен другому пользователю", show_alert=True)
        return

    if await db.check_user_exists_privilege(user_id):
        await safe_cb_answer(cb, "VIP уже активен!", show_alert=True)
        await show(
            cb.message,
            card(f"{e('crown')} VIP уже включён", "Все привилегии активны."),
            back_kb(),
        )
        return

    try:
        inv = await xrocket_client.get_invoice(inv_id)
    except Exception as exc:
        logger.warning("xrocket check invoice %s failed: %r", inv_id, exc)
        await safe_cb_answer(cb, "Ошибка связи с xRocket, попробуйте чуть позже", show_alert=True)
        return

    status = inv.get("status")
    if status == "paid":
        currency = (inv.get("priceCurrency") or rec_currency or "USDT").upper().strip()
        expected_amount = xrocket_price(currency)
        amount_val = inv.get("priceAmount") or rec_amount or str(expected_amount)
        try:
            amount_float = float(amount_val)
        except (ValueError, TypeError):
            amount_float = float(expected_amount)

        if amount_float < (expected_amount - 0.05):
            await safe_cb_answer(
                cb,
                f"Оплаченная сумма ({amount_float} {currency}) меньше требуемой ({expected_amount} {currency})",
                show_alert=True,
            )
            return

        charge_id = f"xrocket_{inv_id}"
        amount_int = int(amount_float * 100)
        try:
            grant_status = await db.complete_payment(
                user_id, charge_id, amount_int, currency, "vip"
            )
            await db.update_xrocket_invoice_status(inv_id, "paid")
        except Exception:
            logger.exception("user %s: failed to complete xrocket payment %s", user_id, inv_id)
            grant_status = "error"

        if grant_status in ("granted", "duplicate", "already_vip"):
            await safe_cb_answer(cb, "Оплата получена! VIP активирован.", show_alert=True)
            invite_link = await issue_vip_community_link(bot, user_id)
            await show(
                cb.message,
                vip_success_card(invite_link),
                vip_activated_kb(invite_link),
                sticker=config.sticker_vip,
            )
            return

        if grant_status == "error":
            await safe_cb_answer(
                cb,
                f"Ошибка активации VIP в базе данных. Платёж зафиксирован ({inv_id}), обратитесь к администратору.",
                show_alert=True,
            )
            return

    if status == "active":
        await safe_cb_answer(
            cb,
            "Оплата пока не поступила. Оплатите счёт в боте @xrocket и нажмите снова.",
            show_alert=True,
        )
        return

    if status in ("expired", "cancelled"):
        await db.update_xrocket_invoice_status(inv_id, status)
        await safe_cb_answer(
            cb,
            "Счёт истёк или был отменён. Пожалуйста, создайте новый счёт.",
            show_alert=True,
        )
        return

    await safe_cb_answer(cb, f"Статус счёта: {status}", show_alert=True)

async def pre_checkout(pcq: PreCheckoutQuery) -> None:
    if await db.is_user_banned(pcq.from_user.id):
        await pcq.answer(ok=False, error_message="Аккаунт заблокирован.")
        return
    if (
        pcq.invoice_payload != vip_payload
        or pcq.currency != "XTR"
        or pcq.total_amount != config.vip_price
    ):
        await pcq.answer(ok=False, error_message="Счёт устарел — открой VIP заново.")
        return
    if await db.check_user_exists_privilege(pcq.from_user.id):
        await pcq.answer(ok=False, error_message="VIP уже активен — оплата не нужна.")
        return
    await pcq.answer(ok=True)

async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if payment is None or payment.invoice_payload != vip_payload:
        return
    user_id = message.from_user.id
    charge_id = payment.telegram_payment_charge_id
    if payment.currency != "XTR" or payment.total_amount != config.vip_price:
        logger.warning(
            "unexpected payment from %s: %s %s charge=%s",
            user_id,
            payment.total_amount,
            payment.currency,
            charge_id,
        )
        await _refund_stars(user_id, charge_id)
        return

    try:
        status = await db.complete_payment(
            user_id,
            charge_id,
            int(payment.total_amount),
            payment.currency,
            payment.invoice_payload,
        )
    except Exception:
        logger.exception("user %s: cannot record payment charge=%s", user_id, charge_id)
        refunded = await _refund_stars(user_id, charge_id)
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('warn')} Платёж не записался",
                (
                    "Звёзды вернулись на баланс. Попробуй купить VIP ещё раз."
                    if refunded
                    else "Звёзды не списались окончательно — напиши админу, платёж виден по ID."
                ),
            ),
            back_kb(),
        )
        return

    if status == "granted":
        logger.info("user %s bought VIP charge=%s", user_id, charge_id)
        invite_link = await issue_vip_community_link(bot, user_id)
        await say(
            bot,
            message.chat.id,
            vip_success_card(invite_link),
            vip_activated_kb(invite_link),
            sticker=config.sticker_vip,
        )
        return

    if status == "duplicate":
        logger.info("user %s: duplicate payment update ignored charge=%s", user_id, charge_id)
        return

    if status not in ("already_vip", "refund_pending"):
        logger.error("user %s: unknown payment status %r charge=%s", user_id, status, charge_id)
        return

    try:
        await db.mark_payment_refund_pending(charge_id)
    except Exception as exc:
        logger.warning("charge=%s: cannot mark refund pending: %r", charge_id, exc)

    refunded = await _refund_stars(user_id, charge_id)
    if refunded:
        try:
            await db.mark_payment_refunded(charge_id)
        except Exception as exc:
            logger.warning("charge=%s: cannot mark refunded: %r", charge_id, exc)
    await say(
        bot,
        message.chat.id,
        card(
            f"{e('crown')} VIP уже был",
            (
                "Повторный платёж вернулся на баланс звёзд."
                if refunded
                else f"Повторный платёж прошёл. Звёзды вернутся по charge <code>{esc(charge_id)}</code>."
            ),
        ),
        back_kb(),
    )

async def _refund_stars(user_id: int, charge_id: str) -> bool:
    bot_inst = bot
    if bot_inst is None:
        return False
    for attempt in (1, 2):
        try:
            await bot_inst.refund_star_payment(
                user_id=user_id, telegram_payment_charge_id=charge_id
            )
            logger.info("user %s: payment refunded charge=%s", user_id, charge_id)
            return True
        except Exception as exc:
            logger.warning(
                "user %s: star refund failed charge=%s attempt=%s: %r",
                user_id,
                charge_id,
                attempt,
                exc,
            )
            if attempt == 1:
                await asyncio.sleep(0.5)
    return False

def register_payments(dp: Dispatcher) -> None:
    dp.callback_query.register(buy_privilege, F.data == "buy_privilege")
    dp.callback_query.register(pay_stars, F.data == "pay_stars")
    dp.callback_query.register(pay_xrocket_menu, F.data == "pay_xrocket_menu")
    dp.callback_query.register(xrocket_create, F.data.startswith("xrocket_create:"))
    dp.callback_query.register(xrocket_check, F.data.startswith("xrocket_check:"))
    dp.pre_checkout_query.register(pre_checkout)
    dp.message.register(successful_payment, F.successful_payment)

def _classify(raw_text: str) -> tuple[list[str], list[str]]:
    valid: list[str] = []
    skipped: list[str] = []
    for token in split_phone_candidates(raw_text):
        phone = normalize_phone(token)
        if not phone or is_fake_number(phone):
            skipped.append(token[:24])
        elif phone not in valid:
            valid.append(phone)
    return valid, skipped

async def _filter_protected(phones: list[str]) -> tuple[list[str], list[str]]:
    try:
        return await db.filter_protected(phones)
    except Exception:
        logger.warning("protected check failed", exc_info=True)
        return list(phones), []

def _flag(phone: str) -> str:
    return e("ua") if phone_country(phone) == "UA" else e("ru")

def _filter_note(skipped: list[str], protected: list[str]) -> str:
    lines = []
    if skipped:
        shown = esc(", ".join(skipped[:8]))
        tail = "…" if len(skipped) > 8 else ""
        lines.append(
            f"{e('warn')} Пропущено некорректных номеров ({len(skipped)}): {shown}{tail}. "
            "Они исключены из списка целей."
        )
    if protected:
        extra = " и ещё" if len(protected) > 1 else ""
        lines.append(
            f"{e('shield')} В стоп-листе защиты ({len(protected)}{extra}): "
            f"<code>{esc(display_phone(protected[0]))}</code>. Запуск теста на них заблокирован."
        )
    return ("\n" + "\n".join(lines)) if lines else ""

def _no_slots_text(has_privilege: bool, max_active: int) -> str:
    if has_privilege:
        return card(
            f"{e('hourglass')} Все VIP-слоты заняты",
            f"Сейчас активно <b>{max_active} из {max_active}</b> параллельных сессий VIP.\n\n"
            "▫️ Дождитесь завершения любого из активных тестов\n"
            "▫️ Либо остановите ненужную сессию досрочно кнопкой управления",
            footer=f"{e('info')} Потолок для VIP: {max_active} одновременных сессий по {fmt_dur(config.vip_max_duration)}",
        )
    return card(
        f"{e('hourglass')} Достигнут лимит активных сессий",
        f"Без VIP одновременно доступна только 1 сессия (занято <b>1 из 1</b> слота).\n\n"
        "▫️ Дождитесь завершения текущей сессии стресс-тестирования\n"
        "▫️ Либо остановите её вручную кнопкой под карточкой\n"
        f"▫️ Подключите доступный VIP для запуска до <b>{config.vip_max_active} сессий одновременно</b>",
        footer=f"{e('crown')} VIP: до {config.vip_max_active} сессий сразу, таймер до {fmt_dur(config.vip_max_duration)} и скорость х2",
    )

def _daily_limit_text(has_privilege: bool, daily_limit: int) -> str:
    if has_privilege:
        return card(
            f"{e('hourglass')} Суточный лимит исчерпан",
            f"На сегодня израсходованы все <b>{daily_limit} из {daily_limit}</b> доступных VIP-запусков.\n\n"
            "▫️ Счётчик квот автоматически обновится в начале новых суток (00:00 МСК)\n"
            f"▫️ В полночь вам снова будут доступны все {daily_limit} запусков",
            footer=f"{e('clock')} Автоматический сброс счётчика: 00:00 МСК",
        )
    return card(
        f"{e('hourglass')} Суточный лимит исчерпан",
        f"Без VIP доступно <b>{daily_limit} запусков в сутки</b> — все {daily_limit} на сегодня уже использованы.\n\n"
        "▫️ Дождитесь автоматического сброса счётчика в начале новых суток (00:00 МСК)\n"
        f"▫️ Либо подключите недорогой VIP и получите <b>{config.vip_daily_limit} запусков в сутки</b> прямо сейчас",
        footer=f"{e('crown')} VIP: {config.vip_daily_limit} запусков в сутки, до {config.vip_max_active} сессий одновременно",
    )

def _duration_prompt(
    phones: list[str], max_active: int, max_duration: int, note: str
) -> str:
    if max_duration == config.vip_max_duration:
        hint = f"{e('crown')} <b>Тариф VIP:</b> максимум — <b>{fmt_dur(max_duration)}</b> ({max_duration} сек)."
    elif max_duration != config.default_max_duration:
        hint = f"{e('gear')} <b>Индивидуальный лимит:</b> максимум — <b>{fmt_dur(max_duration)}</b> ({max_duration} сек)."
    else:
        hint = (
            f"{e('clock')} <b>Базовый лимит:</b> <b>{max_duration} сек ({fmt_dur(max_duration)})</b>\n"
            f"{e('crown')} <i>VIP увеличивает лимит до {fmt_dur(config.vip_max_duration)} ({config.vip_max_duration} сек).</i>"
        )
    dur_subtext = "<i>Примеры: 60 (1 мин), 300 (5 мин), 3600 (1 час)</i>\n" if max_duration >= 60 else ""
    if len(phones) == 1:
        phone = phones[0]
        return card(
            f"{e('target')} Длительность стресс-теста",
            f"<b>Целевой номер:</b> {_flag(phone)} <code>{esc(display_phone(phone))}</code>\n\n"
            f"Отправьте целое число секунд от <b>10</b> до <b>{max_duration}</b>:\n\n"
            f"{dur_subtext}{hint}"
            + note,
            footer=f"{e('info')} Отправьте сообщение с числом секунд в чат",
        )
    listing = "\n".join(
        f"▫️ {_flag(phone)} <code>{esc(display_phone(phone))}</code>" for phone in phones[:15]
    )
    more = f"\n…и ещё {len(phones) - 15} номеров" if len(phones) > 15 else ""
    return card(
        f"{e('target')} Длительность для {len(phones)} номеров",
        f"<b>Выбранные цели:</b>\n"
        + listing
        + more
        + f"\n\nБудет задействовано <b>{len(phones)} из {max_active}</b> доступных слотов.\n"
        f"Отправьте число секунд от <b>10</b> до <b>{max_duration}</b> (применится ко всем):\n\n"
        f"{dur_subtext}{hint}"
        + note,
        footer=f"{e('info')} Отправьте сообщение с числом секунд в чат",
    )

async def open_order(cb: CallbackQuery, state: FSMContext) -> None:
    await safe_cb_answer(cb)
    user_id = cb.from_user.id
    if await db.is_user_banned(user_id):
        await say(bot, cb.message.chat.id, ban_text())
        return
    if not await ensure_subscribed(cb):
        return
    try:
        is_vip = await db.check_user_exists_privilege(user_id)
        daily_limit = config.vip_daily_limit if is_vip else config.default_daily_limit
        daily_used = await db.get_daily_attacks(user_id)
        if daily_used >= daily_limit:
            await show(cb.message, _daily_limit_text(is_vip, daily_limit), back_kb())
            return
    except Exception:
        pass
    await state.clear()
    await state.set_state(AttackState.waiting_phone)
    max_active, max_duration = await resolve_limits(user_id)
    order_table = table(
        ["Параметр", "Условия запуска"],
        [
            ["Направления", f"{e('ru')} Россия (+7) и {e('ua')} Украина (+380)"],
            ["Ввод номеров", "1 номер или список (через запятую / новую строку)"],
            ["Длительность", f"от 10 сек до {fmt_dur(max_duration)} ({max_duration}с)"],
            ["Лимит слотов", f"до {max_active} активных параллельно"],
        ],
    )
    caption = (
        h(f"{e('sword')} Запуск стресс-теста")
        + quote("Отправьте целевой номер телефона сообщением в чат для проверки устойчивости к входящему потоку:")
        + order_table
        + f"\n<footer>{e('info')} Формат: +79123456789 или +380501234567 · Следующим шагом укажите секунды</footer>"
    )
    await show(cb.message, caption, back_kb(), sticker=config.sticker_order)

async def _start_attack(
    user_id: int,
    phone: str,
    duration: int,
    has_privilege: bool,
    max_duration: int,
    message: Message,
    *,
    cancel_epoch: int | None = None,
) -> tuple[bool, str]:
    if cancel_epoch is not None and runtime.cancel_epoch(user_id) != cancel_epoch:
        return False, "cancelled"
    ack = await say(
        bot,
        message.chat.id,
        card(
            f"{e('wait')} Принял в работу",
            f"Подготовка стресс-теста для <code>{esc(display_phone(phone))}</code>…\n\n"
            "<i>Калибровка шлюзов связи и распределение потоков по сервисам.</i>",
            footer=f"{e('bolt')} Инициализация сессии...",
        ),
    )
    try:
        outcome = await runtime.launch(
            user_id=user_id,
            phone=phone,
            duration=duration,
            has_privilege=has_privilege,
            card=ack,
            chat_id=message.chat.id,
            cancel_epoch=cancel_epoch,
        )
    except DailyLimitReached:
        try:
            is_vip = await db.check_user_exists_privilege(user_id)
        except Exception:
            is_vip = has_privilege
        daily_lim = config.vip_daily_limit if is_vip else config.default_daily_limit
        await _edit_launch_card(ack, message, _daily_limit_text(is_vip, daily_lim))
        return False, "daily_limit"
    except DurationLimitReached:
        dur_msg = (
            f"Запрошенная длительность превышает допустимый лимит.\n\n"
            f"• Ваш лимит сессии: <b>{max_duration} сек ({fmt_dur(max_duration)})</b>\n"
        )
        if not has_privilege:
            dur_msg += f"• VIP увеличивает лимит до <b>2 часов ({fmt_dur(config.vip_max_duration)})</b>\n\n"
        dur_msg += "Пожалуйста, отправьте допустимое число секунд."
        await _edit_launch_card(ack, message, card(f"{e('warn')} Превышена длительность", dur_msg))
        return False, "duration_limit"
    except AttackLimitReached:
        await _edit_launch_card(ack, message, _failure_text({"limit"}))
        return False, "limit"
    except ServerBusy:
        await _edit_launch_card(ack, message, _failure_text({"busy"}))
        return False, "busy"
    except LaunchFailed as exc:
        logger.warning("attack launch failed for +%s: %s", phone, exc)
        await _edit_launch_card(ack, message, _failure_text({"engine"}))
        return False, "engine"
    except Exception:
        logger.exception("attack launch crashed for +%s", phone)
        await _edit_launch_card(ack, message, _failure_text({"engine"}))
        return False, "engine"

    end_time = (
        datetime.datetime.now(MSK) + datetime.timedelta(seconds=duration)
    ).strftime("%H:%M:%S МСК")
    started = attack_started_text(
        display_phone(phone),
        duration,
        end_time,
        speed_tag(has_privilege, max_duration),
        services_count=outcome.services_count,
        is_vip=outcome.is_vip,
    )
    if ack is not None:
        await show(ack, started, stop_kb(outcome.attack_id))
    else:
        await say(bot, message.chat.id, started, stop_kb(outcome.attack_id))
    return True, ""

async def _edit_launch_card(
    ack: Message | None, message: Message, text: str
) -> None:
    if ack is not None:
        await show(ack, text, back_kb())
    else:
        await say(bot, message.chat.id, text, back_kb())

def _failure_text(reasons: set[str]) -> str:
    if "daily_limit" in reasons:
        return card(
            f"{e('hourglass')} Суточный лимит исчерпан",
            f"На сегодня все доступные запуски израсходованы. Счётчик квот обновится в начале новых суток (00:00 МСК).\n\n"
            f"Для увеличения суточного лимита до {config.vip_daily_limit} запусков доступен недорогой тариф VIP.",
            footer=f"{e('clock')} Сброс счетчика: 00:00 МСК",
        )
    if "duration_limit" in reasons:
        return card(
            f"{e('warn')} Превышена длительность",
            f"Запрошенная длительность сессии превышает разрешённый лимит сервиса.\n\n"
            f"▫️ <b>Базовый тариф:</b> до {fmt_dur(config.default_max_duration)} ({config.default_max_duration} сек)\n"
            f"▫️ <b>Тариф VIP:</b> до {fmt_dur(config.vip_max_duration)} ({config.vip_max_duration} сек)\n\n"
            "Пожалуйста, отправьте допустимое число секунд.",
            footer=f"{e('info')} Укажите число секунд в пределах лимита",
        )
    if "busy" in reasons:
        return card(
            f"{e('hourglass')} Сервер временно перегружен",
            f"В данный момент выполняется максимальный пул ({config.max_attacks} параллельных сессий).\n\n"
            "Все свободные потоки заняты. Пожалуйста, подождите 1–2 минуты и повторите запуск — введённые цели сохранены.",
            footer=f"{e('info')} Повторите отправку секунд через 1-2 минуты",
        )
    if "limit" in reasons:
        return card(
            f"{e('warn')} Слот не освободился",
            "У вас уже выполняется максимально допустимое число одновременных сессий.\n\n"
            "Дождитесь окончания активной сессии либо остановите её вручную.",
            footer=f"{e('info')} Освободите слот для нового запуска",
        )
    return card(
        f"{e('no')} Запуск не удался",
        "Серверу не удалось инициализировать сессию для указанного номера.\n\n"
        "Номер и параметры сохранены. Попробуйте отправить длительность ещё раз.",
        footer=f"{e('info')} Повторите отправку параметров",
    )

async def _launch_batch(
    message: Message,
    state: FSMContext,
    phones: list[str],
    duration: int,
    has_privilege: bool,
    max_active: int,
    max_duration: int,
) -> None:
    user_id = message.from_user.id
    try:
        is_vip = await db.check_user_exists_privilege(user_id)
    except Exception:
        is_vip = has_privilege
    daily_limit = config.vip_daily_limit if is_vip else config.default_daily_limit
    try:
        daily_used = await db.get_daily_attacks(user_id)
    except Exception:
        daily_used = 0
    daily_left = max(0, daily_limit - daily_used)

    if daily_left <= 0:
        await state.clear()
        await say(bot, message.chat.id, _daily_limit_text(is_vip, daily_limit), back_kb())
        return

    free = max_active - runtime.active_for(user_id)
    free = min(free, max(0, config.max_attacks - runtime.total_active()))
    if free <= 0:
        if len(phones) == 1:
            await state.clear()
            await say(bot, message.chat.id, _no_slots_text(has_privilege, max_active), back_kb())
            return
        await state.update_data(phones=phones)
        await state.set_state(AttackState.waiting_duration)
        await say(
            bot,
            message.chat.id,
            _no_slots_text(has_privilege, max_active)
            + f"\n<blockquote>{e('doc')} В очереди осталось {len(phones)}. "
            "Когда слот освободится, пришли секунды ещё раз — номера не нужно вводить заново.</blockquote>",
            back_kb(),
        )
        return

    slots_to_run = min(free, daily_left)
    to_run, leftover = phones[:slots_to_run], phones[slots_to_run:]
    launched: list[str] = []
    failed: list[str] = []
    reasons: set[str] = set()
    pending: list[str] = []
    cancelled = False
    epoch = runtime.cancel_epoch(user_id)

    for index, phone in enumerate(to_run):
        if runtime.cancel_epoch(user_id) != epoch:
            pending = to_run[index:]
            cancelled = True
            break
        ok, reason = await _start_attack(
            user_id,
            phone,
            duration,
            has_privilege,
            max_duration,
            message,
            cancel_epoch=epoch,
        )
        if ok:
            launched.append(phone)
        else:
            failed.append(phone)
            reasons.add(reason)
        await asyncio.sleep(0.05)
        if runtime.cancel_epoch(user_id) != epoch:
            pending = to_run[index + 1 :]
            cancelled = True
            break

    if cancelled:
        try:
            await runtime.stop_user(user_id)
        except Exception as exc:
            logger.warning("user %s: batch cancel cleanup failed: %r", user_id, exc)

    pending = failed + pending + leftover
    if pending:
        if "daily_limit" in reasons or (daily_left - len(launched) <= 0):
            await state.clear()
            if len(phones) == 1:
                return
            await say(
                bot,
                message.chat.id,
                card(
                    f"{e('hourglass')} Суточный лимит исчерпан",
                    f"Запущено {len(launched)}. В очереди оставалось {len(pending)}, но "
                    f"исчерпан суточный лимит ({daily_limit} из {daily_limit}).\n"
                    "Счётчик обновится в начале новых суток (00:00 МСК).",
                ),
                back_kb(),
            )
            return

        await state.update_data(phones=pending)
        await state.set_state(AttackState.waiting_duration)
        if cancelled:
            await say(
                bot,
                message.chat.id,
                card(
                    f"{e('pause')} Остановлено",
                    f"Запущено {len(launched)}. В очереди осталось {len(pending)} — "
                    "пришли секунды ещё раз, номера сохранены.",
                ),
                back_kb(),
            )
            return
        if len(phones) == 1:
            return
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('wait')} Часть не встала",
                f"Запустил {len(launched)} из {min(len(phones), max_active)} доступных слотов.\n"
                f"В очереди ещё {len(pending)}. Пришли секунды ещё раз — "
                "эти номера уже сохранены.",
            ),
            back_kb(),
        )
    else:
        await state.clear()

    if not launched:
        if len(phones) > 1:
            await say(bot, message.chat.id, _failure_text(reasons), back_kb())
        return
    if len(phones) == 1:
        return

    lines = [
        f"{_flag(phone)} <code>{esc(display_phone(phone))}</code>" for phone in launched[:20]
    ]
    extra = f"\nи ещё {len(launched) - 20}" if len(launched) > 20 else ""
    failed_line = f"\n{e('warn')} не стартовали: {len(failed)}" if failed else ""
    summary = card(
        f"{e('bolt')} Запущено {len(launched)}",
        "\n".join(lines) + extra + failed_line,
        footer=f"{e('clock')} каждая идёт {fmt_dur(duration)}. Остановить можно в её карточке.",
    )
    await say(bot, message.chat.id, summary)

async def get_phone(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    if await db.is_user_banned(user_id):
        await state.clear()
        await say(bot, message.chat.id, ban_text())
        return
    if not await ensure_subscribed(message):
        return

    try:
        is_vip = await db.check_user_exists_privilege(user_id)
        daily_limit = config.vip_daily_limit if is_vip else config.default_daily_limit
        daily_used = await db.get_daily_attacks(user_id)
        if daily_used >= daily_limit:
            await state.clear()
            await say(bot, message.chat.id, _daily_limit_text(is_vip, daily_limit), back_kb())
            return
    except Exception:
        pass

    raw_text = (message.text or "").strip()
    if not raw_text:
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('warn')} Пустое сообщение",
                "Пожалуйста, пришлите номер текстом (не изображением и не контактом).\n\n" + phone_hint,
            ),
            back_kb(),
        )
        return

    candidates = split_phone_candidates(raw_text)
    valid, skipped = _classify(raw_text)
    if not valid:
        if len(candidates) == 1:
            phone = normalize_phone(candidates[0])
            if phone and is_fake_number(phone):
                text = card(
                    f"{e('no')} Недопустимый номер",
                    f"Номер <code>{esc(candidates[0])}</code> распознан как тестовый шаблон (повторяющиеся или фиктивные цифры).\n\n"
                    "Пожалуйста, укажите реальный мобильный номер телефона.\n\n"
                    + phone_hint,
                )
            else:
                text = card(
                    f"{e('no')} Неверный формат номера",
                    f"Не удалось распознать номер <code>{esc(candidates[0])}</code>.\n\n"
                    "Сервис поддерживает мобильные номера России (+7) и Украины (+380).\n\n"
                    + phone_hint,
                )
        else:
            text = card(
                f"{e('no')} Номера не распознаны",
                "Не удалось определить ни одного корректного номера.\n\n"
                "Отправьте каждый номер с новой строки либо через запятую:\n"
                "<code>+79163845120\n+380673920382</code>\n\n" + phone_hint,
            )
        await say(bot, message.chat.id, text, back_kb())
        return

    max_active, max_duration = await resolve_limits(user_id)
    valid, protected = await _filter_protected(valid)
    if not valid:
        if len(candidates) == 1:
            text = card(
                f"{e('shield')} Номер под защитой",
                f"Номер <code>{esc(display_phone(candidates[0]))}</code> внесён в стоп-лист защиты сервиса.\n\n"
                "Запуск стресс-теста на защищённые номера заблокирован.",
                footer=f"{e('crown')} Пользователи VIP могут защитить свой номер в разделе «Защита»",
            )
        else:
            text = card(
                f"{e('shield')} Все номера под защитой",
                "Все указанные номера находятся в стоп-листе защиты сервиса.\n\n"
                "Запуск стресс-теста на эти номера заблокирован. Пожалуйста, укажите другие цели.",
            )
        await say(bot, message.chat.id, text, back_kb())
        return

    await state.update_data(phones=valid)
    await state.set_state(AttackState.waiting_duration)
    await say(
        bot,
        message.chat.id,
        _duration_prompt(valid, max_active, max_duration, _filter_note(skipped, protected)),
        back_kb(),
    )

async def get_duration(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    if await db.is_user_banned(user_id):
        await state.clear()
        await say(bot, message.chat.id, ban_text())
        return
    if not await ensure_subscribed(message):
        return

    is_vip = False
    try:
        is_vip = await db.check_user_exists_privilege(user_id)
        daily_limit = config.vip_daily_limit if is_vip else config.default_daily_limit
        daily_used = await db.get_daily_attacks(user_id)
        if daily_used >= daily_limit:
            await state.clear()
            await say(bot, message.chat.id, _daily_limit_text(is_vip, daily_limit), back_kb())
            return
    except Exception:
        pass

    max_active, max_duration = await resolve_limits(user_id)
    override = None
    try:
        override = await db.get_user_override(user_id)
    except Exception:
        override = None
    has_privilege = bool(
        is_vip
        or max_duration > config.default_max_duration
        or override is not None
    )

    if runtime.total_active() >= config.max_attacks:
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('hourglass')} Сервер временно перегружен",
                f"Сейчас выполняется максимум — {config.max_attacks} атак на всех пользователей сервиса.\n\n"
                "Пожалуйста, подождите 1–2 минуты и отправьте число секунд ещё раз (номера сохранены в памяти).",
            ),
            back_kb(),
        )
        return

    try:
        duration = int((message.text or "").strip())
        if duration < 10 or duration > max_duration:
            raise ValueError
    except (ValueError, AttributeError):
        dur_msg = (
            f"Некорректное значение длительности: <code>{esc(message.text or '')}</code>.\n\n"
            f"<b>Требования:</b>\n"
            f"• Отправьте целое число секунд от <b>10</b> до <b>{max_duration}</b>\n"
            f"• Ваш текущий максимум: <b>{max_duration} сек ({fmt_dur(max_duration)})</b>\n"
        )
        if not has_privilege:
            dur_msg += f"\n{e('crown')} <i>VIP увеличивает лимит до {fmt_dur(config.vip_max_duration)} ({config.vip_max_duration} сек).</i>"
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('warn')} Неверная длительность",
                dur_msg,
                footer=f"{e('info')} Отправьте сообщение с целым числом секунд (например: 30)",
            ),
            back_kb(),
        )
        return

    phones = list((await state.get_data()).get("phones") or [])
    if not phones:
        await state.clear()
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('warn')} Данные сессии устарели",
                "Время ожидания ввода истекло или диалог был сброшен.\n\n"
                "Пожалуйста, нажмите «Запустить стресс-тест» и отправьте номер заново.",
            ),
            back_kb(),
        )
        return

    allowed, _protected = await _filter_protected(phones)
    if not allowed:
        await state.clear()
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('shield')} Номер добавлен в защиту",
                "Пока настраивалась сессия, указанный номер был добавлен в стоп-лист защиты. Запуск отменён.",
            ),
            back_kb(),
        )
        return

    await _launch_batch(
        message, state, allowed, duration, has_privilege, max_active, max_duration
    )

async def stop_attack(cb: CallbackQuery) -> None:
    if await db.is_user_banned(cb.from_user.id):
        await safe_cb_answer(cb, "Аккаунт заблокирован.", show_alert=True)
        await runtime.stop_user(cb.from_user.id)
        return
    try:
        attack_id = int(cb.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await safe_cb_answer(cb, "Неверный идентификатор.", show_alert=True)
        return

    stopped = await runtime.stop(cb.from_user.id, attack_id)
    if not stopped:
        await safe_cb_answer(cb, "Уже не идёт.", show_alert=True)
        return

    shown = await show(
        cb.message,
        card(
            f"{e('pause')} Стресс-тест остановлен",
            "Сессия стресс-тестирования принудительно прервана по вашему требованию.\n\n"
            "• Отправка новых запросов прекращена\n"
            "• Слот освобождён для новых запусков",
            footer=f"{e('clock')} {datetime.datetime.now(MSK).strftime('%H:%M:%S МСК')}",
        ),
        back_kb(),
    )
    if shown is None:
        await safe_cb_answer(cb, "Остановлено", show_alert=True)
        return
    await safe_cb_answer(cb, "Остановлено")

def register_attack(dp: Dispatcher) -> None:
    dp.callback_query.register(open_order, F.data == "order")
    dp.message.register(get_phone, AttackState.waiting_phone)
    dp.message.register(get_duration, AttackState.waiting_duration)
    dp.callback_query.register(stop_attack, F.data.startswith("stop:"))

async def profile(cb: CallbackQuery) -> None:
    if not await ensure_subscribed(cb):
        return
    user_id = cb.from_user.id
    stats = await db.get_user_stats(user_id)
    if not stats:
        await safe_cb_answer(cb, "Профиль не найден.", show_alert=True)
        return

    first_name, username, registration_date, total_attacks = stats
    active_count = runtime.active_for(user_id)
    is_vip = await db.check_user_exists_privilege(user_id)
    daily_limit = config.vip_daily_limit if is_vip else config.default_daily_limit
    try:
        daily_used = await db.get_daily_attacks(user_id)
    except Exception:
        daily_used = 0
    protected = await db.get_protected_by_user(user_id)
    override = await db.get_user_override(user_id)
    if override:
        limit_now, limit_time = override[0], override[1]
        limit_note = "индивидуальные"
    elif is_vip:
        limit_now, limit_time = config.vip_max_active, config.vip_max_duration
        limit_note = "VIP-тариф"
    else:
        limit_now, limit_time = config.default_max_active, config.default_max_duration
        limit_note = "базовый тариф"
    prot = (
        ", ".join(f"<code>{esc(display_phone(row[0]))}</code>" for row in protected)
        if protected
        else "Нет"
    )
    if is_vip:
        exp = await db.get_user_privilege_expiry(user_id)
        vip_status = f"VIP (до {format_dt(exp)})" if exp else "VIP (активен)"
    else:
        vip_status = "Базовый"
    text = (
        h(f"{e('user')} Профиль пользователя")
        + quote("Сводка по параметрам аккаунта, установленным лимитам и статистике тестирования.")
        + person_fields(
            user_id,
            first_name,
            username,
            since=format_dt(registration_date),
            extra=[
                ("Статус", vip_status),
                ("Параллельные сессии", f"{limit_now} слотов ({limit_note})"),
                ("Длительность сессии", f"до {fmt_dur(limit_time)} ({limit_time} сек)"),
                ("Суточный лимит", f"{daily_used} из {daily_limit} (сброс в 00:00 МСК)"),
                ("Скорость отправки", "В 2 раза выше (х2)" if is_vip else "Базовая"),
                ("Активные сейчас", f"{active_count} в работе" if active_count else "Нет активных тестов"),
                ("Всего запусков", str(total_attacks)),
                ("Защита своего номера", prot),
            ],
        )
    )
    if not is_vip:
        text += (
            f"\n<footer>{e('crown')} Доступный VIP: {config.vip_max_active} слотов, "
            f"до {fmt_dur(config.vip_max_duration)}, {config.vip_daily_limit} запусков/день, "
            f"скорость увеличена в 2 раза и защита номера · {config.vip_price} звёзд / {config.xrocket_vip_price_usdt}$ в месяц</footer>"
        )
    await show(cb.message, text, back_kb(), sticker=config.sticker_profile)
    await safe_cb_answer(cb)

async def history(cb: CallbackQuery) -> None:
    if not await ensure_subscribed(cb):
        return
    attacks = await db.get_attack_history(cb.from_user.id, limit=10)
    if not attacks:
        await safe_cb_answer(cb, "История запусков пуста.", show_alert=True)
        return
    await safe_cb_answer(cb)
    rows = []
    for index, (phone, duration, timestamp, stopped) in enumerate(attacks, start=1):
        flag = e("ua") if phone_country(phone) == "UA" else e("ru")
        try:
            moment = datetime.datetime.fromisoformat(timestamp)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            short_dt = moment.astimezone(MSK).strftime("%d.%m %H:%M")
        except Exception:
            short_dt = esc((timestamp or "")[:16])
        status = "Остановлена" if stopped else "Штатно"
        status_icon = e("pause") if stopped else e("ok")
        rows.append([
            f"{index}. {flag} <code>{esc(phone)}</code>",
            f"{fmt_dur(duration)} · {short_dt} ({status_icon} {status})",
        ])
    text = (
        h(f"{e('doc')} История последних тестов")
        + quote(f"Отображаются последние {len(attacks)} сессий стресс-тестирования (начиная с самых свежих).")
        + table(["Номер цели", "Параметры сессии"], rows)
        + f"\n<footer>{e('info')} {e('ok')} Завершена штатно · {e('pause')} Остановлена вручную</footer>"
    )
    await show(cb.message, text, back_kb(), sticker=config.sticker_history)

async def protect_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_cb_answer(cb)
    user_id = cb.from_user.id
    if await db.is_user_banned(user_id):
        await say(bot, cb.message.chat.id, ban_text())
        return
    if not await ensure_subscribed(cb):
        return

    is_vip = await db.check_user_exists_privilege(user_id)
    protected = await db.get_protected_by_user(user_id)

    if not is_vip:
        text = card(
            f"{e('shield')} Персональная защита номера",
            "Внесение личного номера в стоп-лист полностью защищает его от стресс-тестирования через наш сервис: "
            "все попытки запуска на защищённый номер мгновенно отклоняются.\n\n"
            "Функция персональной защиты входит в доступный пакет VIP бесплатно.",
            footer=f"{e('crown')} Доступно с VIP · {config.vip_price} звёзд или {config.xrocket_vip_price_usdt} USDT (в месяц)",
        )
        keyboard = protect_kb(False, False)
    elif protected:
        rows = [
            [
                f"{idx}. <code>{esc(display_phone(phone))}</code>" if len(protected) > 1 else f"<code>{esc(display_phone(phone))}</code>",
                format_dt(added),
            ]
            for idx, (phone, added) in enumerate(protected, start=1)
        ]
        text = (
            h(f"{e('shield')} Номер под защитой")
            + quote("Все попытки запуска стресс-теста на этот номер через платформу отклоняются.")
            + table(["Защищённый номер", "Дата добавления"], rows)
            + f"\n<footer>{e('info')} Для снятия защиты используйте кнопку ниже</footer>"
        )
        keyboard = protect_kb(True, True)
    else:
        text = card(
            f"{e('shield')} Персональная защита номера",
            "Вы можете внести один личный номер в глобальный стоп-лист сервиса. Все запуски на него будут отклоняться системой.\n\n"
            + phone_hint,
            footer=f"{e('info')} Одновременно можно защитить 1 личный номер",
        )
        keyboard = protect_kb(True, False)

    await show(cb.message, text, keyboard, sticker=config.sticker_protect)

async def protect_add(cb: CallbackQuery, state: FSMContext) -> None:
    await safe_cb_answer(cb)
    user_id = cb.from_user.id
    if not await db.check_user_exists_privilege(user_id):
        await say(
            bot,
            cb.message.chat.id,
            card(
                f"{e('crown')} Требуется VIP",
                f"Управление стоп-листом доступно после активации VIP: {config.vip_price} звёзд / {config.xrocket_vip_price_usdt}$ в месяц.",
            ),
            back_kb(),
        )
        return
    existing = await db.get_protected_by_user(user_id)
    if existing:
        await say(
            bot,
            cb.message.chat.id,
            card(
                f"{e('shield')} Лимит защиты исчерпан",
                f"Уже защищён номер: <code>{esc(display_phone(existing[0][0]))}</code>\n\n"
                "Сначала снимите текущую защиту — одновременно можно защитить 1 номер.",
            ),
            back_kb(),
        )
        return
    await state.set_state(ProtectState.waiting_phone)
    await show(
        cb.message,
        card(
            f"{e('shield')} Добавление номера в защиту",
            "Отправьте целевой номер телефона сообщением в чат. После внесения в базу все попытки стресс-теста на него будут отклоняться.\n\n"
            + phone_hint,
            footer=f"{e('info')} Отправьте номер в чат",
        ),
        back_kb(),
        sticker=config.sticker_protect,
    )

async def protect_remove(cb: CallbackQuery, state: FSMContext) -> None:
    await safe_cb_answer(cb)
    user_id = cb.from_user.id
    if not await db.check_user_exists_privilege(user_id):
        await say(
            bot,
            cb.message.chat.id,
            card(f"{e('crown')} Требуется VIP", "Стоп-лист доступен пользователям с VIP-тарифом."),
            back_kb(),
        )
        return
    protected = await db.get_protected_by_user(user_id)
    if not protected:
        await say(
            bot,
            cb.message.chat.id,
            card(f"{e('info')} Список пуст", "Защищённого номера нет в реестре."),
            back_kb(),
        )
        return
    for row in protected:
        await db.remove_protected_number(row[0])
    phone = protected[0][0]
    await show(
        cb.message,
        card(
            f"{e('ok')} Защита снята",
            f"Номер <code>{esc(display_phone(phone))}</code> успешно удалён из стоп-листа платформы.",
            footer=f"{e('info')} Номер разблокирован для тестирования",
        ),
        back_kb(),
        sticker=config.sticker_protect,
    )

async def protect_waiting_phone(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    if await db.is_user_banned(user_id):
        await state.clear()
        await say(bot, message.chat.id, ban_text())
        return
    if not await db.check_user_exists_privilege(user_id):
        await state.clear()
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('crown')} Только с VIP",
                f"Стоп-лист открывается после оплаты: {config.vip_price} звёзд / {config.xrocket_vip_price_usdt}$, один раз.",
            ),
            back_kb(),
        )
        return

    phone = normalize_phone((message.text or "").strip())
    if not phone:
        await say(
            bot,
            message.chat.id,
            card(f"{e('no')} Не разобрал номер", phone_hint),
            back_kb(),
        )
        return
    if is_fake_number(phone):
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('no')} Номер ненастоящий",
                "Похоже на шаблон. Пришли реальный номер телефона.\n\n" + phone_hint,
            ),
            back_kb(),
        )
        return
    existing = await db.get_protected_by_user(user_id)
    if existing:
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('warn')} Уже закрыт один номер",
                f"<code>{esc(display_phone(existing[0][0]))}</code>\nСначала снимите защиту с него.",
            ),
            back_kb(),
        )
        await state.clear()
        return

    added = await db.add_protected_number(phone, user_id)
    await state.clear()
    if added:
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('shield')} Номер закрыт",
                f"<code>{esc(display_phone(phone))}</code>\n"
                "Стресс-тестирование на него теперь отклоняется системой, в том числе для чужих запусков.",
            ),
            back_kb(),
        )
    else:
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('warn')} Не удалось защитить номер",
                "Не удалось установить защиту на данный номер. Проверьте правильность номера или обратитесь в поддержку.",
            ),
            back_kb(),
        )

def register_profile(dp: Dispatcher) -> None:
    dp.callback_query.register(profile, F.data == "profile")
    dp.callback_query.register(history, F.data == "history")
    dp.callback_query.register(protect_menu, F.data == "protect")
    dp.callback_query.register(protect_add, F.data == "protect_add")
    dp.callback_query.register(protect_remove, F.data == "protect_remove")
    dp.message.register(protect_waiting_phone, ProtectState.waiting_phone)

PER_PAGE = 10

async def admin_panel_text() -> str:
    total_users, total_privileged, total_banned, total_protected, total_attacks, total_started = (
        await db.dashboard_stats()
    )
    active_now = runtime.total_active()
    tpl_count = len(manager.templates) if "manager" in globals() and hasattr(manager, "templates") else 0
    try:
        import browser
        b_count = len(getattr(browser, "SERVICES", {}))
    except Exception:
        b_count = 0
    total_services = tpl_count + b_count
    services_val = (
        f"{total_services} ({tpl_count} API · {b_count} браузер)"
        if b_count
        else str(total_services)
    )
    return (
        h(f"{e('gear')} Панель администратора")
        + table(
            ["Параметр", "Значение"],
            [
                [f"{e('user')} Пользователи", str(total_users)],
                [f"{e('crown')} Тариф VIP", str(total_privileged)],
                [f"{e('ban')} Блокировки", str(total_banned)],
                [f"{e('shield')} Стоп-лист", str(total_protected)],
                [f"{e('chart')} Всего сессий", str(total_attacks)],
                [f"{e('bolt')} Активно сейчас", str(active_now)],
                [f"{e('ok')} Успешных стартов", str(total_started)],
                [f"{e('doc')} Сервисы шлюзов", services_val],
            ],
        )
    )

async def _check_cb(cb: CallbackQuery) -> bool:
    if not is_admin(cb.from_user.id):
        await safe_cb_answer(cb, "Нет доступа.", show_alert=True)
        return False
    return True

async def _check_msg(message: Message) -> bool:
    if not is_admin(message.from_user.id):
        await say(bot, message.chat.id, card(f"{e('ban')} Нет доступа", "Эта команда только для админов."))
        return False
    return True

async def _resolve_user_id(message: Message) -> int | None:
    text = (message.text or "").strip()
    if not text:
        await say(bot, message.chat.id, card(f"{e('warn')} Пусто", "Пришли ID или @username."))
        return None
    if text.startswith("@"):
        row = await db.get_user_by_username(text)
        if not row:
            await say(
                bot,
                message.chat.id,
                card(f"{e('no')} Не найден", f"{esc(text)} нет в базе. Нужен точный @username."),
            )
            return None
        return int(row[0])
    if text.isdigit():
        return int(text)
    row = await db.get_user_by_username(text)
    if row:
        return int(row[0])
    await say(
        bot,
        message.chat.id,
        card(
            f"{e('info')} Не понял",
            "Нужен числовой ID (<code>123456</code>) или @username.",
        ),
    )
    return None

def _page_of(cb: CallbackQuery) -> int:
    try:
        return int(cb.data.split(":", 1)[1])
    except (AttributeError, IndexError, ValueError):
        return 1

def _pages(page: int, total: int) -> tuple[int, int]:
    total_pages = max(1, math.ceil(total / PER_PAGE)) if total else 1
    return max(1, min(page, total_pages)), total_pages

def _paged_kb(
    page: int, total_pages: int, prefix: str, kb: InlineKeyboardBuilder | None = None
) -> InlineKeyboardBuilder:
    kb = kb or InlineKeyboardBuilder()
    kb.row(btn(f"{page} / {total_pages}", f"{prefix}:noop"))
    nav = []
    if page > 1:
        nav.append(btn("« Назад", f"{prefix}:{page - 1}"))
    if page < total_pages:
        nav.append(btn("Дальше ›", f"{prefix}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(btn("« В панель", "admin_open"))
    return kb

def vip_granted_notification_text(invite_link: str | None = None) -> str:
    rows = [
        ["Статус", "VIP (30 дней)"],
        ["Сервисы", "Все (UA / RU) + Browser"],
        ["Одновременно", f"до {config.vip_max_active} слотов"],
        ["Длительность", f"до {fmt_dur(config.vip_max_duration)} ({config.vip_max_duration} сек)"],
        ["В сутки", f"{config.vip_daily_limit} запусков"],
        ["Скорость", "Увеличена в 2 раза (х2)"],
        ["Защита номера", "Бесплатно в разделе «Защита»"],
    ]
    community_html = ""
    if invite_link:
        community_html = (
            f"\n\n{e('spark')} <b>Приватное VIP-сообщество:</b>\n"
            f"Ваша индивидуальная ссылка в закрытый форум с новостями и обновлениями:\n"
            f"👉 <a href=\"{invite_link}\">{invite_link}</a>\n"
            f"<i>(Ссылка одноразовая и выключается сразу после входа)</i>"
        )
    return (
        h(f"{e('crown')} Вам выдан VIP-статус")
        + quote(
            "Администратор активировал для вашего аккаунта <b>VIP-доступ на 30 дней</b>.\n"
            "Вам открыты расширенные лимиты, стабильная скорость х2 и полный пул сервисов."
        )
        + table(["Параметр", "Значение"], rows)
        + community_html
        + f"\n<footer>{e('spark')} VIP на 30 дней активирован</footer>"
    )

def _note(*lines: str) -> str:
    body = "\n".join(line for line in lines if line)
    return f"\n\n{body}" if body else ""

async def _grant_vip(user_id: int) -> str:
    granted = await db.add_user_privilege(user_id)
    display = await user_display(
        user_id,
        extra=[
            ("Одновременно", f"до {config.vip_max_active}"),
            ("Одна атака", f"до {fmt_dur(config.vip_max_duration)}"),
            ("В сутки", f"{config.vip_daily_limit}"),
        ],
    )
    if not granted:
        return h(f"{e('crown')} VIP уже был") + quote("Платить и выдавать второй раз не нужно.") + display

    try:
        bot_inst = bot
        if bot_inst is not None and hasattr(bot_inst, "send_message"):
            invite_link = await issue_vip_community_link(bot_inst, user_id)
            await say(
                bot_inst,
                user_id,
                vip_granted_notification_text(invite_link),
                vip_activated_kb(invite_link),
            )
    except Exception as exc:
        logger.warning("Не удалось отправить уведомление о VIP пользователю %s: %s", user_id, exc)

    return (
        h(f"{e('crown')} VIP выдан")
        + quote("Готово. Лимиты действуют 30 дней. Пользователю отправлено уведомление.")
        + display
    )

async def _revoke_vip(user_id: int) -> str:
    removed = await db.remove_user_privilege(user_id)
    display = await user_display(user_id)
    if removed:
        return (
            h(f"{e('no')} VIP снят")
            + quote("Дальше у него базовые лимиты. Звёзды сами не вернутся.")
            + display
        )
    return h(f"{e('info')} VIP и не было") + quote("Снимать нечего.") + display

async def _set_ban(user_id: int) -> str:
    existed = await db.user_exists(user_id)
    await db.ban_user(user_id)
    stopped = await runtime.stop_user(user_id)
    notes = []
    if not existed:
        notes.append("В базе его не было — бан всё равно записан, /start закроется.")
    if stopped:
        notes.append("Активные атаки остановлены.")
    return (
        h(f"{e('ban')} Забанен")
        + quote("Бот ему больше не отвечает.")
        + await user_display(user_id)
        + _note(*notes)
    )

async def _set_unban(user_id: int) -> str:
    was_banned = await db.is_user_banned(user_id)
    await db.unban_user(user_id)
    display = await user_display(user_id)
    if was_banned:
        return h(f"{e('ok')} Разбанен") + quote("Снова может зайти через /start.") + display
    return h(f"{e('info')} И не был забанен") + quote("Менять нечего.") + display

@dataclass(frozen=True, slots=True)
class Action:
    prompt: str
    apply: Callable[[int], Awaitable[str]]
    require_existing: bool = False
    admin_block: str = ""

ACTIONS: dict[str, Action] = {
    "grant": Action(
        prompt=card(
            f"{e('crown')} Выдать VIP",
            "Пришли ID (<code>123456</code>) или @username. "
            "Человек уже должен быть в базе — иначе сначала пусть нажмёт /start, "
            "либо выдай VIP из его карточки в списке.",
        ),
        apply=_grant_vip,
        require_existing=True,
    ),
    "revoke": Action(
        prompt=card(
            f"{e('no')} Забрать VIP",
            "ID или @username. Базовые лимиты вернутся сразу, оплаченные звёзды не возвращаются.",
        ),
        apply=_revoke_vip,
        require_existing=True,
    ),
    "ban": Action(
        prompt=card(
            f"{e('ban')} Бан",
            "ID или @username. Бот перестанет отвечать, активные атаки остановятся.",
        ),
        apply=_set_ban,
        admin_block="Админа банить нельзя.",
    ),
    "unban": Action(
        prompt=card(
            f"{e('ok')} Разбан",
            "ID или @username. После этого /start снова откроет меню.",
        ),
        apply=_set_unban,
    ),
}

async def admin_panel(message: Message, state: FSMContext) -> None:
    if not await _check_msg(message):
        return
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await say(bot, message.chat.id, await admin_panel_text(), admin_kb(), sticker=config.sticker_admin)

async def admin_open(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _check_cb(cb):
        return
    await state.clear()
    await safe_cb_answer(cb)
    await show(cb.message, await admin_panel_text(), admin_kb(), sticker=config.sticker_admin)

async def admin_close(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _check_cb(cb):
        return
    await state.clear()
    await safe_cb_answer(cb)
    await cb.message.delete()

async def admin_stats(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    await safe_cb_answer(cb)
    total_users, vip, banned, protected, attacks, _ = await db.dashboard_stats()
    active_now = runtime.total_active()
    top = await db.get_top_attackers(limit=5)
    lines = [
        person_fields(
            user_id,
            first_name,
            username,
            extra=[("Атак", str(count))],
        )
        for user_id, first_name, username, count in top
    ]
    share = round(vip / total_users * 100) if total_users else 0
    tpl_count = len(manager.templates) if "manager" in globals() and hasattr(manager, "templates") else 0
    try:
        import browser
        b_count = len(getattr(browser, "SERVICES", {}))
    except Exception:
        b_count = 0
    total_services = tpl_count + b_count
    services_val = (
        f"{total_services} ({tpl_count} API · {b_count} браузер)"
        if b_count
        else str(total_services)
    )
    text = (
        h(f"{e('chart')} Статистика")
        + table(
            ["Параметр", "Значение"],
            [
                [f"{e('user')} Юзеры", str(total_users)],
                [f"{e('crown')} VIP", f"{vip} · {share}%"],
                [f"{e('ban')} Баны", str(banned)],
                [f"{e('shield')} Под защитой", str(protected)],
                [f"{e('bolt')} Атак всего", str(attacks)],
                [f"{e('fire')} Идёт сейчас", str(active_now)],
                [f"{e('doc')} Сервисы", services_val],
            ],
        )
        + "\n"
        + h(f"{e('star')} Топ по атакам", 3)
        + ("\n".join(lines) if lines else quote("Пока никто не запускал."))
    )
    await show(cb.message, text, admin_back_kb())

def services_text(
    templates: list,
    browser_services: dict[str, Any] | None = None,
    browser_countries: dict[str, str] | None = None,
) -> str:
    if browser_services is None:
        try:
            import browser
            browser_services = getattr(browser, "SERVICES", {})
            browser_countries = getattr(browser, "SERVICE_COUNTRIES", {})
        except Exception:
            browser_services = {}
            browser_countries = {}
    elif browser_countries is None:
        try:
            import browser
            browser_countries = getattr(browser, "SERVICE_COUNTRIES", {})
        except Exception:
            browser_countries = {}

    tpl_total = len(templates)
    tpl_ru = sum(1 for tpl in templates if getattr(tpl, "country", "") == "RU")
    tpl_ua = tpl_total - tpl_ru

    browser_total = len(browser_services)
    browser_ru = sum(
        1 for name, fn in browser_services.items()
        if (browser_countries.get(name) or getattr(fn, "country", "RU")) == "RU"
    )
    browser_ua = sum(
        1 for name, fn in browser_services.items()
        if (browser_countries.get(name) or getattr(fn, "country", "")) == "UA"
    )

    total_all = tpl_total + browser_total
    total_ru = tpl_ru + browser_ru
    total_ua = tpl_ua + browser_ua

    yandex_names = [tpl.name for tpl in templates if getattr(tpl, "yandex_captcha", None)]
    recaptcha = sum(1 for tpl in templates if getattr(tpl, "recaptcha_v3", None))
    chains = sum(1 for tpl in templates if getattr(tpl, "chain", None))
    prepare = sum(1 for tpl in templates if getattr(tpl, "prepare", None))

    tpl_vip = sum(1 for tpl in templates if getattr(tpl, "vip", False))
    tpl_free = tpl_total - tpl_vip

    rows = [
        [f"{e('play')} Всего", str(total_all)],
        [f"{e('ru')} RU", f"{total_ru} ({tpl_ru} + {browser_ru} вип)" if browser_total else str(total_ru)],
        [f"{e('ua')} UA", f"{total_ua} ({tpl_ua} + {browser_ua} вип)" if browser_total else str(total_ua)],
        [f"{e('spark')} Free тариф", f"{config.free_request_services_limit} сервисов на страну"],
        [f"{e('crown')} VIP тариф", f"Все {total_all} сервисов (+{total_all - config.free_request_services_limit * 2})"],
        [f"{e('doc')} HTTP / API", f"{tpl_total} ({tpl_free} free / {tpl_vip} VIP)"],
    ]
    if browser_total:
        rows.append([f"{e('device')} Вип сервисы (Browser)", str(browser_total)])
    rows.extend([
        [f"{e('key')} Yandex SmartCaptcha", str(len(yandex_names))],
        [f"{e('lock')} reCAPTCHA", str(recaptcha)],
        [f"{e('bolt')} Цепочки", str(chains)],
        [f"{e('gear')} prepare", str(prepare)],
    ])

    extra_lines = []
    if yandex_names:
        shown = ", ".join(yandex_names[:8])
        extra_lines.append(quote(
            f"SmartCaptcha: {shown}{'…' if len(yandex_names) > 8 else ''}"
        ))
    if browser_services:
        shown_browser = ", ".join(sorted(browser_services.keys()))
        extra_lines.append(quote(
            f"Вип сервисы ({browser_total}): {shown_browser}"
        ))

    details = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

    return (
        h(f"{e('doc')} Сервисы")
        + table(["Параметр", "Значение"], rows)
        + details
        + "\n"
        + quote(
            "Правка services.json и browser.py применяется кнопкой «Перезагрузить»: "
            "идущие атаки доживают на прежнем списке."
        )
    )

def services_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Перезагрузить", "admin_reload", icon_name="reset", style=PRIMARY)],
            [btn("В панель", "admin_open", icon_name="panel")],
        ]
    )

async def admin_services(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    await safe_cb_answer(cb)
    await show(cb.message, services_text(manager.templates), services_kb())

async def admin_reload(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    try:
        templates = manager.reload_templates(config.services_file or None)
        manager.reload_proxies(config.proxies_file)
        try:
            import browser
            importlib.reload(browser)
        except Exception as exc:
            logger.warning("reload browser: %s", exc)
    except TemplateError as exc:
        await safe_cb_answer(cb, "services.json не применён", show_alert=True)
        await show(
            cb.message,
            card(
                f"{e('no')} Сервисы",
                f"Файл не применён, работает прежний список:\n{esc(exc)}",
            ),
            services_kb(),
        )
        return
    await safe_cb_answer(cb)
    await show(cb.message, services_text(templates), services_kb())

async def cmd_reload(message: Message) -> None:
    if not await _check_msg(message):
        return
    try:
        templates = manager.reload_templates(config.services_file or None)
        manager.reload_proxies(config.proxies_file)
        try:
            import browser
            importlib.reload(browser)
        except Exception as exc:
            logger.warning("reload browser: %s", exc)
    except TemplateError as exc:
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('no')} Сервисы",
                f"Файл не применён, работает прежний список:\n{esc(exc)}",
            ),
            services_kb(),
        )
        return
    await say(bot, message.chat.id, services_text(templates), services_kb())

async def admin_users_noop(cb: CallbackQuery) -> None:
    await safe_cb_answer(cb)

async def admin_users_list(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    await safe_cb_answer(cb)
    total = await db.get_total_users()
    page, total_pages = _pages(_page_of(cb), total)
    users = await db.get_all_users(page=page, per_page=PER_PAGE)
    if not users:
        await show(
            cb.message,
            card(f"{e('user')} Юзеры", "База пустая: ещё никто не нажимал /start."),
            admin_back_kb(),
        )
        return

    lines = []
    kb = InlineKeyboardBuilder()
    for user_id, first_name, username, accepted, attacks, has_vip, is_banned in users:
        if is_banned:
            status = e("ban")
        elif has_vip:
            status = e("crown")
        else:
            status = e("ok") if accepted else e("dot")
        lines.append(
            f"{status}\n"
            + person_fields(
                user_id,
                first_name,
                username,
                extra=[("Атак", str(attacks))],
            )
        )
        kb.button(text=str(user_id), callback_data=f"admin_user:{user_id}")

    kb.adjust(5)
    text = (
        h(f"{e('user')} Юзеры · {total}")
        + "\n".join(lines)
        + f"\n<footer>{e('info')} Кнопка с ID открывает карточку: VIP, бан, лимиты.</footer>"
    )
    await show(cb.message, text, _paged_kb(page, total_pages, "admin_users", kb).as_markup())

async def admin_vip_list(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    if cb.data.endswith(":noop"):
        await safe_cb_answer(cb)
        return
    await safe_cb_answer(cb)
    total = await db.get_privileged_count()
    page, total_pages = _pages(_page_of(cb), total)
    rows, total = await db.get_vip_users(page=page, per_page=PER_PAGE)
    if not rows:
        await show(
            cb.message,
            card(f"{e('crown')} VIP", "Ни у кого нет. Выдать можно из карточки или кнопкой «Выдать VIP»."),
            admin_back_kb(),
        )
        return

    lines = []
    kb = InlineKeyboardBuilder()
    for user_id, first_name, username, attacks, reg_date in rows:
        lines.append(
            person_fields(
                user_id,
                first_name,
                username,
                since=format_dt(reg_date),
                extra=[("Атак", str(attacks))],
            )
        )
        kb.button(text=str(user_id), callback_data=f"admin_user:{user_id}")

    kb.adjust(5)
    await show(
        cb.message,
        h(f"{e('crown')} VIP · {total}") + "\n".join(lines),
        _paged_kb(page, total_pages, "admin_vip", kb).as_markup(),
    )

async def _card_markup(user_id: int):
    has_vip = await db.check_user_exists_privilege(user_id)
    is_banned = await db.is_user_banned(user_id)
    return admin_user_kb(user_id, has_vip, is_banned)

async def admin_user_view(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    await safe_cb_answer(cb)
    try:
        user_id = int(cb.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return

    stats = await db.get_user_stats(user_id)
    if not stats:
        await show(
            cb.message,
            card(
                f"{e('no')} Не найден",
                f"<code>{user_id}</code> нет в базе. Пусть нажмёт /start или выдай доступ по ID.",
            ),
            admin_back_kb(),
        )
        return
    first_name, username, reg_date, total_attacks = stats
    has_vip = await db.check_user_exists_privilege(user_id)
    is_banned = await db.is_user_banned(user_id)
    protected = await db.get_protected_by_user(user_id)
    override = await db.get_user_override(user_id)
    prot = (
        ", ".join(f"<code>{esc(display_phone(row[0]))}</code>" for row in protected)
        if protected
        else "Нет"
    )
    if has_vip:
        exp = await db.get_user_privilege_expiry(user_id)
        vip_label = f"Да (до {format_dt(exp)})" if exp else "Да"
    else:
        vip_label = "Нет"
    extra = [
        ("VIP", vip_label),
        ("Бан", "Да" if is_banned else "Нет"),
        ("Атак", str(total_attacks)),
        ("Сейчас", str(runtime.active_for(user_id))),
        ("Защита", prot),
    ]
    if override:
        extra.append(
            ("Свои лимиты", f"{override[0]} сразу, до {fmt_dur(override[1])}")
        )
    text = (
        h(f"{e('user')} Карточка")
        + quote("Кто это и что с ним можно сделать — кнопками ниже.")
        + person_fields(
            user_id,
            first_name,
            username,
            since=format_dt(reg_date),
            extra=extra,
        )
    )
    await show(cb.message, text, admin_user_kb(user_id, has_vip, is_banned))

async def admin_toggle_vip(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    await safe_cb_answer(cb)
    try:
        user_id = int(cb.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    if not await db.user_exists(user_id):
        await show(
            cb.message,
            card(f"{e('no')} Не найден", f"<code>{user_id}</code> нет в базе."),
            admin_back_kb(),
        )
        return
    if await db.check_user_exists_privilege(user_id):
        text = await _revoke_vip(user_id)
    else:
        text = await _grant_vip(user_id)
    await show(cb.message, text, await _card_markup(user_id))

async def admin_toggle_ban(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    try:
        user_id = int(cb.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    if is_admin(user_id):
        await safe_cb_answer(cb, "Нельзя банить админа", show_alert=True)
        return
    await safe_cb_answer(cb)
    if await db.is_user_banned(user_id):
        text = await _set_unban(user_id)
    else:
        text = await _set_ban(user_id)
    await show(cb.message, text, await _card_markup(user_id))

async def admin_action_prompt(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _check_cb(cb):
        return
    try:
        key = cb.data.split(":", 1)[1]
    except (IndexError, AttributeError):
        return
    action = ACTIONS.get(key)
    if action is None:
        return
    await safe_cb_answer(cb)
    await show(cb.message, action.prompt, admin_back_kb())
    await state.update_data(action=key)
    await state.set_state(AdminState.waiting_user)

async def process_admin_action(message: Message, state: FSMContext) -> None:
    if not await _check_msg(message):
        return
    key = (await state.get_data()).get("action")
    action = ACTIONS.get(key)
    if action is None:
        await state.clear()
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('hourglass')} Сессия истекла",
                "Бот перезапускался или диалог сброшен. Открой панель и выбери действие заново.",
            ),
            admin_back_kb(),
        )
        return
    user_id = await _resolve_user_id(message)
    if user_id is None:
        return
    if action.admin_block and is_admin(user_id):
        await say(bot, message.chat.id, action.admin_block, admin_back_kb())
        await state.clear()
        return
    if action.require_existing and not await db.user_exists(user_id):
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('no')} Не найден",
                f"<code>{user_id}</code> нет в базе. Сначала человек должен нажать /start.",
            ),
            admin_back_kb(),
        )
        await state.clear()
        return
    text = await action.apply(user_id)
    await say(bot, message.chat.id, text, admin_back_kb())
    await state.clear()

async def admin_search_cmd(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _check_cb(cb):
        return
    await safe_cb_answer(cb)
    await show(
        cb.message,
        card(
            f"{e('eye')} Поиск",
            "ID, @username или часть имени. Точный ID и @username откроют карточку сразу.",
        ),
        admin_back_kb(),
    )
    await state.set_state(AdminState.waiting_search_query)

async def process_search_query(message: Message, state: FSMContext) -> None:
    if not await _check_msg(message):
        return
    query = (message.text or "").strip()
    if not query:
        await say(bot, message.chat.id, card(f"{e('warn')} Пустой запрос", "Пришли ID, @username или часть имени."))
        return
    await state.clear()

    if query.isdigit() or query.startswith("@"):
        user_id = await _resolve_user_id(message)
        if user_id and await db.get_user_stats(user_id):
            has_vip = await db.check_user_exists_privilege(user_id)
            is_banned = await db.is_user_banned(user_id)
            user = await db.get_user_by_id(user_id)
            first_name, username = user if user else (None, None)
            await say(
                bot,
                message.chat.id,
                h(f"{e('ok')} Нашёл")
                + quote("Вот карточка. Дальше — кнопками.")
                + person_fields(
                    user_id,
                    first_name,
                    username,
                    extra=[
                        ("VIP", "Да" if has_vip else "Нет"),
                        ("Бан", "Да" if is_banned else "Нет"),
                    ],
                ),
                admin_user_kb(user_id, has_vip, is_banned),
            )
            return

    rows = await db.search_users(query, limit=10)
    if not rows:
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('eye')} Ничего нет",
                f"По запросу <code>{esc(query)}</code> совпадений нет. Проверь ID или username.",
            ),
            admin_back_kb(),
        )
        return

    lines = []
    kb = InlineKeyboardBuilder()
    for user_id, first_name, username, _accepted, attacks, has_vip, is_banned in rows:
        status = e("ban") if is_banned else e("crown") if has_vip else e("ok")
        lines.append(
            f"{status}\n"
            + person_fields(
                user_id,
                first_name,
                username,
                extra=[("Атак", str(attacks))],
            )
        )
        kb.button(text=f"{user_id}", callback_data=f"admin_user:{user_id}")
    kb.adjust(5)
    kb.row(btn("В панель", "admin_open", icon_name="panel"))
    await say(
        bot,
        message.chat.id,
        h(f"{e('eye')} {esc(query)} · {len(rows)}") + "\n".join(lines),
        kb.as_markup(),
    )

async def admin_limits_cmd(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _check_cb(cb):
        return
    await safe_cb_answer(cb)
    try:
        user_id = int(cb.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return

    current = await db.get_user_override(user_id)
    extra = (
        [
            ("Сейчас сразу", str(current[0])),
            ("Сейчас потолок", fmt_dur(current[1])),
        ]
        if current
        else [("Свои лимиты", "Нет, действуют VIP или базовые")]
    )
    await state.update_data(limits_uid=user_id)
    await state.set_state(AdminState.waiting_limits_input)
    await show(
        cb.message,
        h(f"{e('gear')} Персональные лимиты")
        + quote(
            "Пришли два числа: сколько атак сразу и потолок в секундах.\n"
            f"<code>35 20000</code> — 35 одновременно, до {fmt_dur(20000)}.\n"
            "Работают и без VIP, скорость отправки как у VIP."
        )
        + await user_display(user_id, extra=extra),
        admin_back_kb(),
    )

async def process_limits_input(message: Message, state: FSMContext) -> None:
    if not await _check_msg(message):
        return
    user_id = (await state.get_data()).get("limits_uid")
    if not user_id:
        await state.clear()
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('hourglass')} Сессия истекла",
                "Открой карточку пользователя и нажми «Лимиты» ещё раз.",
            ),
            admin_back_kb(),
        )
        return

    parts = (message.text or "").replace(",", " ").split()
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        user_id = int(parts[0])
        parts = parts[1:]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('warn')} Не тот формат",
                "Нужны два числа: атаки и секунды.\nПример: <code>35 20000</code>",
            ),
            admin_back_kb(),
        )
        return

    max_active, max_duration = int(parts[0]), int(parts[1])
    if not (1 <= max_active <= 100):
        await say(
            bot,
            message.chat.id,
            card(f"{e('warn')} Слишком много или мало", "Одновременно можно задать от 1 до 100 атак."),
            admin_back_kb(),
        )
        return
    if not (10 <= max_duration <= 86400):
        await say(
            bot,
            message.chat.id,
            card(
                f"{e('warn')} Длительность вне диапазона",
                "Потолок одной атаки — от 10 секунд до 86400 (сутки).",
            ),
            admin_back_kb(),
        )
        return

    await db.set_user_override(user_id, max_active, max_duration)
    await state.clear()
    await say(
        bot,
        message.chat.id,
        h(f"{e('ok')} Лимиты выданы")
        + quote("Уже действуют, даже без VIP. Скорость отправки как у VIP.")
        + await user_display(
            user_id,
            extra=[
                ("Одновременно", str(max_active)),
                ("Одна атака", f"до {fmt_dur(max_duration)}"),
            ],
        ),
        await _card_markup(user_id),
    )

async def admin_limits_off(cb: CallbackQuery) -> None:
    if not await _check_cb(cb):
        return
    await safe_cb_answer(cb)
    try:
        user_id = int(cb.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    removed = await db.remove_user_override(user_id)
    if removed:
        text = (
            h(f"{e('ok')} Лимиты сброшены")
            + quote("Дальше снова тариф: VIP или базовый.")
            + await user_display(
                user_id,
                extra=[
                    ("VIP сразу", str(config.vip_max_active)),
                    ("VIP одна атака", f"до {fmt_dur(config.vip_max_duration)}"),
                    ("VIP в сутки", str(config.vip_daily_limit)),
                    ("Без VIP сразу", str(config.default_max_active)),
                    ("Без VIP одна атака", f"до {fmt_dur(config.default_max_duration)}"),
                    ("Без VIP в сутки", str(config.default_daily_limit)),
                ],
            )
        )
    else:
        text = (
            h(f"{e('info')} Своих лимитов не было")
            + quote("Сбрасывать нечего.")
            + await user_display(user_id)
        )
    markup = await _card_markup(user_id)
    await show(cb.message, text, markup)

def register_admin(dp: Dispatcher) -> None:
    dp.message.register(admin_panel, F.text == "адм")
    dp.callback_query.register(admin_open, F.data == "admin_open")
    dp.callback_query.register(admin_close, F.data == "admin_close")
    dp.callback_query.register(admin_stats, F.data == "admin_stats")
    dp.callback_query.register(admin_services, F.data == "admin_services")
    dp.callback_query.register(admin_reload, F.data == "admin_reload")
    dp.message.register(cmd_reload, Command("reload", "services"))

    dp.callback_query.register(admin_users_noop, F.data == "admin_users:noop")
    dp.callback_query.register(admin_users_list, F.data.startswith("admin_users:"))
    dp.callback_query.register(admin_vip_list, F.data.startswith("admin_vip:"))
    dp.callback_query.register(admin_user_view, F.data.startswith("admin_user:"))
    dp.callback_query.register(admin_toggle_vip, F.data.startswith("admin_toggle_vip:"))
    dp.callback_query.register(admin_toggle_ban, F.data.startswith("admin_toggle_ban:"))

    dp.callback_query.register(admin_action_prompt, F.data.startswith("admin_action:"))
    dp.message.register(process_admin_action, AdminState.waiting_user)

    dp.callback_query.register(admin_search_cmd, F.data == "admin_search")
    dp.message.register(process_search_query, AdminState.waiting_search_query)

    dp.callback_query.register(admin_limits_off, F.data.startswith("admin_limits_off:"))
    dp.callback_query.register(admin_limits_cmd, F.data.startswith("admin_limits:"))
    dp.message.register(process_limits_input, AdminState.waiting_limits_input)

_background: set[asyncio.Task] = set()
_broadcast: asyncio.Task | None = None
_broadcast_stop: asyncio.Event | None = None
_broadcast_lock = asyncio.Lock()

def broadcast_control_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn(
                    "Остановить",
                    "admin_broadcast_stop",
                    icon_name="stop",
                    style=DANGER,
                )
            ]
        ]
    )

async def admin_broadcast_cmd(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await safe_cb_answer(cb, "Нет доступа.", show_alert=True)
        return
    await safe_cb_answer(cb)
    await show(
        cb.message,
        card(
            f"{e('siren')} Рассылка",
            "Пришли текст. Он уйдёт всем обычным сообщением, без оформления и кнопок.",
        ),
        admin_back_kb(),
    )
    await state.set_state(AdminState.waiting_broadcast_text)

async def process_broadcast_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await say(bot, message.chat.id, card(f"{e('ban')} Нет доступа", "Рассылка только для админов."))
        return
    text = (message.text or "").strip()
    if not text:
        await say(
            bot,
            message.chat.id,
            card(f"{e('warn')} Пусто", "Пришли текст, который уйдёт всем."),
        )
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("Отправить", "admin_broadcast_confirm", icon_name="send", style=PRIMARY),
                btn("Отмена", "admin_broadcast_cancel", icon_name="cancel", style=DANGER),
            ]
        ]
    )
    await state.update_data(broadcast_text=text)
    await say(
        bot,
        message.chat.id,
        card(
            f"{e('eye')} Предпросмотр",
            esc(text),
            footer=f"{e('info')} Уйдёт всем как обычный текст. Оформление из панели не переносится.",
        ),
        keyboard,
    )

async def _broadcast_send(user_id: int, text: str) -> bool:
    try:
        await bot.send_message(user_id, text, parse_mode=None)
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 0.1)
        try:
            await bot.send_message(user_id, text, parse_mode=None)
            return True
        except Exception:
            return False
    except Exception:
        return False

async def _run_broadcast(
    chat_id: int,
    message_id: int,
    text: str,
    *,
    start_after: int = 0,
    sent: int = 0,
    failed: int = 0,
    stop_event: asyncio.Event | None = None,
) -> None:
    global _broadcast, _broadcast_stop
    stop_event = stop_event or asyncio.Event()
    last_id = int(start_after)
    processed = 0
    stopped = False
    try:
        while True:
            if stop_event.is_set():
                stopped = True
                break
            try:
                user_ids = await db.get_user_ids_after(last_id, 200)
            except Exception as exc:
                logger.warning("broadcast: cannot load batch after %s: %r", last_id, exc)
                stopped = True
                break
            if not user_ids:
                break

            for user_id in user_ids:
                if stop_event.is_set():
                    stopped = True
                    break
                if await _broadcast_send(user_id, text):
                    sent += 1
                else:
                    failed += 1
                last_id = user_id
                processed += 1
                await asyncio.sleep(config.broadcast_delay)
                if processed % config.broadcast_batch == 0:
                    try:
                        await db.save_broadcast_state(
                            text, last_id, sent, failed, active=True
                        )
                    except Exception as exc:
                        logger.warning("broadcast: checkpoint failed: %r", exc)
                    await asyncio.sleep(0.5)

            if stopped:
                break

        if processed:
            try:
                await db.save_broadcast_state(
                    text, last_id, sent, failed, active=False
                )
            except Exception as exc:
                logger.warning("broadcast: final checkpoint failed: %r", exc)

        if not stopped:
            try:
                await db.clear_broadcast_state()
            except Exception as exc:
                logger.warning("broadcast: clear checkpoint failed: %r", exc)

        title = (
            f"{e('warn')} Рассылка остановлена"
            if stopped
            else f"{e('ok')} Рассылка готова"
        )
        footer = (
            f"{e('info')} Продолжить можно тем же текстом — начну с места остановки."
            if stopped
            else f"{e('info')} «Не дошло» — бот заблокирован или аккаунт удалён."
        )
        body = (
            f"{e('ok')} доставлено {sent}\n"
            f"{e('no')} не дошло {failed}\n"
            f"{e('chart')} всего {sent + failed}"
        )
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                rich_message=rich(card(title, body, footer=footer)),
                reply_markup=admin_back_kb(),
            )
        except Exception:
            pass
        logger.info(
            "broadcast %s: sent=%d failed=%d last_id=%s",
            "stopped" if stopped else "finished",
            sent,
            failed,
            last_id,
        )
    finally:
        async with _broadcast_lock:
            if _broadcast is asyncio.current_task():
                _broadcast = None
                _broadcast_stop = None

async def admin_broadcast_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    global _broadcast, _broadcast_stop
    if not is_admin(cb.from_user.id):
        await safe_cb_answer(cb, "Нет доступа.", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    await state.clear()
    if not text:
        await safe_cb_answer(cb, "Текст не найден.", show_alert=True)
        return

    async with _broadcast_lock:
        if _broadcast is not None and not _broadcast.done():
            await safe_cb_answer(cb, "Рассылка уже идёт.", show_alert=True)
            return

        start_after = 0
        sent = 0
        failed = 0
        resumed = False
        try:
            saved = await db.get_broadcast_state()
        except Exception as exc:
            logger.warning("broadcast: cannot read checkpoint: %r", exc)
            saved = None
        if saved:
            saved_text, saved_last, saved_sent, saved_failed = (
                saved[0],
                int(saved[1] or 0),
                int(saved[2] or 0),
                int(saved[3] or 0),
            )
            if saved_text == text and saved_last > 0:
                start_after = saved_last
                sent = saved_sent
                failed = saved_failed
                resumed = True
            else:
                try:
                    await db.clear_broadcast_state()
                except Exception:
                    pass

        await safe_cb_answer(cb)
        await show(
            cb.message,
            card(
                f"{e('wait')} Рассылка идёт",
                (
                    "Продолжаю с места остановки.\n"
                    if resumed
                    else "Сообщения уходят по очереди.\n"
                )
                + "Этот экран обновится, когда очередь кончится.",
            ),
            broadcast_control_kb(),
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            _run_broadcast(
                cb.message.chat.id,
                cb.message.message_id,
                text,
                start_after=start_after,
                sent=sent,
                failed=failed,
                stop_event=stop_event,
            ),
            name="broadcast",
        )
        _broadcast = task
        _broadcast_stop = stop_event
        _background.add(task)
        task.add_done_callback(_background.discard)

async def admin_broadcast_stop(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await safe_cb_answer(cb, "Нет доступа.", show_alert=True)
        return
    async with _broadcast_lock:
        task = _broadcast
        event = _broadcast_stop
        running = task is not None and not task.done()
    if not running or event is None:
        await safe_cb_answer(cb, "Рассылка уже не идёт.", show_alert=True)
        return
    event.set()
    await safe_cb_answer(cb, "Останавливаю после текущего сообщения.")

async def admin_broadcast_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb.from_user.id):
        await safe_cb_answer(cb, "Нет доступа.", show_alert=True)
        return
    await safe_cb_answer(cb)
    await state.clear()
    await show(cb.message, await admin_panel_text(), admin_kb())

def register_broadcast(dp: Dispatcher) -> None:
    dp.callback_query.register(admin_broadcast_cmd, F.data == "admin_broadcast")
    dp.message.register(process_broadcast_text, AdminState.waiting_broadcast_text)
    dp.callback_query.register(admin_broadcast_confirm, F.data == "admin_broadcast_confirm")
    dp.callback_query.register(admin_broadcast_stop, F.data == "admin_broadcast_stop")
    dp.callback_query.register(admin_broadcast_cancel, F.data == "admin_broadcast_cancel")

_FSM_UNFETCHED = object()

class SqliteStorage(BaseStorage):

    def __init__(self) -> None:
        self._db: Database | None = None
        self._cache: dict[str, list] = {}

    def bind(self, database: Database) -> None:
        self._db = database

    def _key(self, key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.destiny}"

    def _require(self):
        if self._db is None or self._db._conn is None:
            raise RuntimeError("database is not initialized")
        return self._db._conn

    async def _entry(self, key: StorageKey) -> list:
        storage_key = self._key(key)
        entry = self._cache.get(storage_key)
        if (
            entry is not None
            and entry[0] is not _FSM_UNFETCHED
            and entry[1] is not _FSM_UNFETCHED
        ):
            return entry

        conn = self._require()
        async with self._db._lock:
            async with conn.execute(
                "SELECT state, data FROM fsm WHERE key = ?", (storage_key,)
            ) as cursor:
                row = await cursor.fetchone()

        state = row[0] if row else None
        data: dict = {}
        if row and row[1]:
            try:
                loaded = json.loads(row[1])
                data = loaded if isinstance(loaded, dict) else {}
            except (TypeError, ValueError):
                data = {}

        if len(self._cache) > 2048:
            for k in list(self._cache)[:512]:
                self._cache.pop(k, None)

        if entry is None:
            entry = [state, data]
        else:
            entry[0], entry[1] = state, data
        self._cache[storage_key] = entry
        return entry

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        storage_key = self._key(key)
        self._require()
        await self._db.write(
            """
            INSERT INTO fsm (key, state, data, updated_at) VALUES (?, ?, '{}', ?)
            ON CONFLICT(key) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at
            """,
            (storage_key, value, time.time()),
        )
        entry = self._cache.get(storage_key)
        if entry is None:
            self._cache[storage_key] = [value, _FSM_UNFETCHED]
        else:
            entry[0] = value

    async def get_state(self, key: StorageKey) -> str | None:
        entry = await self._entry(key)
        return entry[0]

    async def set_data(self, key: StorageKey, data: dict) -> None:
        storage_key = self._key(key)
        payload = dict(data)
        self._require()
        await self._db.write(
            """
            INSERT INTO fsm (key, state, data, updated_at) VALUES (?, NULL, ?, ?)
            ON CONFLICT(key) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
            """,
            (storage_key, json.dumps(payload), time.time()),
        )
        entry = self._cache.get(storage_key)
        if entry is None:
            self._cache[storage_key] = [_FSM_UNFETCHED, payload]
        else:
            entry[1] = payload

    async def get_data(self, key: StorageKey) -> dict:
        entry = await self._entry(key)
        return dict(entry[1]) if isinstance(entry[1], dict) else {}

    async def close(self) -> None:
        self._cache.clear()
        return None

storage = SqliteStorage()
dp = Dispatcher(storage=storage)

def register_vip_community(dp: Dispatcher) -> None:
    dp.chat_member.register(vip_chat_member_handler)

def register_all(dp: Dispatcher) -> None:
    register_common(dp)
    register_payments(dp)
    register_attack(dp)
    register_profile(dp)
    register_admin(dp)
    register_broadcast(dp)
    register_vip_community(dp)

    # The Autonomous Service Agent is controlled exclusively through its web
    # dashboard (`python -m agent.cli web`). Telegram control handlers are
    # intentionally NOT registered here.

READY_CACHE_SECONDS = 120.0
POLLING_BACKOFF_START = 5
POLLING_BACKOFF_MAX = 60

_ready_checked_at = 0.0

def setup_logging() -> None:
    level = getattr(logging, str(config.log_level).upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            config.log_file,
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        logger.warning("log file unavailable (%s): %s", config.log_file, exc)

    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

def _build_bot() -> Bot:
    session = AiohttpSession(proxy=config.proxy) if config.proxy else None
    return Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

def _raise_nofile_limit() -> None:
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = max(soft, min(32768, hard if hard > 0 else 32768))
        if want > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard if hard > 0 else want))
            logger.info("nofile limit: %s -> %s", soft, want)
    except Exception:
        pass

def _health_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/readyz", _readyz)
    return app

async def _healthz(_request: web.Request) -> web.Response:
    try:
        if manager._closed:
            raise RuntimeError("движок остановлен")
        return web.Response(text="ok", content_type="text/plain")
    except Exception as exc:
        return web.Response(text=f"not ok: {exc}", status=503)

async def _readyz(_request: web.Request) -> web.Response:
    global _ready_checked_at
    try:
        await db.get_total_users()
        engine = manager.health()
        if not engine.get("ok"):
            raise RuntimeError("движок остановлен")
        loop_now = asyncio.get_running_loop().time()
        if loop_now - _ready_checked_at > READY_CACHE_SECONDS:
            await bot.get_me()
            _ready_checked_at = loop_now
        return web.Response(text="ready", content_type="text/plain")
    except Exception as exc:
        return web.Response(text=f"not ready: {exc}", status=503)

async def _start_health_server() -> web.AppRunner | None:
    runner = web.AppRunner(_health_app())
    await runner.setup()
    site = web.TCPSite(runner, config.health_host, config.health_port)
    try:
        await site.start()
        logger.info("health: http://%s:%s/healthz", config.health_host, config.health_port)
        return runner
    except OSError as exc:
        logger.warning("health server bind failed: %s", exc)
        await runner.cleanup()
        return None

def _acquire_instance_lock():
    path = config.lock_file
    try:
        handle = open(path, "a+")
    except OSError as exc:
        logger.warning("instance lock unavailable (%s): %s", path, exc)
        return None
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        return handle
    except OSError as exc:
        handle.close()
        raise SystemExit(f"другой экземпляр уже запущен (lock {path}): {exc}") from exc
    try:
        os.chmod(path, 0o600)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass
    return handle

def _seconds_until_midnight_msk() -> float:
    now = datetime.datetime.now(MSK)
    tomorrow = now.date() + datetime.timedelta(days=1)
    midnight = datetime.datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 1, tzinfo=MSK)
    return max(1.0, (midnight - now).total_seconds())

async def _daily_reset_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        sleep_secs = _seconds_until_midnight_msk()
        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_secs)
            return
        except asyncio.TimeoutError:
            pass
        try:
            today_str = datetime.datetime.now(MSK).strftime("%Y-%m-%d")
            logger.info("Новые сутки (%s): суточные счётчики сброшены", today_str)
            if hasattr(db, "reset_daily_cache"):
                db.reset_daily_cache()
            if hasattr(db, "cleanup_old_daily_launches"):
                pruned = await db.cleanup_old_daily_launches(keep_days=30)
                if pruned:
                    logger.info("Очищено %d старых записей суточных запусков", pruned)
        except Exception as exc:
            logger.warning("Ошибка при сбросе в начале суток: %r", exc)

async def _fsm_cleanup_loop(stop: asyncio.Event) -> None:
    interval = max(1, int(config.fsm_cleanup_hours)) * 3600
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            dropped = await db.cleanup_fsm()
            if dropped:
                logger.info("dropped %d stale fsm row(s)", dropped)
        except Exception as exc:
            logger.warning("periodic fsm cleanup failed: %r", exc)

async def _auto_reload_loop(mgr: AttackManager, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            mgr.check_and_reload()
        except Exception as exc:
            logger.debug("auto-reload tick error: %r", exc)

async def _xrocket_poll_loop(client: XRocketClient, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=10.0)
            return
        except asyncio.TimeoutError:
            pass

        try:
            active_invoices = await db.get_active_xrocket_invoices()
        except Exception as exc:
            logger.debug("xrocket poll db error: %r", exc)
            continue

        for inv_row in active_invoices:
            if stop.is_set():
                return
            inv_id, user_id, amount_val, currency, _pay_url = inv_row
            try:
                inv = await client.get_invoice(inv_id)
            except Exception as exc:
                logger.debug("xrocket poll get_invoice %s error: %r", inv_id, exc)
                continue

            status = inv.get("status")
            if status == "paid":
                charge_id = f"xrocket_{inv_id}"
                try:
                    amount_int = int(float(amount_val) * 100)
                except (ValueError, TypeError):
                    amount_int = int(config.xrocket_vip_price_usdt * 100)
                try:
                    grant_status = await db.complete_payment(
                        user_id, charge_id, amount_int, currency, "vip"
                    )
                    await db.update_xrocket_invoice_status(inv_id, "paid")
                except Exception as exc:
                    logger.exception(
                        "user %s: failed to complete xrocket poll payment %s: %r",
                        user_id,
                        inv_id,
                        exc,
                    )
                    grant_status = "error"

                if grant_status in ("granted", "duplicate", "already_vip"):
                    logger.info("xrocket poll: user %s activated VIP via invoice %s", user_id, inv_id)
                    try:
                        invite_link = await issue_vip_community_link(bot, user_id)
                        await say(
                            bot,
                            user_id,
                            vip_success_card(
                                invite_link,
                                title=f"{e('crown')} Оплата получена! VIP включён",
                            ),
                            vip_activated_kb(invite_link),
                            sticker=config.sticker_vip,
                        )
                    except Exception as exc:
                        logger.warning(
                            "user %s: failed to send xrocket vip activation message: %r",
                            user_id,
                            exc,
                        )
            elif status in ("expired", "cancelled"):
                try:
                    await db.update_xrocket_invoice_status(inv_id, status)
                except Exception as exc:
                    logger.debug("xrocket poll update status error for %s: %r", inv_id, exc)

async def run() -> None:
    global db, bot, manager, runtime, xrocket_client

    setup_logging()
    _raise_nofile_limit()

    lock_handle = _acquire_instance_lock()

    if not config.bot_token or config.bot_token.startswith("PASTE"):
        raise SystemExit("config.bot_token не заполнен")

    db = Database(config.db_path)
    await db.init_db()
    storage.bind(db)

    closed = await db.close_dangling_attacks()
    if closed:
        logger.info("closed %d unfinished attack(s) from a previous run", closed)

    try:
        dropped = await db.cleanup_fsm()
    except Exception as exc:
        logger.warning("fsm cleanup failed: %r", exc)
    else:
        if dropped:
            logger.info("dropped %d stale fsm row(s)", dropped)

    templates = load_templates(config.services_file or None, strict=False)
    proxies = [] if config.proxy else load_proxies(config.proxies_file)
    if proxies:
        ru_count = sum(1 for p in proxies if p.country == "RU")
        ua_count = sum(1 for p in proxies if p.country == "UA")
        logger.info(
            "proxies loaded: %d (RU: %d, UA: %d, rotation per request)",
            len(proxies),
            ru_count,
            ua_count,
        )
    elif config.proxy:
        logger.info("proxy: fixed %s", config.proxy.split("@")[-1])
    else:
        logger.info("proxies: none, sending directly")
    manager = AttackManager(
        templates,
        proxy=config.proxy,
        proxies=proxies,
        proxy_cooldown=config.proxy_cooldown,
        proxy_fail_threshold=config.proxy_fail_threshold,
        timeout=config.request_timeout,
        delay_ms=config.http_delay_ms,
        max_attacks=config.max_attacks,
        max_workers=config.http_workers_cap,
    )
    bot = _build_bot()
    runtime = AttackRuntime(manager, db, bot)

    health_runner = await _start_health_server()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    cleanup_task = asyncio.create_task(_fsm_cleanup_loop(stop), name="fsm-cleanup")
    _background.add(cleanup_task)
    cleanup_task.add_done_callback(_background.discard)

    daily_reset_task = asyncio.create_task(_daily_reset_loop(stop), name="daily-reset")
    _background.add(daily_reset_task)
    daily_reset_task.add_done_callback(_background.discard)

    reload_task = asyncio.create_task(_auto_reload_loop(manager, stop), name="auto-reload")
    _background.add(reload_task)
    reload_task.add_done_callback(_background.discard)

    xrocket_client = XRocketClient(config.xrocket_api_key, config.xrocket_api_url)
    if config.xrocket_enabled:
        xrocket_task = asyncio.create_task(
            _xrocket_poll_loop(xrocket_client, stop), name="xrocket-poll"
        )
        _background.add(xrocket_task)
        xrocket_task.add_done_callback(_background.discard)
    else:
        logger.warning("XROCKET_API_KEY не задан — криптоплатежи отключены")

    logger.info(
        "starting: templates=%d delay=%sms max_attacks=%d",
        len(templates),
        config.http_delay_ms,
        config.max_attacks,
    )

    backoff = POLLING_BACKOFF_START
    dispatcher = Dispatcher(storage=storage)
    dispatcher.update.outer_middleware(AccessMiddleware())
    register_all(dispatcher)
    try:
        while not stop.is_set():
            started_at = time.monotonic()
            polling = asyncio.create_task(
                dispatcher.start_polling(
                    bot,
                    handle_signals=False,
                    close_bot_session=False,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                ),
                name="polling",
            )
            waiter = asyncio.create_task(stop.wait(), name="stop-waiter")
            await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)

            if stop.is_set():
                logger.info("shutdown signal received, stopping polling…")
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                break

            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

            try:
                exc = polling.exception()
            except asyncio.CancelledError:
                break
            if time.monotonic() - started_at > 60:
                backoff = POLLING_BACKOFF_START
            if exc is None:
                logger.error("polling exited without exception — restarting in %ss", backoff)
            else:
                logger.error("polling crashed — restarting in %ss: %r", backoff, exc)

            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, POLLING_BACKOFF_MAX)
    finally:
        logger.info("shutting down…")
        if health_runner is not None:
            try:
                await health_runner.cleanup()
            except Exception:
                pass
        if _broadcast is not None and not _broadcast.done():
            _broadcast.cancel()
        for task in list(_background):
            task.cancel()
        if _background:
            await asyncio.gather(*_background, return_exceptions=True)
        try:
            await runtime.shutdown()
        except Exception as exc:
            logger.warning("runtime shutdown: %r", exc)
        try:
            await manager.shutdown()
        except Exception as exc:
            logger.warning("engine shutdown: %r", exc)
        try:
            await db.close()
        except Exception as exc:
            logger.warning("db close: %r", exc)
        try:
            await xrocket_client.close()
        except Exception as exc:
            logger.warning("xrocket close: %r", exc)
        try:
            await bot.session.close()
        except Exception:
            pass
        if lock_handle is not None:
            try:
                lock_handle.close()
            except Exception:
                pass
        logger.info("stopped")

def main() -> int:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(f"[exit] {exc}", file=sys.stderr)
            return int(exc.code)
        print("[exit] остановлено пользователем")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
