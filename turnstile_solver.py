from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from patchright.async_api import async_playwright

logger = logging.getLogger(__name__)

__all__ = [
    "AsyncTurnstileSolver",
    "TOKEN_INPUT",
    "TurnstileChallenge",
    "TurnstileError",
    "TurnstileResult",
    "TurnstileSolver",
    "TurnstileTimeout",
    "apply_turnstile_token",
    "detect_turnstile",
    "get_turnstile_token",
    "solve_for_page",
]

TOKEN_INPUT = "[name=cf-turnstile-response]"
WIDGET_SELECTOR = ".cf-turnstile"

_DEFAULT_LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)
_CHANNELS = frozenset({"chromium", "chrome", "msedge"})

class TurnstileError(RuntimeError):
    pass

class TurnstileTimeout(TurnstileError):
    pass

@dataclass(frozen=True, slots=True)
class TurnstileChallenge:
    sitekey: str
    url: str = ""
    action: str | None = None
    cdata: str | None = None
    response_selector: str = TOKEN_INPUT

@dataclass(frozen=True)
class TurnstileResult:

    turnstile_value: str | None
    elapsed_time_seconds: float
    status: str = "failure"
    reason: str | None = None

    @property
    def token(self) -> str:
        return self.turnstile_value or ""

    @property
    def is_solved(self) -> bool:
        return self.status == "success" and bool(self.turnstile_value)

    @property
    def ok(self) -> bool:
        return self.is_solved

    def as_dict(self) -> dict[str, Any]:
        return {
            "turnstile_value": self.turnstile_value,
            "elapsed_time_seconds": self.elapsed_time_seconds,
            "status": self.status,
            "reason": self.reason,
        }

_DETECT_JS = r"""() => {
    const result = {sitekeys: [], actions: [], cdatas: [], url: location.href};
    const add = (value) => {
        if (!value) return;
        const key = String(value).trim();
        if (key && !result.sitekeys.includes(key)) result.sitekeys.push(key);
    };
    const addValue = (list, value) => {
        if (value && !list.includes(value)) list.push(value);
    };

    for (const el of document.querySelectorAll('[data-sitekey]')) {
        add(el.getAttribute('data-sitekey'));
        addValue(result.actions, el.getAttribute('data-action'));
        addValue(result.cdatas, el.getAttribute('data-cdata'));
    }

    for (const frame of document.querySelectorAll('iframe[src]')) {
        try {
            const src = new URL(frame.src, location.href);
            if (!src.hostname.endsWith('challenges.cloudflare.com')) continue;
            add(src.searchParams.get('k') || src.searchParams.get('sitekey'));
            addValue(result.actions, src.searchParams.get('action'));
            addValue(result.cdatas, src.searchParams.get('cdata'));
        } catch (_) {
            /* ignore malformed iframe URLs */
        }
    }

    for (const script of document.scripts) {
        const text = (script.textContent || '') + ' ' + (script.src || '');
        const matches = text.match(/0x4[A-Za-z0-9_-]{10,}/g) || [];
        for (const match of matches) add(match);
    }

    return result;
}"""

_READ_TOKEN_JS = r"""() => {
    const input = document.querySelector('[name=cf-turnstile-response]');
    if (input && input.value) return input.value;
    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
        try {
            return window.turnstile.getResponse() || '';
        } catch (_) {
            return '';
        }
    }
    return '';
}"""

_APPLY_TOKEN_JS = r"""payload => {
    const {token, selector} = payload;
    let input = document.querySelector(selector);
    if (!input) {
        input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'cf-turnstile-response';
        input.id = 'cf-turnstile-response';
        (document.body || document.documentElement).appendChild(input);
    }
    input.value = token;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));

    const callbacks = new Set();
    for (const el of document.querySelectorAll('[data-callback]')) {
        const name = el.getAttribute('data-callback');
        if (name) callbacks.add(name);
    }
    for (const name of callbacks) {
        try {
            if (typeof window[name] === 'function') window[name](token);
        } catch (_) {
            /* page callback errors must not break the service flow */
        }
    }
    return input.value === token;
}"""

