#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import attack
from attack import DEFAULT_SERVICES_PATH, Template, TemplateError, YANDEX_TOKENS
from yandex_captcha import YandexCaptcha, YandexCaptchaCache

COUNTRIES = ("RU", "UA")
METHODS = ("POST", "GET")
PLACEHOLDER_HINT = "{{RAW}}"


class AddError(RuntimeError):
    pass


def parse_headers(raw: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise AddError(f"--header {item!r}: нужен формат 'Имя: значение'")
        headers[name.strip()] = value.strip()
    return headers


def build_entry(args: argparse.Namespace, settings: YandexCaptcha | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": args.name.strip(),
        "country": args.country,
        "method": args.method,
        "url": args.url.strip(),
        "headers": parse_headers(args.header),
        "body": args.body,
        "content_type": args.content_type,
        "needs_prepare": False,
        "has_phone": True,
    }
    if args.chain:
        entry["chain"] = args.chain
    if settings is not None:
        entry["yandex_captcha"] = settings_to_raw(settings)
    if args.api_ok:
        entry["api_ok"] = args.api_ok
    if args.api_error:
        entry["api_error"] = args.api_error
    return entry


def settings_to_raw(settings: YandexCaptcha) -> dict[str, Any]:
    raw: dict[str, Any] = {"sitekey": settings.sitekey, "host": settings.host}
    if settings.href:
        raw["href"] = settings.href
    if settings.ttl:
        raw["ttl"] = settings.ttl
    if settings.cipher:
        raw["cipher"] = settings.cipher
    return raw


def entry_blob(entry: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(entry.get("url") or ""),
            str(entry.get("body") or ""),
            "\n".join(str(value) for value in (entry.get("headers") or {}).values()),
        ]
    )


def merge_entry(entries: list[dict], entry: dict, *, replace: bool) -> tuple[list[dict], bool]:
    for index, existing in enumerate(entries):
        if existing.get("name") == entry["name"]:
            if not replace:
                raise AddError(
                    f"сервис {entry['name']} уже есть в services.json "
                    f"(строка {index + 1}); замена — с --replace"
                )
            merged = list(entries)
            merged[index] = entry
            return merged, True
    return [*entries, entry], False


def write_services(path: pathlib.Path, entries: list[dict], *, backup: bool) -> pathlib.Path | None:
    payload = json.dumps(entries, ensure_ascii=False, indent=1) + "\n"
    backup_path: pathlib.Path | None = None
    if backup and path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = path.parent / "backups"
        if backup_dir.is_dir():
            backup_path = backup_dir / f"{path.name}.{stamp}.bak"
        else:
            backup_path = path.with_name(f"{path.name}.{stamp}.bak")
        try:
            backup_path.write_bytes(path.read_bytes())
        except OSError:
            backup_path = None
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        path.write_text(payload, encoding="utf-8")
    return backup_path


async def discover_settings(page: str, proxy: str | None, timeout: float) -> YandexCaptcha:
    from yandexcaptcha import discover

    try:
        info = await asyncio.wait_for(discover(page, proxy=proxy or None), timeout)
    except asyncio.TimeoutError as exc:
        raise AddError(f"страница {page} не ответила за {timeout:.0f}с") from exc
    except Exception as exc:
        raise AddError(
            f"sitekey не найден на {page}: {exc}\n"
            "Страница могла закрыться от этого IP — повтори с --proxy."
        ) from exc
    return YandexCaptcha(
        sitekey=info.sitekey,
        host=info.host,
        href=info.href or page,
    )


async def probe_token(settings: YandexCaptcha, proxy: str | None, timeout: float) -> str:
    cache = YandexCaptchaCache(timeout)
    try:
        return await cache.token("probe", settings, proxy)
    finally:
        await cache.aclose()


def print_entry(entry: dict[str, Any]) -> None:
    print(json.dumps(entry, ensure_ascii=False, indent=1))


def warn_unused_token(entry: dict[str, Any]) -> None:
    if not entry.get("yandex_captcha"):
        return
    blob = entry_blob(entry)
    if not any(f"{{{{{name}}}}}" in blob for name in YANDEX_TOKENS):
        raise AddError(
            "yandex_captcha задан, но в url/body/headers нет ни одного плейсхолдера "
            + ", ".join("{{" + name + "}}" for name in sorted(YANDEX_TOKENS))
            + ": токен будет решаться впустую"
        )


def require_captcha_config(entry: dict[str, Any]) -> None:
    blob = entry_blob(entry)
    if not entry.get("yandex_captcha") and any(
        f"{{{{{name}}}}}" in blob for name in YANDEX_TOKENS
    ):
        raise AddError(
            "в шаблоне есть плейсхолдер SmartCaptcha, но не задан --page/--sitekey: "
            "бот отклонит такой сервис"
        )


