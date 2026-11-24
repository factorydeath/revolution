#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp
from attack import build_job, make_formatter, parse_templates
from proxies import load_proxies


async def probe(name: str, template, proxy, digits: str) -> None:
    job = build_job(template, make_formatter(digits, {}))
    headers = dict(job.headers)
    headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )
    if job.has_body and job.content_type:
        headers["Content-Type"] = job.content_type
    body = job.body.encode("utf-8") if job.has_body else None
    kwargs = proxy.request_kwargs() if proxy is not None else {}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        ) as session:
            async with session.request(
                job.method,
                job.url,
                headers=headers,
                data=body,
                allow_redirects=True,
                **kwargs,
            ) as response:
                chunk = await response.content.read(600)
                text = chunk.decode("utf-8", "replace")
                text = " ".join(text.split())
                print(f"{response.status:>4} {name:32} {text[:260]}", flush=True)
    except Exception as exc:
        print(f"  -- {name:32} {type(exc).__name__}: {exc}"[:300], flush=True)


async def main() -> int:
    services_path = ROOT / "configs" / "services.json" if (ROOT / "configs" / "services.json").exists() else ROOT / "services.json"
    services = json.loads(
        services_path.read_text(encoding="utf-8")
    )
    wanted = set(sys.argv[1:])
    templates = [
        tpl for tpl in parse_templates(services, strict=False) if tpl.name in wanted
    ]
    proxies_path = (
        ROOT / "configs" / "proxies.json"
        if (ROOT / "configs" / "proxies.json").exists()
        else ROOT / "configs" / "proxies.txt"
        if (ROOT / "configs" / "proxies.txt").exists()
        else ROOT / "proxies.json"
        if (ROOT / "proxies.json").exists()
        else ROOT / "proxies.txt"
    )
    proxies = load_proxies(str(proxies_path))
    digits = {"RU": "70000000000", "UA": "380000000000"}
    rng = random.Random()
    for template in templates:
        country_proxies = [p for p in proxies if getattr(p, "country", "") == template.country]
        proxy = rng.choice(country_proxies) if country_proxies else (rng.choice(proxies) if proxies else None)
        await probe(template.name, template, proxy, digits[template.country])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
