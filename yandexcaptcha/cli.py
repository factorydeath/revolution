from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import sys
import time
from collections.abc import Sequence
from typing import Any

import aiohttp

from . import __version__
from .config import Settings
from .discover import DiscoveryError, discover, host_of, normalize_url
from .exceptions import ChallengeRequiredError, YandexCaptchaError
from .fingerprint import Profile
from .models import SolveResult
from .proxy import (
    Proxy,
    ProxyPool,
    connector_for,
    detect_proxy,
    load_proxies,
    parse_proxy,
    probe_proxy,
    request_proxy,
    resolve_pool,
)
from .solver import YandexCaptchaSolver, solve_many

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FAILED = 2
EXIT_CHALLENGE = 3

FORMATS = ("plain", "json", "jsonl", "env", "shell")


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="debug logging on stderr",
    )
    common.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
        help="suppress progress output",
    )
    common.add_argument(
        "--config", metavar="FILE", default=argparse.SUPPRESS,
        help="JSON config file",
    )

    parser = argparse.ArgumentParser(
        prog="yandexcaptcha",
        description="Yandex SmartCaptcha invisible-mode solver.",
        epilog="Run `yandexcaptcha <command> --help` for command details.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--config", metavar="FILE", help="JSON config file")

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    solve = sub.add_parser(
        "solve",
        help="mint captcha tokens",
        parents=[common],
        description="Mint one or more tokens. Targets may be page URLs (the "
        "sitekey is discovered automatically) or a sitekey/host pair.",
    )
    solve.add_argument("targets", nargs="*", metavar="TARGET", help="page URL(s), or ysc1_... keys")
    solve.add_argument("-s", "--sitekey", help="ysc1_... key (skips discovery)")
    solve.add_argument("--host", help="site host, e.g. example.com")
    solve.add_argument("--href", help="page URL used for the widget (default https://<host>/)")
    solve.add_argument("--url", help="page URL to discover the sitekey from")
    solve.add_argument("-n", "--count", type=int, help="tokens per target (default 1)")
    solve.add_argument("-c", "--concurrency", type=int, help="parallel solves (default 8)")
    solve.add_argument("-f", "--format", choices=FORMATS, default="plain", help="output format")
    solve.add_argument("--profile", help="fingerprint profile JSON file or rdata string")
    solve.add_argument("--randomize", action="store_true", default=None, help="vary screen size and canvas digests")
    solve.add_argument(
        "--proxy",
        help="proxy URL, host:port:user:pass entry, or a file with one per line",
    )
    solve.add_argument(
        "--proxy-file", dest="proxy_file", metavar="FILE",
        help="rotate over the proxies listed in FILE",
    )
    solve.add_argument(
        "--cipher", choices=("a", "b"),
        help="PGreed cipher build for fresh payloads (default: a)",
    )
    solve.add_argument(
        "--proxy-strategy", dest="proxy_strategy",
        choices=("round-robin", "random", "sticky"),
        help="how a batch picks a proxy from the pool (default: round-robin)",
    )
    solve.add_argument("--lang", help="widget language (default ru)")
    solve.add_argument("--timeout", type=float, help="per-request timeout in seconds")
    solve.add_argument("--retries", type=int, help="attempts per token")
    solve.add_argument("--user-agent", dest="user_agent", help="override the browser UA")
    solve.add_argument("--endpoint", help="captcha endpoint (advanced)")
    solve.add_argument(
        "--no-discovery",
        action="store_true",
        help="never fetch a page to find the sitekey",
    )
    solve.add_argument(
        "--stdin",
        action="store_true",
        help="read extra targets from stdin, one per line",
    )

    dec = sub.add_parser(
        "discover",
        help="find the sitekey of a page",
        parents=[common],
        description="Fetch page(s) and print the SmartCaptcha sitekey, host and href.",
    )
    dec.add_argument("urls", nargs="+", metavar="URL")
    dec.add_argument("-f", "--format", choices=("table", "json", "jsonl", "plain"), default="table")
    dec.add_argument("-c", "--concurrency", type=int, default=5)
    dec.add_argument("--no-deep", action="store_true", help="do not scan linked scripts")
    dec.add_argument("--proxy")
    dec.add_argument("--proxy-file", dest="proxy_file")
    dec.add_argument("--timeout", type=float, default=15.0)

    ver = sub.add_parser(
        "verify",
        help="redeem a token at a site endpoint",
        parents=[common],
        description="POST a token to an API endpoint and print the response. "
        "Useful to confirm that a real backend accepts minted tokens.",
    )
    ver.add_argument("endpoint_url", metavar="ENDPOINT", help="URL that consumes the token")
    ver.add_argument("--token", help="token to submit (otherwise one is minted)")
    ver.add_argument("--url", help="page URL to mint a token for")
    ver.add_argument("--sitekey")
    ver.add_argument("--host")
    ver.add_argument(
        "--body",
        default='{"recaptchaToken":"{{token}}"}',
        help="request body; {{token}} is replaced (default: JSON with recaptchaToken)",
    )
    ver.add_argument("--body-file", help="read the body template from a file")
    ver.add_argument("-H", "--header", action="append", default=[], metavar="K:V")
    ver.add_argument("-X", "--method", default="POST")
    ver.add_argument("--origin", help="set Origin and Referer to this URL")
    ver.add_argument("--timeout", type=float, default=20.0)
    ver.add_argument("--proxy")
    ver.add_argument("--show-token", action="store_true", help="also print the token")
    ver.add_argument("--expect", type=int, help="expected HTTP status (default: any 2xx)")

    proxy_parser = sub.add_parser(
        "proxy",
        help="check proxies",
        description="Probe proxy endpoints and report their exit IP and latency.",
        parents=[common],
    )
    proxy_sub = proxy_parser.add_subparsers(
        dest="proxy_command", required=True, metavar="ACTION"
    )
    pcheck = proxy_sub.add_parser("check", help="probe one or more proxies")
    pcheck.add_argument("entries", nargs="*", metavar="PROXY", help="proxy URLs or host:port:user:pass")
    pcheck.add_argument("--file", dest="proxy_file", help="read proxies from a file")
    pcheck.add_argument("-f", "--format", choices=("table", "json", "jsonl"), default="table")
    pcheck.add_argument("--timeout", type=float, default=15.0)
    pcheck.add_argument("--url", default="https://api.ipify.org", help="probe URL")

    doc = sub.add_parser(
        "doctor",
        help="self-test the installation",
        parents=[common],
        description="Check Python, dependencies, connectivity and a full solve.",
    )
    doc.add_argument("--url", help="page to test against")
    doc.add_argument("--sitekey")
    doc.add_argument("--host")
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--proxy", help="proxy URL or host:port:user:pass")
    doc.add_argument("--proxy-file", dest="proxy_file", help="rotate over a proxy list")
    doc.add_argument("--timeout", type=float, default=15.0)

    profile = sub.add_parser("profile", help="inspect or convert fingerprints", parents=[common])
    psub = profile.add_subparsers(dest="profile_command", required=True, metavar="ACTION")

    pdec = psub.add_parser("decode", help="decode an rdata string into plain text")
    pdec.add_argument("rdata", nargs="?", help="rdata value (default: bundled capture)")
    pdec.add_argument("--file", help="read rdata from a file")
    pdec.add_argument("--har", help="extract rdata from a .har capture")

    penc = psub.add_parser("encode", help="encode a profile JSON into rdata")
    penc.add_argument("file", help="profile JSON from `profile decode`")
    penc.add_argument("--timestamp", type=int)

    pshow = psub.add_parser("show", help="print the bundled profile")
    pshow.add_argument("--json", action="store_true")

    token = sub.add_parser("token", help="decode a spravka token", parents=[common])
    token.add_argument("value")

    return parser


