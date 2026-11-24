from __future__ import annotations

import json
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_same_report_is_not_counted_as_two_runs(tmp_path):
    services = tmp_path / "services.json"
    report = tmp_path / "report.json"
    services.write_text(json.dumps([{"name": "Broken_RU_SMS"}]), encoding="utf-8")
    report.write_text(
        json.dumps({"results": [{"name": "Broken_RU_SMS", "verdict": "dead_host"}]}),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "prune_services.py"),
            "--services",
            str(services),
            "--drop-run",
            str(report),
            "--drop-run",
            str(report),
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
        timeout=20,
    )

    assert "повтор отчёта пропущен" in completed.stderr
    assert "к удалению: 0" in completed.stdout
