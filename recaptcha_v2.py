from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from pypasser.captcha.recaptcha.v2 import asolve
from pypasser.exceptions import IpBlock

logger = logging.getLogger(__name__)

__all__ = [
    "RECAPTCHA_INPUT",
    "RecaptchaBlocked",
    "RecaptchaV2Challenge",
    "RecaptchaV2Error",
    "RecaptchaV2Result",
    "RecaptchaV2Timeout",
    "apply_recaptcha_token",
    "detect_recaptcha_v2",
    "solve_v2_for_page",
]

RECAPTCHA_INPUT = "#g-recaptcha-response"
ANCHOR_FRAME = "iframe[name^='a-']"
_ANCHOR_SRC = "google.com/recaptcha/api2/anchor"

class RecaptchaV2Error(RuntimeError):
    pass

class RecaptchaV2Timeout(RecaptchaV2Error):
    pass

class RecaptchaBlocked(RecaptchaV2Error):
    pass

@dataclass(frozen=True, slots=True)
class RecaptchaV2Challenge:
    sitekey: str | None = None
    url: str = ""
    response_selector: str = RECAPTCHA_INPUT

@dataclass(frozen=True)
class RecaptchaV2Result:

    is_solved: bool
    response_token: str | None = None
    elapsed_time_seconds: float = 0.0
    status: str = "failure"
    reason: str | None = None

    @property
    def token(self) -> str:
        return self.response_token or ""

    @property
    def ok(self) -> bool:
        return self.is_solved and bool(self.response_token)

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_solved": self.is_solved,
            "response_token": self.response_token,
            "elapsed_time_seconds": self.elapsed_time_seconds,
            "status": self.status,
            "reason": self.reason,
        }

_DETECT_JS = r"""() => {
    const result = {anchors: [], sitekeys: [], url: location.href};

    for (const frame of document.querySelectorAll('iframe[src]')) {
        try {
            const src = new URL(frame.src, location.href);
            if (!src.hostname.endsWith('google.com')) continue;
            if (src.pathname.startsWith('/recaptcha/api2/anchor')) {
                result.anchors.push(src.toString());
            }
        } catch (_) {
            /* ignore malformed iframe URLs */
        }
    }

    for (const el of document.querySelectorAll('[data-sitekey]')) {
        const sitekey = String(el.getAttribute('data-sitekey') || '').trim();
        if (!sitekey || result.sitekeys.includes(sitekey)) continue;
        const widget = el.closest('.g-recaptcha');
        const size = (widget && widget.getAttribute('data-size')) || '';
        if (size !== 'invisible') result.sitekeys.push(sitekey);
    }

    return result;
}"""

_APPLY_TOKEN_JS = r"""payload => {
    const {token, selector} = payload;
    const targets = document.querySelectorAll(selector);
    let applied = false;
    for (const input of targets) {
        input.value = token;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        applied = true;
    }

    for (const el of document.querySelectorAll('[data-callback]')) {
        const name = el.getAttribute('data-callback');
        if (!name) continue;
        try {
            if (typeof window[name] === 'function') window[name](token);
        } catch (_) {
            /* page callback errors must not break the service flow */
        }
    }
    return applied;
}"""

def _clean_sitekey(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 200 or any(char.isspace() for char in text):
        return None
    return text

async def detect_recaptcha_v2(
    page: Any,
    *,
    sitekey: str | None = None,
    timeout: float = 2.0,
) -> RecaptchaV2Challenge | None:
    page_url = str(getattr(page, "url", "") or "")
    explicit = _clean_sitekey(sitekey)
    if explicit:
        return RecaptchaV2Challenge(sitekey=explicit, url=page_url)

    try:
        info = await asyncio.wait_for(page.evaluate(_DETECT_JS), max(0.2, float(timeout)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("recaptcha v2: не удалось просканировать страницу: %r", exc)
        return None

    if not isinstance(info, Mapping):
        return None

    sitekeys = info.get("sitekeys") or []
    if isinstance(sitekeys, str):
        sitekeys = [sitekeys]
    found = ""
    for raw in sitekeys:
        found = _clean_sitekey(raw) or ""
        if found:
            break

    anchors = info.get("anchors") or []
    if isinstance(anchors, str):
        anchors = [anchors]
    has_anchor = any(_ANCHOR_SRC in str(src) for src in anchors)

    if not found and not has_anchor:
        return None
    return RecaptchaV2Challenge(
        sitekey=found or None,
        url=str(info.get("url") or page_url or ""),
    )

async def apply_recaptcha_token(
    page: Any,
    token: str,
    *,
    selector: str = RECAPTCHA_INPUT,
) -> bool:
    value = str(token or "").strip()
    if not value:
        return False
    try:
        result = await page.evaluate(
            _APPLY_TOKEN_JS,
            {"token": value, "selector": selector or RECAPTCHA_INPUT},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("recaptcha v2: не удалось подставить токен: %r", exc)
        return False
    return bool(result)

SolverFn = Callable[[Any], Awaitable[Any]]

async def solve_v2_for_page(
    page: Any,
    *,
    sitekey: str | None = None,
    timeout: float = 120.0,
    step_timeout: float = 10.0,
    attempts: int = 3,
    play: bool = False,
    backend: str = "patchright",
    apply: bool = True,
    strict: bool = False,
    solver: SolverFn | None = None,
) -> str | None:
    started = time.monotonic()
    try:
        challenge = await detect_recaptcha_v2(page, sitekey=sitekey)
        if challenge is None:
            return None

        if solver is None:
            async def default_solver(target: Any) -> Any:
                return await asolve(
                    target,
                    backend=backend,
                    play=play,
                    attempts=max(1, int(attempts)),
                    timeout=step_timeout,
                )

            solver = default_solver

        try:
            raw = await asyncio.wait_for(solver(page), max(1.0, float(timeout)))
        except IpBlock as exc:
            raise RecaptchaBlocked(f"reCAPTCHA V2: IP заблокирован: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise RecaptchaV2Timeout(
                f"reCAPTCHA V2 не уложилась в {float(timeout):.0f}с"
            ) from exc

        token = str(getattr(raw, "response_token", "") or "").strip()
        solved = bool(raw is not None and getattr(raw, "is_solved", False) and token)
        elapsed = round(time.monotonic() - started, 3)
        if not solved:
            raise RecaptchaV2Timeout("reCAPTCHA V2 не отдала токен")

        if apply:
            await apply_recaptcha_token(
                page,
                token,
                selector=challenge.response_selector,
            )
        logger.debug("recaptcha v2: решён за %.3fс", elapsed)
        return token
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if strict:
            if isinstance(exc, RecaptchaV2Error):
                raise
            raise RecaptchaV2Error(f"reCAPTCHA V2: {exc}") from exc
        logger.warning("recaptcha v2: пропущен: %s", exc)
        return None