def _looks_like_sitekey(value: str) -> bool:
    return value.startswith(("ysc0_", "ysc1_"))


def _looks_like_url(value: str) -> bool:
    return "://" in value or "/" in value or "." in value or ":" in value


def _progress_printer(total: int, quiet: bool):
    if quiet or not sys.stderr.isatty():
        return None

    def report(index: int, result: SolveResult | BaseException) -> None:
        if isinstance(result, SolveResult):
            note = f"ok   {result.elapsed * 1000:5.0f} ms"
        elif isinstance(result, ChallengeRequiredError):
            note = f"challenge ({result.kind})"
        else:
            note = f"{type(result).__name__}: {result}"
        print(f"[{index + 1}/{total}] {note}", file=sys.stderr, flush=True)

    return report


def _load_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    config_path = getattr(args, "config", None)
    if config_path:
        settings = settings.overlay(Settings.from_file(config_path))
    overrides = {
        name: getattr(args, name, None)
        for name in (
            "sitekey",
            "host",
            "href",
            "url",
            "profile",
            "randomize",
            "proxy",
            "proxy_file",
            "proxy_strategy",
            "cipher",
            "lang",
            "timeout",
            "retries",
            "user_agent",
            "endpoint",
            "concurrency",
            "count",
        )
    }
    return settings.merge(**overrides)


