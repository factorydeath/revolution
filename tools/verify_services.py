#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import pathlib
import random
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Iterable

import aiohttp

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from attack import (
    DEFAULT_SERVICES_PATH,
    DYNAMIC_TOKENS,
    YANDEX_TOKENS,
    Template,
    build_job,
    csrf_token,
    fill_tokens,
    make_formatter,
    parse_templates,
    prepare_tokens,
    unresolved_tokens,
)
from proxies import load_proxies
from recaptcha import RecaptchaCache
from yandex_captcha import YandexCaptchaCache

SAFE_PROBE = {"RU": "70000000000", "UA": "380000000000"}
DEAD_ENDPOINT = frozenset({404, 405, 410, 501})
KEEP = frozenset({"ok", "chain", "alive", "rate_limited", "api_error", "captcha"})


class CaptchaProbeError(RuntimeError):
    pass


USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
)


@dataclass(slots=True)
class Probe:
    name: str
    country: str
    method: str
    url: str
    verdict: str = "unchecked"
    status: int | None = None
    error: str = ""
    ms: int = 0
    attempts: int = 0
    note: str = ""
    proxy: str = ""

    @property
    def keep(self) -> bool:
        return self.verdict in KEEP


class Routes:
    def __init__(self, session: aiohttp.ClientSession, proxies: list) -> None:
        self._session = session
        self._proxies = proxies
        self._rng = random.Random()
        self._sessions: dict[str, aiohttp.ClientSession] = {}

    def __bool__(self) -> bool:
        return bool(self._proxies)

    def pick(self, country: str = ""):
        if not self._proxies:
            return None
        if country:
            c = country.upper().strip()
            country_proxies = [p for p in self._proxies if getattr(p, "country", "") == c]
            if country_proxies:
                return self._rng.choice(country_proxies)
            return None
        return self._rng.choice(self._proxies)

    async def open(self, proxy):
        if proxy is None:
            return self._session, {}
        if not proxy.is_socks:
            return self._session, proxy.request_kwargs()
        session = self._sessions.get(proxy.key)
        if session is None or session.closed:
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(
                proxy.proxy_url, limit=8, ssl=False, rdns=True
            )
            session = aiohttp.ClientSession(
                connector=connector,
                timeout=self._session.timeout,
                max_line_size=32768,
                max_field_size=32768,
            )
            self._sessions[proxy.key] = session
        return session, {}

    async def aclose(self) -> None:
        for session in self._sessions.values():
            if not session.closed:
                await session.close()
        self._sessions.clear()


def classify(status: int | None, error: str | None, attempt: int, attempts: int) -> str:
    if error is not None:
        return "retry" if attempt < attempts else "dead_host"
    if status is None:
        return "dead_host"
    if 200 <= status < 400:
        return "ok"
    if status == 429:
        return "rate_limited"
    if status in DEAD_ENDPOINT:
        return "dead_endpoint"
    if 400 <= status < 500:
        return "alive"
    if status >= 500:
        return "retry" if attempt < attempts else "error_server"
    return "alive"


async def mint_tokens(
    job,
    digits: str,
    proxy_url: str | None,
    *,
    recaptcha_timeout: float,
    yandex_timeout: float,
) -> dict[str, str]:
    tokens: dict[str, str] = {}
    if job.recaptcha_v3:
        cache = RecaptchaCache(recaptcha_timeout)
        tokens["RECAPTCHA"] = await cache.token(job.name, job.recaptcha_v3, proxy_url)
    if job.yandex_captcha:
        yandex = YandexCaptchaCache(yandex_timeout)
        try:
            token = await yandex.token(job.name, job.yandex_captcha, proxy_url)
        finally:
            await yandex.aclose()
        for name in YANDEX_TOKENS:
            tokens[name] = token
    return tokens