def _clean_sitekey(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200 or any(char.isspace() for char in text):
        return ""
    return text

def _clean_option(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

def _target_url(url: Any) -> str:
    text = str(url or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    if not text.startswith(("http://", "https://")):
        raise TurnstileError(f"Turnstile: нужен http(s) URL, получено {text!r}")
    if "#" in text:
        text = text.split("#", 1)[0]
    return text

def _same_url(candidate: str, target: str) -> bool:
    left = str(candidate or "").split("#", 1)[0]
    right = str(target or "").split("#", 1)[0]
    return left == right or left.rstrip("/") == right.rstrip("/")

def _proxy_config(proxy: Any) -> dict[str, str] | None:
    if proxy is None or proxy == "":
        return None
    if isinstance(proxy, Mapping):
        config = {
            str(key): str(value)
            for key, value in proxy.items()
            if value is not None and str(value) != ""
        }
        server = config.get("server", "").replace("socks5h://", "socks5://")
        if not server:
            return None
        config["server"] = server
        return config

    config_method = getattr(proxy, "browser_config", None)
    if callable(config_method):
        return _proxy_config(config_method())
    proxy_url = getattr(proxy, "proxy_url", None)
    if proxy_url:
        return _proxy_config(str(proxy_url))
    if hasattr(proxy, "host") and hasattr(proxy, "port"):
        scheme = str(getattr(proxy, "scheme", "http") or "http").replace("socks5h", "socks5")
        server = f"{scheme}://{getattr(proxy, 'host')}:{getattr(proxy, 'port')}"
        config = {"server": server}
        username = getattr(proxy, "username", "")
        password = getattr(proxy, "password", "")
        if username:
            config["username"] = str(username)
            if password:
                config["password"] = str(password)
        return config

    raw = str(proxy).strip()
    if not raw:
        return None
    if "://" not in raw and raw.count(":") == 4:
        scheme, host, port, username, password = raw.split(":")
        raw = f"{scheme}://{username}:{password}@{host}:{port}"
    if "://" not in raw:
        raw = "http://" + raw

    parts = urlsplit(raw)
    scheme = (parts.scheme or "http").lower().replace("socks5h", "socks5")
    if not parts.hostname:
        return None
    port = parts.port or (443 if scheme == "https" else 80)
    config = {"server": f"{scheme}://{parts.hostname}:{port}"}
    if parts.username:
        config["username"] = unquote(parts.username)
        if parts.password:
            config["password"] = unquote(parts.password)
    return config

def _widget_html(sitekey: str, action: str | None, cdata: str | None) -> str:
    attributes = f' data-sitekey="{html.escape(sitekey, quote=True)}"'
    if action:
        attributes += f' data-action="{html.escape(action, quote=True)}"'
    if cdata:
        attributes += f' data-cdata="{html.escape(cdata, quote=True)}"'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Turnstile</title>
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head>
<body>
    <div class="cf-turnstile" style="background: white;"{attributes}></div>
</body>
</html>"""

async def detect_turnstile(
    page: Any,
    *,
    sitekey: str | None = None,
    action: str | None = None,
    timeout: float = 2.0,
) -> TurnstileChallenge | None:
    page_url = str(getattr(page, "url", "") or "")
    explicit = _clean_sitekey(sitekey)
    clean_action = _clean_option(action)
    if explicit and clean_action:
        return TurnstileChallenge(
            sitekey=explicit,
            url=page_url,
            action=clean_action,
        )

    try:
        info = await asyncio.wait_for(page.evaluate(_DETECT_JS), max(0.2, float(timeout)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("turnstile: не удалось просканировать страницу: %r", exc)
        if explicit:
            return TurnstileChallenge(sitekey=explicit, url=page_url, action=clean_action)
        return None

    if not isinstance(info, Mapping):
        if explicit:
            return TurnstileChallenge(sitekey=explicit, url=page_url, action=clean_action)
        return None

    keys = info.get("sitekeys") or []
    if isinstance(keys, str):
        keys = [keys]
    found = explicit
    if not found:
        for raw in keys:
            found = _clean_sitekey(raw)
            if found:
                break
    if not found:
        return None

    actions = info.get("actions") or []
    if isinstance(actions, str):
        actions = [actions]
    detected_action = clean_action
    if not detected_action:
        for candidate in actions:
            detected_action = _clean_option(candidate)
            if detected_action:
                break

    cdatas = info.get("cdatas") or []
    if isinstance(cdatas, str):
        cdatas = [cdatas]
    detected_cdata = None
    for candidate in cdatas:
        detected_cdata = _clean_option(candidate)
        if detected_cdata:
            break

    return TurnstileChallenge(
        sitekey=found,
        url=str(info.get("url") or page_url or ""),
        action=detected_action,
        cdata=detected_cdata,
    )

async def apply_turnstile_token(
    page: Any,
    token: str,
    *,
    selector: str = TOKEN_INPUT,
) -> bool:
    value = str(token or "").strip()
    if not value:
        return False
    try:
        result = await page.evaluate(
            _APPLY_TOKEN_JS,
            {"token": value, "selector": selector or TOKEN_INPUT},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("turnstile: не удалось подставить токен: %r", exc)
        return False
    return bool(result)

class TurnstileSolver:

    def __init__(
        self,
        *,
        context: Any = None,
        browser: Any = None,
        proxy: Any = None,
        headless: bool = True,
        useragent: str | None = None,
        timeout: float = 30.0,
        max_attempts: int = 10,
        poll_interval: float = 0.5,
        browser_type: str = "chromium",
        launch_args: Sequence[str] | None = None,
        context_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.context = context
        self.browser = browser
        self.proxy = proxy
        self.headless = bool(headless)
        self.useragent = _clean_option(useragent)
        self.timeout = max(1.0, float(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.poll_interval = max(0.05, float(poll_interval))
        self.browser_type = str(browser_type or "chromium").lower()
        if self.browser_type not in _CHANNELS:
            raise TurnstileError(
                f"Turnstile: неизвестный браузер {browser_type!r}; "
                f"доступны {sorted(_CHANNELS)}"
            )
        self.launch_args = list(launch_args or _DEFAULT_LAUNCH_ARGS)
        self.context_options = dict(context_options or {})

    def _new_context_options(self) -> dict[str, Any]:
        options = dict(self.context_options)
        if self.useragent:
            options.setdefault("user_agent", self.useragent)
        return options

    async def _launch(self, playwright: Any) -> Any:
        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": self.launch_args,
        }
        proxy = _proxy_config(self.proxy)
        if proxy:
            kwargs["proxy"] = proxy
        if self.browser_type != "chromium":
            kwargs["channel"] = self.browser_type
        return await playwright.chromium.launch(**kwargs)

    async def _open_page(self) -> tuple[Any, Any | None, Any | None, Any | None]:
        if self.context is not None:
            return await self.context.new_page(), None, None, None

        if self.browser is not None:
            context = await self.browser.new_context(**self._new_context_options())
            try:
                page = await context.new_page()
            except BaseException:
                await _quiet_close(context)
                raise
            return page, context, None, None

        playwright = await async_playwright().start()
        browser = context = page = None
        try:
            browser = await self._launch(playwright)
            context = await browser.new_context(**self._new_context_options())
            page = await context.new_page()
            return page, context, browser, playwright
        except BaseException:
            await _quiet_close(page)
            await _quiet_close(context)
            await _quiet_close(browser)
            await _quiet_close(playwright, method="stop")
            raise

    async def _prepare_widget(self, page: Any, deadline: float) -> None:
        remaining_ms = max(500, int((deadline - time.monotonic()) * 1000))
        try:
            await page.wait_for_selector(
                WIDGET_SELECTOR,
                state="attached",
                timeout=min(5000, remaining_ms),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        try:
            await page.eval_on_selector(
                WIDGET_SELECTOR,
                "el => { el.style.width = '70px'; }",
                timeout=1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _read_token(self, page: Any) -> str:
        try:
            value = await page.input_value(TOKEN_INPUT, timeout=1000)
            if isinstance(value, str) and value.strip():
                return value.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        try:
            value = await page.evaluate(_READ_TOKEN_JS)
        except asyncio.CancelledError:
            raise
        except Exception:
            return ""
        return value.strip() if isinstance(value, str) else ""

    async def _wait_token(self, page: Any, deadline: float) -> str:
        for _ in range(self.max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            token = await self._read_token(page)
            if token:
                return token

            try:
                await page.locator(WIDGET_SELECTOR).first.click(timeout=1000)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

            pause = min(self.poll_interval, max(0.0, deadline - time.monotonic()))
            if pause > 0:
                await asyncio.sleep(pause)
        return ""

    async def solve(
        self,
        url: str,
        sitekey: str,
        action: str | None = None,
        cdata: str | None = None,
    ) -> TurnstileResult:
        started = time.monotonic()
        clean_key = _clean_sitekey(sitekey)
        if not clean_key:
            return TurnstileResult(
                turnstile_value=None,
                elapsed_time_seconds=0.0,
                status="failure",
                reason="пустой sitekey",
            )
        try:
            target = _target_url(url)
        except TurnstileError as exc:
            return TurnstileResult(None, 0.0, "failure", str(exc))

        page = owned_context = owned_browser = playwright = None
        try:
            page, owned_context, owned_browser, playwright = await self._open_page()
            page_html = _widget_html(
                clean_key,
                _clean_option(action),
                _clean_option(cdata),
            )

            async def fulfill(route: Any) -> None:
                try:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=page_html,
                    )
                except Exception as exc:
                    logger.debug("turnstile: route.fulfill failed: %r", exc)

            await page.route(lambda candidate: _same_url(candidate, target), fulfill)
            timeout_ms = max(1000, int(self.timeout * 1000))
            await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            deadline = started + self.timeout
            await self._prepare_widget(page, deadline)
            token = await self._wait_token(page, deadline)

            elapsed = round(time.monotonic() - started, 3)
            if token:
                logger.debug("turnstile: решён за %.3fс", elapsed)
                return TurnstileResult(token, elapsed, "success")

            logger.debug("turnstile: токен не получен за %.3fс", elapsed)
            return TurnstileResult(
                None,
                elapsed,
                "failure",
                "виджет не отдал токен за отведённое время",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 3)
            logger.debug("turnstile: ошибка решения: %r", exc)
            return TurnstileResult(None, elapsed, "failure", str(exc) or type(exc).__name__)
        finally:
            await _quiet_close(page)
            await _quiet_close(owned_context)
            await _quiet_close(owned_browser)
            await _quiet_close(playwright, method="stop")

async def _quiet_close(resource: Any, *, method: str = "close") -> None:
    if resource is None:
        return
    closer = getattr(resource, method, None)
    if not callable(closer):
        return
    try:
        await closer()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("turnstile: cleanup %s failed: %r", method, exc)

async def solve_for_page(
    page: Any,
    *,
    url: str | None = None,
    sitekey: str | None = None,
    action: str | None = None,
    cdata: str | None = None,
    timeout: float = 30.0,
    apply: bool = True,
    strict: bool = False,
    solver: TurnstileSolver | None = None,
) -> str | None:
    try:
        challenge = await detect_turnstile(page, sitekey=sitekey, action=action)
        if challenge is None:
            return None

        target = str(url or challenge.url or getattr(page, "url", "") or "").strip()
        if not target:
            raise TurnstileError("Turnstile: не удалось определить URL страницы")

        if solver is None:
            context = getattr(page, "context", None)
            if context is None:
                raise TurnstileError(
                    "Turnstile: у страницы нет browser context; "
                    "передайте solver=TurnstileSolver(...) явно"
                )
            solver = TurnstileSolver(context=context, timeout=timeout)
        result = await solver.solve(
            url=target,
            sitekey=challenge.sitekey,
            action=_clean_option(action) or challenge.action,
            cdata=_clean_option(cdata) or challenge.cdata,
        )
        if not result.is_solved:
            raise TurnstileTimeout(result.reason or "Turnstile не отдал токен")

        token = result.token
        if apply:
            await apply_turnstile_token(
                page,
                token,
                selector=challenge.response_selector,
            )
        return token
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if strict:
            if isinstance(exc, TurnstileError):
                raise
            raise TurnstileError(f"Turnstile: {exc}") from exc
        logger.warning("turnstile: пропущен: %s", exc)
        return None

async def get_turnstile_token(
    url: str,
    sitekey: str,
    action: str | None = None,
    cdata: str | None = None,
    debug: bool = False,
    headless: bool = True,
    useragent: str | None = None,
    browser_type: str = "chromium",
    *,
    proxy: Any = None,
    timeout: float = 30.0,
    context: Any = None,
) -> dict[str, Any]:
    if debug:
        logger.debug("turnstile: solve %s (%s)", url, sitekey)
    solver = TurnstileSolver(
        context=context,
        proxy=proxy,
        headless=headless,
        useragent=useragent,
        timeout=timeout,
        browser_type=browser_type,
    )
    result = await solver.solve(
        url=url,
        sitekey=sitekey,
        action=action,
        cdata=cdata,
    )
    return result.as_dict()

class AsyncTurnstileSolver(TurnstileSolver):

    def __init__(
        self,
        debug: bool = False,
        headless: bool = True,
        useragent: str | None = None,
        browser_type: str = "chromium",
        **kwargs: Any,
    ) -> None:
        self.debug = bool(debug)
        super().__init__(
            headless=headless,
            useragent=useragent,
            browser_type=browser_type,
            **kwargs,
        )