def _proxy_pool(settings: Settings) -> list[Proxy]:
    if settings.proxy_file:
        return load_proxies(settings.proxy_file)
    if settings.proxy:
        if os.path.isfile(settings.proxy):
            return load_proxies(settings.proxy)
        entries = [item for item in settings.proxy.split(",") if item.strip()]
        return [parse_proxy(item) for item in entries]
    return []


def _stdin_targets() -> list[str]:
    return [
        line.strip()
        for line in sys.stdin
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _collect_targets(args: argparse.Namespace, settings: Settings) -> list[tuple[str, str, str]]:
    raw: list[str] = list(getattr(args, "targets", []) or [])
    if getattr(args, "stdin", False):
        raw.extend(_stdin_targets())
    if settings.url:
        raw.append(settings.url)

    if not raw:
        if not settings.has_target():
            raise SystemExit(
                "error: no target. Pass a page URL, or --sitekey with --host, "
                "or set YANDEXCAPTCHA_URL / YANDEXCAPTCHA_SITEKEY+host."
            )
        if not settings.sitekey and not settings.url:
            raise SystemExit("error: --sitekey requires --host (or pass a page URL)")

    if not raw and settings.sitekey:
        raw.append(settings.sitekey)

    triples: list[tuple[str, str, str]] = []
    for target in raw:
        if _looks_like_sitekey(target):
            if not settings.host:
                raise SystemExit(f"error: {target} needs --host")
            triples.append((target, settings.host, settings.href or f"https://{settings.host}/"))
        elif _looks_like_url(target):
            page = normalize_url(target)
            if settings.sitekey:
                triples.append(
                    (settings.sitekey, settings.host or host_of(page), settings.href or page)
                )
            else:
                triples.append(("", page, ""))
        else:
            raise SystemExit(f"error: cannot tell whether {target!r} is a URL or a sitekey")

    return triples


async def _resolve_all(
    triples: Sequence[tuple[str, str, str]],
    settings: Settings,
    *,
    no_discovery: bool,
    session: aiohttp.ClientSession | None,
    proxy: Proxy | str | None = None,
) -> list[tuple[str, str, str]]:
    resolved: list[tuple[str, str, str]] = []
    for sitekey, page, href in triples:
        if sitekey:
            host = page if page and "://" not in page and "/" not in page else ""
            if not host and href:
                host = host_of(href)
            if not host:
                raise SystemExit(f"error: {sitekey} needs a host")
            resolved.append((sitekey, host, href or f"https://{host}/"))
            continue
        if no_discovery:
            raise SystemExit(f"error: discovery disabled, cannot resolve {page!r}")
        info = await discover(
            page,
            session=session,
            proxy=proxy,
            verify_ssl=settings.verify_ssl if settings.verify_ssl is not None else True,
        )
        resolved.append((info.sitekey, info.host, href or info.href))
    return resolved


def _emit(results: Sequence[SolveResult | BaseException], fmt: str, quiet: bool) -> int:
    ok = [r for r in results if isinstance(r, SolveResult)]
    failed = [r for r in results if not isinstance(r, SolveResult)]

    if fmt == "plain":
        for result in results:
            if isinstance(result, SolveResult):
                print(result.token)
            elif not quiet:
                print(f"ERROR {type(result).__name__}: {result}", file=sys.stderr)
    elif fmt == "jsonl":
        for result in results:
            print(json.dumps(_payload(result), ensure_ascii=False))
    elif fmt == "json":
        print(json.dumps([_payload(r) for r in results], ensure_ascii=False, indent=2))
    elif fmt == "env":
        for index, result in enumerate(ok, start=1):
            key = "YANDEXCAPTCHA_TOKEN" if len(ok) == 1 else f"YANDEXCAPTCHA_TOKEN_{index}"
            print(f"{key}={result.token}")
        for result in failed:
            print(f"# {type(result).__name__}: {result}", file=sys.stderr)
    elif fmt == "shell":
        for result in ok:
            print(shlex.quote(result.token))
        for result in failed:
            print(f"# {type(result).__name__}: {result}", file=sys.stderr)
    else:
        raise SystemExit(f"unknown format {fmt}")

    if not ok:
        return EXIT_CHALLENGE if any(isinstance(r, ChallengeRequiredError) for r in failed) else EXIT_FAILED
    return EXIT_OK if not failed else EXIT_PARTIAL


def _payload(result: SolveResult | BaseException) -> dict[str, Any]:
    if isinstance(result, SolveResult):
        return {"ok": True, **result.to_dict()}
    body: dict[str, Any] = {
        "ok": False,
        "error": type(result).__name__,
        "message": str(result),
    }
    if isinstance(result, ChallengeRequiredError):
        body["challenge"] = result.kind
        body["image"] = result.image
    return body


async def _cmd_solve(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    triples = _collect_targets(args, settings)

    count = max(1, settings.count or 1)
    concurrency = max(1, settings.concurrency or 8)

    pool = _proxy_pool(settings)
    solver_kwargs = settings.solver_kwargs()
    solver_kwargs.pop("proxy", None)
    solver_kwargs.pop("proxy_file", None)
    solver_kwargs.pop("cipher", None)
    if args.cipher:
        solver_kwargs["profile"] = Profile.reference(args.cipher)
    if pool:
        solver_kwargs["proxy"] = pool[0] if len(pool) == 1 else pool

    first = pool[0] if pool else None
    async with aiohttp.ClientSession() as session:
        resolved = await _resolve_all(
            triples,
            settings,
            no_discovery=args.no_discovery,
            session=None if first is not None else session,
            proxy=first,
        )
        if not args.quiet and sys.stderr.isatty():
            for sitekey, host, href in resolved:
                print(f"target: {host}  sitekey={sitekey}  href={href}", file=sys.stderr)

        jobs = [
            {"sitekey": sitekey, "host": host, "href": href}
            for sitekey, host, href in resolved
            for _ in range(count)
        ]
        results = await solve_many(
            jobs,
            concurrency=concurrency,
            session=session,
            progress=_progress_printer(len(jobs), args.quiet),
            proxy_strategy=settings.proxy_strategy or "round-robin",
            **solver_kwargs,
        )

    return _emit(results, args.format, args.quiet)


async def _cmd_discover(args: argparse.Namespace) -> int:
    settings = Settings()
    settings = settings.merge(proxy=args.proxy, proxy_file=getattr(args, "proxy_file", None))
    pool = _proxy_pool(settings)
    infos: list[Any] = []
    for index, url in enumerate(args.urls):
        proxy = pool[index % len(pool)] if pool else None
        try:
            infos.append(
                await discover(
                    url,
                    deep=not args.no_deep,
                    timeout=args.timeout,
                    proxy=proxy,
                )
            )
        except (DiscoveryError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            infos.append(exc)

    if args.format == "json":
        print(
            json.dumps(
                [
                    info.as_dict()
                    if not isinstance(info, Exception)
                    else {"ok": False, "error": type(info).__name__, "message": str(info)}
                    for info in infos
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.format == "jsonl":
        for info in infos:
            payload = (
                {"ok": True, **info.as_dict()}
                if not isinstance(info, Exception)
                else {"ok": False, "error": type(info).__name__, "message": str(info)}
            )
            print(json.dumps(payload, ensure_ascii=False))
    elif args.format == "plain":
        for info in infos:
            print(info.sitekey if not isinstance(info, Exception) else "")
    else:
        for info in infos:
            if isinstance(info, Exception):
                print(f"FAIL  {type(info).__name__}: {info}")
            else:
                print(f"{info.sitekey}  {info.host}  {info.href}  [{info.source}]")

    failures = sum(1 for info in infos if isinstance(info, Exception))
    return EXIT_OK if not failures else (EXIT_PARTIAL if failures < len(infos) else EXIT_FAILED)


async def _prepare_proxy(value: str | None, timeout: float = 15.0) -> Proxy | None:
    if not value:
        return None
    proxy = parse_proxy(value)
    if proxy.needs_detection:
        proxy = await detect_proxy(proxy, timeout=min(timeout, 10.0))
    return proxy


async def _cmd_verify(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    verify_proxy = await _prepare_proxy(args.proxy, args.timeout)

    token = args.token
    if token is None:
        if not (args.url or (args.sitekey and args.host)):
            raise SystemExit("error: pass --token, or --url / --sitekey+--host to mint one")
        async with aiohttp.ClientSession(
            connector=connector_for(verify_proxy)
        ) as session:
            resolved = await _resolve_all(
                [(args.sitekey or "", args.host or args.url or "", "")],
                settings,
                no_discovery=False,
                session=session,
                proxy=verify_proxy,
            )
            sitekey, host, href = resolved[0]
            solver_kwargs = {
                key: value
                for key, value in settings.solver_kwargs().items()
                if key not in {"proxy", "proxy_file", "session"}
            }
            solver = YandexCaptchaSolver(
                sitekey=sitekey,
                host=host,
                href=href,
                proxy=verify_proxy,
                **solver_kwargs,
            )
            try:
                token = (await solver.solve_detailed()).token
            finally:
                await solver.aclose()
        if not args.quiet:
            print(f"minted token: {token[:48]}…", file=sys.stderr)

    template = args.body
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as fh:
            template = fh.read()
    body = template.replace("{{token}}", token)

    headers: dict[str, str] = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": (
            settings.user_agent
            or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/157.0.0.0 Safari/537.36"
        ),
    }
    if args.origin:
        headers["Origin"] = args.origin
        headers["Referer"] = args.origin.rstrip("/") + "/"
    for raw in args.header:
        if ":" not in raw:
            raise SystemExit(f"error: header must be 'Name: value', got {raw!r}")
        name, value = raw.split(":", 1)
        headers[name.strip()] = value.strip()

    request_kwargs: dict[str, Any] = {
        "headers": headers,
        "proxy": request_proxy(verify_proxy),
        "timeout": aiohttp.ClientTimeout(total=args.timeout),
    }
    if args.method.upper() in ("POST", "PUT", "PATCH") and body:
        request_kwargs["data"] = body.encode()

    async with aiohttp.ClientSession(connector=connector_for(verify_proxy)) as session:
        try:
            async with session.request(args.method.upper(), args.endpoint_url, **request_kwargs) as response:
                status = response.status
                text = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}))
            return EXIT_FAILED

    accepted = status == args.expect if args.expect is not None else 200 <= status < 300
    if args.show_token or not accepted:
        print(json.dumps({"ok": accepted, "status": status, "token": token, "body": text}, ensure_ascii=False))
    else:
        print(json.dumps({"ok": True, "status": status, "body": text}, ensure_ascii=False))
    return EXIT_OK if accepted else EXIT_FAILED


async def _cmd_proxy(args: argparse.Namespace) -> int:
    proxies: list[Proxy] = []
    for entry in args.entries:
        proxies.extend(resolve_pool(entry))
    if args.proxy_file:
        proxies.extend(load_proxies(args.proxy_file))
    if not proxies:
        raise SystemExit("error: pass proxy entries or --file proxies.txt")

    pool = ProxyPool(proxies)
    health = await pool.check(timeout=args.timeout, url=args.url)

    if args.format == "json":
        print(json.dumps([item.to_dict() for item in health], ensure_ascii=False, indent=2))
    elif args.format == "jsonl":
        for item in health:
            print(json.dumps(item.to_dict(), ensure_ascii=False))
    else:
        for item in health:
            mark = "OK  " if item.ok else "FAIL"
            if item.ok:
                latency = (item.latency or 0.0) * 1000
                detail = f"ip={item.ip:<16} {latency:6.0f} ms"
            else:
                detail = item.error or "failed"
            print(f"{mark} {item.proxy.anonymized():<44} {detail}")

    ok = sum(1 for item in health if item.ok)
    if ok == len(health):
        return EXIT_OK
    return EXIT_PARTIAL if ok else EXIT_FAILED


async def _cmd_doctor(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append((name, passed, detail))

    import platform

    check("python", sys.version_info >= (3, 10), platform.python_version())
    try:
        version = aiohttp.__version__
        check("aiohttp", True, version)
    except Exception as exc:
        check("aiohttp", False, str(exc))

    check(
        "crypto",
        True,
        f"{len(Profile.reference().values)} fingerprint fields",
    )

    pool = _proxy_pool(settings)
    proxy = pool[0] if pool else None
    if proxy is not None and proxy.needs_detection:
        proxy = await detect_proxy(proxy, timeout=min(args.timeout, 10.0))

    if proxy is not None:
        health = await probe_proxy(proxy, timeout=args.timeout)
        check(
            "proxy",
            health.ok,
            f"{proxy.anonymized()} -> {health.ip} ({(health.latency or 0.0) * 1000:.0f} ms)"
            if health.ok
            else f"{proxy.anonymized()}: {health.error}",
        )

    async with aiohttp.ClientSession(connector=connector_for(proxy)) as session:
        try:
            async with session.get(
                "https://smartcaptcha.yandexcloud.net/captcha.js",
                timeout=aiohttp.ClientTimeout(total=args.timeout),
                proxy=request_proxy(proxy),
            ) as response:
                check("network", response.status == 200, f"captcha.js HTTP {response.status}")
        except Exception as exc:
            check("network", False, f"{type(exc).__name__}: {exc}")

        if args.sitekey and args.host:
            sitekey, host, href = args.sitekey, args.host, f"https://{args.host}/"
        elif args.url:
            try:
                info = await discover(
                    args.url, session=session, proxy=proxy, timeout=args.timeout
                )
                sitekey, host, href = info.sitekey, info.host, info.href
                check("discovery", True, f"{info.sitekey} [{info.source}]")
            except Exception as exc:
                check("discovery", False, f"{type(exc).__name__}: {exc}")
                return _report_doctor(checks, args.json)
        else:
            check("solve", True, "skipped (pass --url or --sitekey/--host)")
            return _report_doctor(checks, args.json)

        try:
            solver = YandexCaptchaSolver(
                sitekey=sitekey,
                host=host,
                href=href,
                session=session,
                proxy=proxy,
                **{
                    k: v
                    for k, v in settings.solver_kwargs().items()
                    if k not in {"proxy", "proxy_file"}
                },
            )
            started = time.perf_counter()
            result = await solver.solve_detailed()
            elapsed = (time.perf_counter() - started) * 1000
            check(
                "solve",
                bool(result.token),
                f"{elapsed:.0f} ms, ip={result.ip}, attempts={result.attempts}",
            )
            check("token", result.issued_at is not None, (result.token[:32] + "…"))
        except ChallengeRequiredError as exc:
            check("solve", False, f"interactive challenge: {exc.kind} (change egress IP)")
        except YandexCaptchaError as exc:
            check("solve", False, f"{type(exc).__name__}: {exc}")

    return _report_doctor(checks, args.json)


def _report_doctor(checks: Sequence[tuple[str, bool, str]], as_json: bool) -> int:
    failed = [c for c in checks if not c[1]]
    if as_json:
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "checks": [
                        {"name": n, "ok": ok, "detail": d} for n, ok, d in checks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for name, ok, detail in checks:
            mark = "PASS" if ok else "FAIL"
            print(f"{mark}  {name:<10} {detail}")
        print()
        print("all checks passed" if not failed else f"{len(failed)} check(s) failed")
    return EXIT_OK if not failed else EXIT_FAILED


def _cmd_profile(args: argparse.Namespace) -> int:
    cmd = args.profile_command
    if cmd == "decode":
        data = args.rdata
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                data = fh.read().strip()
        if args.har:
            data = _rdata_from_har(args.har)
        profile = Profile.reference() if not data else Profile.from_rdata(data)
        print(json.dumps(profile.to_json(), ensure_ascii=False, indent=2))
        return EXIT_OK
    if cmd == "encode":
        profile = Profile.from_json(args.file)
        print(profile.to_rdata(args.timestamp))
        return EXIT_OK
    if cmd == "show":
        profile = Profile.reference()
        if args.json:
            print(json.dumps(profile.to_json(), ensure_ascii=False, indent=2))
        else:
            for key, value in profile.as_text().items():
                print(f"{key:>6} = {value}")
        return EXIT_OK
    return EXIT_FAILED


def _rdata_from_har(path: str) -> str:
    import urllib.parse

    with open(path, encoding="utf-8") as fh:
        har = json.load(fh)
    found = None
    for entry in har["log"]["entries"]:
        request = entry.get("request", {})
        if "smartcaptcha" not in request.get("url", ""):
            continue
        text = (request.get("postData") or {}).get("text") or ""
        if "rdata=" not in text:
            continue
        for pair in text.split("&"):
            if pair.startswith("rdata="):
                found = pair[len("rdata="):]
    if not found:
        raise SystemExit(f"no rdata found in {path}")
    return urllib.parse.unquote(found)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s"
        )

    try:
        if args.command == "solve":
            return asyncio.run(_cmd_solve(args))
        if args.command == "discover":
            return asyncio.run(_cmd_discover(args))
        if args.command == "verify":
            return asyncio.run(_cmd_verify(args))
        if args.command == "doctor":
            return asyncio.run(_cmd_doctor(args))
        if args.command == "proxy":
            return asyncio.run(_cmd_proxy(args))
        if args.command == "profile":
            return _cmd_profile(args)
        if args.command == "token":
            info = SolveResult.decode_token(args.value)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return EXIT_OK
    except ChallengeRequiredError as exc:
        if args.verbose:
            raise
        print(json.dumps({"ok": False, "error": "challenge", "kind": exc.kind}))
        return EXIT_CHALLENGE
    except YandexCaptchaError as exc:
        if args.verbose:
            raise
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}))
        return EXIT_FAILED
    except KeyboardInterrupt:
        return 130

    parser.error("unknown command")
    return EXIT_FAILED