async def probe_one(
    template: Template,
    job,
    *,
    routes: Routes,
    attempts: int,
    pause: float,
    digits: str,
    recaptcha_timeout: float = 20.0,
    yandex_timeout: float = 25.0,
) -> Probe:
    probe = Probe(
        name=template.name,
        country=template.country,
        method=job.method,
        url=job.url,
    )
    body = job.body.encode("utf-8") if job.has_body else None
    raw_body = body

    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        probe.attempts = attempt
        headers = dict(job.headers)
        headers["User-Agent"] = USER_AGENTS[(attempt - 1) % len(USER_AGENTS)]
        if job.has_body and job.content_type:
            headers["Content-Type"] = job.content_type

        proxy = routes.pick(template.country)
        session, kwargs = await routes.open(proxy)
        probe.proxy = proxy.label if proxy is not None else ""
        body = raw_body
        url, request_headers = job.url, headers

        error: str | None = None
        status: int | None = None
        rejected = False
        captcha_failed = False
        accepted = not template.api_ok
        try:
            dynamic: dict[str, str] = {}
            if job.csrf is not None:
                dynamic["CSRF"] = await csrf_token(session, job.csrf, kwargs)
            if job.needs_uuid:
                dynamic["UUID"] = str(uuid.uuid4())
            if job.recaptcha_v3 or job.yandex_captcha:
                try:
                    tokens = await mint_tokens(
                        job,
                        digits,
                        proxy.proxy_url if proxy else None,
                        recaptcha_timeout=recaptcha_timeout,
                        yandex_timeout=yandex_timeout,
                    )
                except Exception as exc:
                    raise CaptchaProbeError(f"{type(exc).__name__}: {exc}") from exc
                dynamic.update(tokens)
            if dynamic:
                url = fill_tokens(job.url, dynamic)
                body = (
                    fill_tokens(job.body, dynamic).encode("utf-8")
                    if job.has_body
                    else None
                )
                request_headers = {
                    key: fill_tokens(value, dynamic) for key, value in headers.items()
                }
            async with session.request(
                job.method,
                url,
                headers=request_headers,
                data=body,
                allow_redirects=True,
                **kwargs,
            ) as response:
                status = response.status
                if 200 <= status < 300 and (template.api_error or template.api_ok):
                    chunk = await response.content.read(8192)
                    if template.api_error:
                        rejected = template.api_error.encode() in chunk
                    if template.api_ok:
                        accepted = template.api_ok.encode() in chunk
                    response.close()
                else:
                    await _drain(response)
        except asyncio.CancelledError:
            raise
        except CaptchaProbeError as exc:
            error = str(exc)
            captcha_failed = True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        failed = rejected or not accepted
        captcha = captcha_failed
        if captcha:
            verdict = "captcha"
        elif failed:
            verdict = "api_error"
        else:
            verdict = classify(status, error, attempt, attempts)
        probe.status = status
        probe.error = (error or "")[:200]
        if captcha:
            probe.note = "капча не решилась: нужен другой прокси или живой sitekey"
        elif failed:
            probe.note = "API не подтвердил отправку"
        probe.ms = int((time.monotonic() - started) * 1000)
        if verdict != "retry":
            probe.verdict = verdict
            return probe
        if pause:
            await asyncio.sleep(pause * attempt)
    probe.verdict = "dead_host" if probe.status is None else "error_server"
    return probe


async def _drain(response: aiohttp.ClientResponse) -> None:
    if response.content_length is not None and response.content_length > 256 * 1024:
        response.close()
        return
    try:
        await response.read()
    except Exception:
        response.close()


async def host_touch(routes: Routes, url: str) -> str:
    try:
        parsed = aiohttp.client.URL(url)
        root = f"{parsed.scheme}://{parsed.host}/"
        session, kwargs = await routes.open(routes.pick())
        async with session.get(root, allow_redirects=True, **kwargs) as response:
            await _drain(response)
            return f"root {response.status}"
    except Exception as exc:
        return f"root down ({type(exc).__name__})"