def describe(entry: dict[str, Any]) -> str:
    lines = [
        f"сервис:   {entry['name']} ({entry['country']}, {entry['method']})",
        f"эндпоинт: {entry['url']}",
        f"тело:     {entry.get('body', '')[:120] or '—'}",
    ]
    yandex = entry.get("yandex_captcha") or {}
    if yandex:
        lines.append(
            "капча:    Yandex SmartCaptcha, sitekey "
            f"{yandex.get('sitekey', '')[:16]}… на {yandex.get('host', '')}"
        )
    if entry.get("chain"):
        lines.append(f"цепочка:  {entry['chain']} (капча в цепочках не поддерживается)")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.services)

    settings: YandexCaptcha | None = None
    if args.page:
        found = await discover_settings(args.page, args.proxy, args.timeout)
        settings = YandexCaptcha(
            sitekey=found.sitekey,
            host=found.host,
            href=args.href.strip() or found.href,
            ttl=args.yandex_ttl,
            cipher=args.yandex_cipher,
        )
    elif args.sitekey or args.host:
        if not args.sitekey or not args.host:
            raise AddError("--sitekey и --host задаются вместе")
        settings = YandexCaptcha(
            sitekey=args.sitekey.strip(),
            host=args.host.strip(),
            href=args.href.strip(),
            ttl=args.yandex_ttl,
            cipher=args.yandex_cipher,
        )
    if settings is not None and args.chain:
        raise AddError(
            "капча не поддерживается в шаблонах с chain: "
            "для цепочек добавь запрос в attack.CHAINS"
        )

    entry = build_entry(args, settings)
    warn_unused_token(entry)
    require_captcha_config(entry)

    try:
        Template.from_raw(entry)
    except (TemplateError, ValueError) as exc:
        raise AddError(f"шаблон не прошёл проверку: {exc}") from exc

    if settings is not None and args.probe:
        token = await probe_token(settings, args.proxy, args.timeout)
        print(f"[probe] токен получен: {token[:24]}… ({len(token)} символов)")

    print(describe(entry))
    if not args.write:
        print()
        print_entry(entry)
        print()
        print("файл не тронут: добавь --write, чтобы применить")
        return 0

    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AddError(f"нет файла {path}") from exc
    except json.JSONDecodeError as exc:
        raise AddError(f"{path}: битый JSON: {exc}") from exc

    merged, replaced = merge_entry(entries, entry, replace=args.replace)
    try:
        attack.parse_templates(merged, strict=True)
    except TemplateError as exc:
        raise AddError(f"после добавления services.json невалиден: {exc}") from exc

    backup = write_services(path, merged, backup=not args.no_backup)
    print()
    print(f"{'заменён' if replaced else 'добавлен'}: {entry['name']} → {path} ({len(merged)} сервисов)")
    if backup is not None:
        print(f"бэкап: {backup}")
    print("применить без рестарта: кнопка «Сервисы» → «Перезагрузить» в админке или /reload в боте")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Добавить сервис в services.json (в том числе с Yandex SmartCaptcha)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "sitekey ищется сам по --page. Если страница закрыта от сервера, "
            "передай --proxy или --sitekey/--host из DevTools."
        ),
    )
    parser.add_argument("--name", required=True, help="имя сервиса, например Cafe_RU_SMS")
    parser.add_argument("--country", required=True, choices=COUNTRIES)
    parser.add_argument("--method", default="POST", choices=METHODS)
    parser.add_argument("--url", required=True, help="эндпоинт отправки кода")
    parser.add_argument("--body", default="", help=f"тело запроса, номер — {PLACEHOLDER_HINT}")
    parser.add_argument("--content-type", default="", help="application/json или x-www-form-urlencoded")
    parser.add_argument("--header", action="append", default=[], help="'Имя: значение', можно повторять")
    parser.add_argument("--api-ok", default="", help="подстрока ответа, подтверждающая отправку")
    parser.add_argument("--api-error", default="", help="подстрока ответа с ошибкой номера")
    parser.add_argument("--chain", default="", help="имя HTTP-цепочки из attack.CHAINS")
    parser.add_argument("--page", default="", help="страница с виджетом SmartCaptcha: sitekey найдётся сам")
    parser.add_argument("--sitekey", default="", help="sitekey ysc1_… вручную")
    parser.add_argument("--host", default="", help="host, на который выдан sitekey")
    parser.add_argument("--href", default="", help="URL страницы виджета (по умолчанию — --page)")
    parser.add_argument("--yandex-ttl", type=float, default=0.0, help="переиспользовать токен, сек (0 — новый на попытку)")
    parser.add_argument("--yandex-cipher", default="", choices=("", "a", "b"), help="сборка шифра из DevTools")
    parser.add_argument("--probe", action="store_true", help="решить одну капчу и проверить токен")
    parser.add_argument("--proxy", default="", help="прокси для поиска sitekey и probe")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--services", default=str(DEFAULT_SERVICES_PATH))
    parser.add_argument("--replace", action="store_true", help="перезаписать сервис с тем же именем")
    parser.add_argument("--write", action="store_true", help="записать сервис в services.json")
    parser.add_argument("--dry-run", action="store_true", help="только показать запись (по умолчанию)")
    parser.add_argument("--no-backup", action="store_true", help="не делать .bak перед записью")
    args = parser.parse_args()

    if args.write and args.dry_run:
        raise SystemExit("--write и --dry-run несовместимы")
    if args.yandex_cipher and not (args.page or args.sitekey):
        raise SystemExit("--yandex-cipher имеет смысл только с --page/--sitekey")

    try:
        return asyncio.run(run(args))
    except AddError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
