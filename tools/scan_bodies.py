#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import re
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp
from attack import build_job, csrf_token, fill_tokens, make_formatter, parse_templates
from proxies import load_proxies

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

SUSPICIOUS = re.compile(
    r'"success"\s*:\s*false'
    r"|'success'\s*:\s*false"
    r'|"status"\s*:\s*(?:false|"error")'
    r'|"error"\s*:'
    r'|"errors"\s*:\s*\['
    r'|"ok"\s*:\s*false'
    r"|captcha"
    r"|recaptcha"
    r"|access\s*(?:is\s*)?(?:denied|blocked)"
    r"|forbidden"
    r"|blocked"
    r"|too\s+many\s+requests"
    r"|rate\s*limit"
    r"|неверн"
    r"|некоррект"
    r"|ошибка"
    r"|не\s+удалось"
    r"|заблокир",
    re.IGNORECASE,
)


async def probe_one(template, formatters, routes, timeout):
    job = build_job(template, formatters[template.country])
    headers = dict(job.headers)
    headers["User-Agent"] = UA
    if job.has_body and job.content_type:
        headers["Content-Type"] = job.content_type
    body = job.body.encode("utf-8") if job.has_body else None
    proxy = routes.pick(template.country)
    kwargs = proxy.request_kwargs() if proxy is not None else {}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            dynamic: dict[str, str] = {}
            if job.csrf is not None:
                dynamic["CSRF"] = await csrf_token(session, job.csrf, kwargs)
            if job.needs_uuid:
                dynamic["UUID"] = str(uuid.uuid4())
            url = job.url
            if dynamic:
                url = fill_tokens(job.url, dynamic)
                body = (
                    fill_tokens(job.body, dynamic).encode("utf-8")
                    if job.has_body
                    else None
                )
                headers = {
                    key: fill_tokens(value, dynamic) for key, value in headers.items()
                }
            async with session.request(
                job.method,
                url,
                headers=headers,
                data=body,
                allow_redirects=True,
                **kwargs,
            ) as response:
                chunk = await response.content.read(600)
                text = " ".join(chunk.decode("utf-8", "replace").split())
                return {
                    "name": template.name,
                    "status": response.status,
                    "api_ok": bool(template.api_ok),
                    "api_error": bool(template.api_error),
                    "body": text[:300],
                    "suspicious": bool(
                        200 <= response.status < 300
                        and not template.api_ok
                        and SUSPICIOUS.search(text)
                    ),
                }
    except Exception as exc:
        return {
            "name": template.name,
            "status": None,
            "api_ok": bool(template.api_ok),
            "api_error": bool(template.api_error),
            "body": f"{type(exc).__name__}: {exc}"[:200],
            "suspicious": False,
        }


async def main() -> int:
    parser = argparse.ArgumentParser()
    _services_def = ROOT / "configs" / "services.json" if (ROOT / "configs" / "services.json").exists() else ROOT / "services.json"
    _proxies_def = (
        ROOT / "configs" / "proxies.json"
        if (ROOT / "configs" / "proxies.json").exists()
        else ROOT / "configs" / "proxies.txt"
        if (ROOT / "configs" / "proxies.txt").exists()
        else ROOT / "proxies.json"
        if (ROOT / "proxies.json").exists()
        else ROOT / "proxies.txt"
    )
    parser.add_argument("--services", default=str(_services_def))
    parser.add_argument("--proxies", default=str(_proxies_def))
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--report", default="/tmp/body_scan.json")
    args = parser.parse_args()

    raw = json.loads(pathlib.Path(args.services).read_text(encoding="utf-8"))
    templates = parse_templates(raw, strict=False)
    digits = {"RU": "70000000000", "UA": "380000000000"}
    formatters = {code: make_formatter(value, {}) for code, value in digits.items()}

    entries = load_proxies(args.proxies)

    def pick_route(country: str = ""):
        if not entries:
            return None
        if country:
            c = country.upper().strip()
            subset = [e for e in entries if getattr(e, "country", "") == c]
            if subset:
                return random.choice(subset)
            return None
        return random.choice(entries)

    routes = type("R", (), {"pick": staticmethod(pick_route)})()

    semaphore = asyncio.Semaphore(args.concurrency)
    done = 0
    total = len(templates)
    results = []

    async def worker(template):
        nonlocal done
        async with semaphore:
            result = await probe_one(template, formatters, routes, args.timeout)
        done += 1
        if done % 25 == 0 or done == total:
            print(f"  {done}/{total}", flush=True)
        results.append(result)

    await asyncio.gather(*(worker(t) for t in templates))

    pathlib.Path(args.report).write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    suspicious = [r for r in results if r["suspicious"]]
    print(f"\nподозрительных (2xx + текст ошибки, без api_ok): {len(suspicious)}")
    for row in sorted(suspicious, key=lambda r: r["name"]):
        print(f"  {row['status']} {row['name']:32} {row['body'][:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