async def run(args: argparse.Namespace) -> list[Probe]:
    services_path = pathlib.Path(args.services)
    raw = json.loads(await asyncio.to_thread(services_path.read_text, encoding="utf-8"))
    templates = parse_templates(raw, strict=False)

    countries = {code.strip().upper() for code in args.countries.split(",") if code.strip()}
    if countries:
        templates = [t for t in templates if t.country in countries]

    chain_probes = [
        Probe(
            name=tpl.name,
            country=tpl.country,
            method=tpl.method,
            url=tpl.url,
            verdict="chain",
            note="HTTP-цепочка из нескольких запросов, проверяется вручную",
        )
        for tpl in templates
        if tpl.chain
    ]
    templates = [tpl for tpl in templates if not tpl.chain]
    if not templates:
        if not chain_probes:
            raise SystemExit("после фильтра не осталось шаблонов")
        return chain_probes

    if args.mode == "own":
        if not args.phone:
            raise SystemExit("--mode own требует --phone +<ваш номер>")
        if not args.i_own_this_number:
            raise SystemExit(
                "Режим own шлёт РЕАЛЬНЫЕ SMS на указанный номер.\n"
                "Убедитесь, что номер ваш, и добавьте --i-own-this-number.\n"
                "Для проверки без SMS используйте режим safe (по умолчанию)."
            )
        digits = "".join(ch for ch in args.phone if ch.isdigit())
        if len(digits) < 11:
            raise SystemExit("--phone: слишком короткий номер")
        digits_by_country = {"RU": digits, "UA": digits}
        attempts = 1
        pause = 0.0
    else:
        digits_by_country = dict(SAFE_PROBE)
        attempts = max(1, args.attempts)
        pause = args.pause

    timeout = aiohttp.ClientTimeout(total=args.timeout, connect=min(args.timeout, 6.0))
    connector = aiohttp.TCPConnector(limit=args.concurrency, ssl=False)

    entries: list = []
    if args.proxies:
        entries = load_proxies(args.proxies)
        if not entries:
            raise SystemExit(f"не прочитал прокси из {args.proxies}")
        socks = sum(1 for entry in entries if entry.is_socks)
        print(
            f"  прокси: {len(entries)} (socks: {socks}), случайный на каждую попытку",
            flush=True,
        )

    results: list[Probe] = []
    skipped: list[Probe] = []

    async with contextlib.AsyncExitStack() as stack:
        session = await stack.enter_async_context(
            aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                max_line_size=32768,
                max_field_size=32768,
            )
        )
        routes = Routes(session, entries)
        stack.push_async_callback(routes.aclose)

        providers = {tpl.prepare for tpl in templates if tpl.prepare}
        prepared = await prepare_tokens(session, providers) if providers else {}
        missing = providers - prepared.keys()
        if missing:
            for name in sorted(missing):
                print(f"  prepare {name} недоступен — шаблоны пропущены", flush=True)
            skipped.extend(
                Probe(
                    name=tpl.name,
                    country=tpl.country,
                    method=tpl.method,
                    url=tpl.url,
                    verdict="prepare_failed",
                    note="не удалось получить prepare-токены",
                )
                for tpl in templates
                if tpl.prepare in missing
            )
            templates = [tpl for tpl in templates if tpl.prepare not in missing]
            if not templates:
                return skipped

        extra: dict[str, str] = {}
        for tokens in prepared.values():
            extra.update(tokens)
        extra.update({name: "PENDING" for name in (*YANDEX_TOKENS, "RECAPTCHA")})
        formatters = {
            code: make_formatter(digits, extra) for code, digits in digits_by_country.items()
        }

        async def worker(template: Template) -> None:
            job = build_job(template, formatters[template.country])
            blob = job.url + "\x00" + job.body + "\x00" + "\x00".join(v for _, v in job.headers)
            leftover = set(unresolved_tokens(blob)) - set(DYNAMIC_TOKENS)
            if job.csrf is not None:
                leftover.discard("CSRF")
            if leftover:
                skipped.append(
                    Probe(
                        name=template.name,
                        country=template.country,
                        method=job.method,
                        url=job.url,
                        verdict="broken_template",
                        note=f"остались токены после подстановки: {sorted(leftover)}",
                    )
                )
                return
            results.append(
                await probe_one(
                    template,
                    job,
                    routes=routes,
                    attempts=attempts,
                    pause=pause,
                    digits=digits_by_country[template.country],
                    recaptcha_timeout=args.recaptcha_timeout,
                    yandex_timeout=args.yandex_timeout,
                )
            )

        total = len(templates)
        for start in range(0, total, args.concurrency):
            await asyncio.gather(
                *(worker(t) for t in templates[start : start + args.concurrency])
            )
            print(f"  проверено {min(start + args.concurrency, total)}/{total}", flush=True)

        if args.mode == "safe" and args.root_check:
            dead_hosts = [p for p in results if p.verdict == "dead_host" and not p.keep]
            if dead_hosts:
                print(f"  уточняю {len(dead_hosts)} недоступных хостов…", flush=True)
                for probe in dead_hosts:
                    probe.note = await host_touch(routes, probe.url)

    return results + skipped + chain_probes


