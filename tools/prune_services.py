#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys
import time
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from attack import parse_templates

WORKING = frozenset({"ok", "chain", "api_error", "rate_limited"})
UNKNOWN = frozenset({"alive", "captcha"})
DEAD = frozenset({"dead_host", "error_server", "prepare_failed", "broken_template"})


def load_report(path: pathlib.Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    verdicts: dict[str, str] = {}
    for probe in data.get("results", []):
        name = probe.get("name")
        if not name:
            continue
        verdict = probe.get("verdict") or "unknown"
        if name not in verdicts or rank(verdict) < rank(verdicts[name]):
            verdicts[name] = verdict
    return verdicts


def rank(verdict: str) -> int:
    if verdict in WORKING:
        return 0
    if verdict in UNKNOWN:
        return 1
    return 2


def report_identity(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Удаление подтверждённо нерабочих сервисов из services.json.")
    _services_def = ROOT / "configs" / "services.json" if (ROOT / "configs" / "services.json").exists() else ROOT / "services.json"
    parser.add_argument("--services", default=str(_services_def))
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        help="отчёт живой проверки (может быть несколько)",
    )
    parser.add_argument(
        "--drop-run",
        action="append",
        default=[],
        help="отчёт прогона, по которому можно удалять (нужно два независимых)",
    )
    parser.add_argument("--min-bad-runs", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--keep", action="append", default=[], help="имена, которые нельзя удалять")
    parser.add_argument(
        "--remove",
        action="append",
        default=[],
        help="имя, снятое вручную после разбора тела ответа (без опоры на отчёты)",
    )
    args = parser.parse_args()

    services_path = pathlib.Path(args.services)
    raw = json.loads(services_path.read_text(encoding="utf-8"))

    verdicts: dict[str, str] = {}
    for path in args.report:
        for name, verdict in load_report(pathlib.Path(path)).items():
            if name not in verdicts or rank(verdict) < rank(verdicts[name]):
                verdicts[name] = verdict

    bad_runs: dict[str, int] = defaultdict(int)
    bad_verdict: dict[str, str] = {}
    seen_runs: set[str] = set()
    for raw_path in args.drop_run:
        path = pathlib.Path(raw_path)
        identity = report_identity(path)
        if identity in seen_runs:
            print(f"повтор отчёта пропущен: {path}", file=sys.stderr)
            continue
        seen_runs.add(identity)
        for name, verdict in load_report(path).items():
            if verdict in DEAD:
                bad_runs[name] += 1
                bad_verdict.setdefault(name, verdict)

    keep = set(args.keep)
    manual = [name for name in args.remove if name not in keep]
    drop: list[tuple[str, str, int]] = [
        (name, "manual", 0) for name in manual
    ]
    for item in raw:
        name = item.get("name")
        if not name or name in keep or name in manual:
            continue
        verdict = verdicts.get(name)
        if verdict in WORKING or verdict in UNKNOWN:
            continue
        if bad_runs.get(name, 0) >= args.min_bad_runs:
            drop.append((name, bad_verdict.get(name, "dead"), bad_runs[name]))

    drop_names = {name for name, _, _ in drop}
    kept = [item for item in raw if item.get("name") not in drop_names]

    unknown = sorted(
        name
        for name, verdict in verdicts.items()
        if verdict in UNKNOWN and name not in keep and name not in manual
    )

    print(f"шаблонов: {len(raw)}")
    print(f"к удалению: {len(drop)} · останется: {len(kept)}")
    if drop:
        print("\nудаляем:")
        for name, verdict, runs in sorted(drop, key=lambda row: (row[1], row[0])):
            tail = f"провалов: {runs}" if runs else "проверено вручную"
            print(f"  {verdict:16} {name:34} {tail}")
    if unknown:
        print(f"\nне проверено до конца (оставлены): {len(unknown)}")
        for name in unknown:
            print(f"  {verdicts[name]:16} {name}")

    if not args.apply:
        print("\nрежим просмотра: файл не изменён (--apply для записи)")
        return 0

    if not drop:
        print("\nнечего удалять")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = services_path.with_name(f"{services_path.name}.bak-{stamp}")
    shutil.copy2(services_path, backup)
    services_path.write_text(
        json.dumps(kept, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    parsed = parse_templates(
        json.loads(services_path.read_text(encoding="utf-8")), strict=True
    )
    print(f"\nзаписано: {len(parsed)} шаблонов · бэкап: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