def summarize(results: Iterable[Probe]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for probe in results:
        counts[probe.verdict] = counts.get(probe.verdict, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def _write_report(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверка боеспособности шаблонов services.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "safe (по умолчанию) подставляет не абонентские номера и проверяет "
            "живость эндпоинта без отправки SMS. own делает ровно одну отправку "
            "на ваш номер и требует --i-own-this-number."
        ),
    )
    parser.add_argument("--services", default=str(DEFAULT_SERVICES_PATH))
    parser.add_argument("--mode", choices=("safe", "own"), default="safe")
    parser.add_argument("--phone", default="", help="только для --mode own: ВАШ номер")
    parser.add_argument(
        "--i-own-this-number",
        action="store_true",
        help="подтверждение для --mode own: номер принадлежит вам",
    )
    parser.add_argument("--countries", default="", help="фильтр, например RU или RU,UA")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--recaptcha-timeout", type=float, default=20.0)
    parser.add_argument("--yandex-timeout", type=float, default=25.0)
    parser.add_argument("--attempts", type=int, default=3, help="попыток на шаблон (safe)")
    parser.add_argument("--pause", type=float, default=0.5, help="пауза между попытками")
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument(
        "--root-check",
        action="store_true",
        help="для dead_host дополнительно дёрнуть корень хоста (safe)",
    )
    parser.add_argument(
        "--proxies",
        default="",
        help="proxies.json / proxies.txt: каждая попытка идёт через случайный прокси",
    )
    parser.add_argument("--report", default=str(ROOT / "tools" / "services_report.json"))
    parser.add_argument("--write", action="store_true", help="перезаписать services.json")
    args = parser.parse_args()

    started = time.monotonic()
    results = asyncio.run(run(args))
    elapsed = time.monotonic() - started

    counts = summarize(results)
    kept = [p for p in results if p.keep]
    dropped = [p for p in results if not p.keep]

    print(f"\nрежим: {args.mode} · время: {elapsed:.1f}s · шаблонов: {len(results)}")
    for verdict, count in counts.items():
        print(f"  {verdict:16} {count}")
    print(f"\nоставить: {len(kept)} · удалить: {len(dropped)}")

    if dropped:
        print("\nнерабочие:")
        for probe in sorted(dropped, key=lambda p: (p.verdict, p.name)):
            detail = f"HTTP {probe.status}" if probe.status else (probe.error or probe.note)
            extra = f" · {probe.note}" if probe.note and probe.note != detail else ""
            print(f"  {probe.verdict:16} {probe.name:30} {detail}{extra}")

    by_country: dict[str, dict[str, int]] = {}
    for probe in results:
        bucket = by_country.setdefault(probe.country, {"keep": 0, "drop": 0})
        bucket["keep" if probe.keep else "drop"] += 1
    print("\nпо странам:")
    for code, bucket in sorted(by_country.items()):
        print(f"  {code}: оставить {bucket['keep']}, удалить {bucket['drop']}")

    report = {
        "mode": args.mode,
        "elapsed_sec": round(elapsed, 1),
        "counts": counts,
        "by_country": by_country,
        "kept": sorted(p.name for p in kept),
        "dropped": [asdict(p) for p in sorted(dropped, key=lambda p: p.name)],
        "results": [asdict(p) for p in sorted(results, key=lambda p: p.name)],
    }
    report_path = pathlib.Path(args.report)
    _write_report(report_path, report)
    print(f"\nотчёт: {report_path}")

    if args.write:
        drop_names = {p.name for p in dropped}
        if not drop_names:
            print("нечего удалять")
            return 0
        services_path = pathlib.Path(args.services)
        raw = json.loads(services_path.read_text(encoding="utf-8"))
        filtered = [item for item in raw if item.get("name") not in drop_names]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = services_path.with_name(f"{services_path.name}.{stamp}.bak")
        shutil.copy2(services_path, backup)
        services_path.write_text(
            json.dumps(filtered, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(
            f"services.json перезаписан: {len(filtered)} шаблонов "
            f"(было {len(raw)}, удалено {len(raw) - len(filtered)})\n"
            f"бэкап: {backup}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
