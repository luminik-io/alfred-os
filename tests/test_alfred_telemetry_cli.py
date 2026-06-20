"""Operator CLI tests for ``alfred telemetry``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(tmp_path: Path, *args: str, alfredrc: Path | None = None, agents_conf: Path | None = None):
    home = tmp_path / "home"
    alfred_home = tmp_path / "alfred"
    home.mkdir(exist_ok=True)
    alfred_home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "ALFRED_HOME": str(alfred_home),
        "PYTHONPATH": str(ROOT / "lib"),
    }
    for key in ("ALFRED_TELEMETRY_ENABLED", "ALFRED_TELEMETRY_URL", "ALFRED_TELEMETRY_TOKEN"):
        env.pop(key, None)
    cmd = [sys.executable, str(ROOT / "bin" / "alfred"), "telemetry", *args]
    if alfredrc is not None:
        cmd.extend(["--alfredrc", str(alfredrc)])
    if agents_conf is not None:
        cmd.extend(["--agents-conf", str(agents_conf)])
    return subprocess.run(cmd, check=False, capture_output=True, text=True, env=env, timeout=15)


def test_telemetry_status_reads_managed_files(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    agents_conf = tmp_path / "agents.conf"

    result = _run(tmp_path, "status", "--json", alfredrc=alfredrc, agents_conf=agents_conf)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["enabled"] is True
    assert payload["endpoint"] == ""
    assert payload["scheduler_row"] == "missing"


def test_telemetry_status_honors_commented_opt_out(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    alfredrc.write_text(
        "ALFRED_TELEMETRY_ENABLED=0 # opt out\n"
        "ALFRED_TELEMETRY_URL=https://telemetry.example.com/ingest\n",
        encoding="utf-8",
    )
    agents_conf = tmp_path / "agents.conf"

    result = _run(tmp_path, "status", "--json", alfredrc=alfredrc, agents_conf=agents_conf)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["enabled"] is False
    assert payload["endpoint"] == "https://telemetry.example.com/ingest"


def test_telemetry_on_writes_rc_block_before_init_block_and_schedules_row(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    alfredrc.write_text(
        "GH_ORG=acme\n\n"
        "# alfred-init, generated below this line. Safe to re-run.\n"
        "ALFRED_LUCIUS_REPOS=api\n",
        encoding="utf-8",
    )
    agents_conf = tmp_path / "agents.conf"
    agents_conf.write_text(
        "# label\tscript\tschedule\tneeds_java\tlog_stem\trole\n", encoding="utf-8"
    )

    result = _run(
        tmp_path,
        "on",
        "--url",
        "https://telemetry.example.com/ingest",
        "--token",
        "shared secret",
        alfredrc=alfredrc,
        agents_conf=agents_conf,
    )

    assert result.returncode == 0, result.stderr
    rc_text = alfredrc.read_text(encoding="utf-8")
    assert rc_text.index("# alfred telemetry") < rc_text.index("# alfred-init")
    assert "ALFRED_TELEMETRY_ENABLED=1" in rc_text
    assert "ALFRED_TELEMETRY_URL=https://telemetry.example.com/ingest" in rc_text
    assert "ALFRED_TELEMETRY_TOKEN='shared secret'" in rc_text
    conf_text = agents_conf.read_text(encoding="utf-8")
    assert conf_text.count("alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\t") == 1

    status = _run(tmp_path, "status", "--json", alfredrc=alfredrc, agents_conf=agents_conf)
    payload = json.loads(status.stdout)
    assert payload["enabled"] is True
    assert payload["endpoint"] == "https://telemetry.example.com/ingest"
    assert payload["token_configured"] is True
    assert payload["scheduler_row"] == "present"


def test_telemetry_off_disables_and_removes_scheduler_row(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    agents_conf = tmp_path / "agents.conf"
    on = _run(
        tmp_path,
        "on",
        "--url",
        "https://telemetry.example.com/ingest",
        "--token",
        "secret",
        alfredrc=alfredrc,
        agents_conf=agents_conf,
    )
    assert on.returncode == 0

    off = _run(tmp_path, "off", alfredrc=alfredrc, agents_conf=agents_conf)

    assert off.returncode == 0
    rc_text = alfredrc.read_text(encoding="utf-8")
    assert "ALFRED_TELEMETRY_ENABLED=0" in rc_text
    assert "ALFRED_TELEMETRY_URL=https://telemetry.example.com/ingest" in rc_text
    assert "ALFRED_TELEMETRY_TOKEN" not in rc_text
    assert "alfred.proof-telemetry" not in agents_conf.read_text(encoding="utf-8")


def test_telemetry_off_removes_later_init_block_telemetry_values(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    alfredrc.write_text(
        "GH_ORG=acme\n\n"
        "# alfred-init, generated below this line. Safe to re-run.\n"
        "ALFRED_TELEMETRY_ENABLED=1\n"
        "ALFRED_TELEMETRY_URL=https://old.example/ingest\n",
        encoding="utf-8",
    )
    agents_conf = tmp_path / "agents.conf"
    agents_conf.write_text(
        "alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\tno\t"
        "alfred.proof-telemetry\tAnonymous usage totals\n",
        encoding="utf-8",
    )

    off = _run(tmp_path, "off", alfredrc=alfredrc, agents_conf=agents_conf)

    assert off.returncode == 0
    rc_text = alfredrc.read_text(encoding="utf-8")
    assert rc_text.count("ALFRED_TELEMETRY_ENABLED=") == 1
    assert "ALFRED_TELEMETRY_ENABLED=0" in rc_text
    assert "https://old.example/ingest" in rc_text
    assert "# alfred-init" in rc_text
    status = _run(tmp_path, "status", "--json", alfredrc=alfredrc, agents_conf=agents_conf)
    assert json.loads(status.stdout)["enabled"] is False


def test_telemetry_on_prefers_deploy_source_over_runtime_copy(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    alfred_home = tmp_path / "alfred"
    runtime_launchd = alfred_home / "launchd"
    runtime_launchd.mkdir(parents=True)
    runtime_conf = runtime_launchd / "agents.conf"
    runtime_conf.write_text("# deployed runtime copy\n", encoding="utf-8")

    source_root = tmp_path / "source"
    source_launchd = source_root / "launchd"
    source_launchd.mkdir(parents=True)
    source_conf = source_launchd / "agents.conf"
    source_conf.write_text("# deploy source\n", encoding="utf-8")
    (source_root / "deploy.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (runtime_launchd / "source-repo.txt").write_text(str(source_root), encoding="utf-8")

    result = _run(
        tmp_path,
        "on",
        "--url",
        "https://telemetry.example.com/ingest",
        alfredrc=alfredrc,
    )

    assert result.returncode == 0, result.stderr
    assert "alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\t" in source_conf.read_text(
        encoding="utf-8"
    )
    assert "alfred.proof-telemetry" not in runtime_conf.read_text(encoding="utf-8")
    assert str(source_conf) in result.stdout
    assert f"bash {source_root / 'deploy.sh'}" in result.stdout


def test_telemetry_on_creates_source_agents_conf_when_marker_exists(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    alfred_home = tmp_path / "alfred"
    runtime_launchd = alfred_home / "launchd"
    runtime_launchd.mkdir(parents=True)

    source_root = tmp_path / "source"
    (source_root / "launchd").mkdir(parents=True)
    (source_root / "deploy.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    source_conf = source_root / "launchd" / "agents.conf"
    (runtime_launchd / "source-repo.txt").write_text(str(source_root), encoding="utf-8")

    result = _run(
        tmp_path,
        "on",
        "--url",
        "https://telemetry.example.com/ingest",
        alfredrc=alfredrc,
    )

    assert result.returncode == 0, result.stderr
    assert source_conf.exists()
    assert "alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\t" in source_conf.read_text(
        encoding="utf-8"
    )


def test_telemetry_on_rejects_nonlocal_http_endpoint(tmp_path):
    result = _run(tmp_path, "on", "--url", "http://telemetry.example.com/ingest")

    assert result.returncode == 2
    assert "must be HTTPS" in result.stderr


def test_telemetry_on_allows_local_http_endpoint(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    agents_conf = tmp_path / "agents.conf"

    result = _run(
        tmp_path,
        "on",
        "--url",
        "http://127.0.0.1:8787/ingest",
        alfredrc=alfredrc,
        agents_conf=agents_conf,
    )

    assert result.returncode == 0
    assert "ALFRED_TELEMETRY_URL=http://127.0.0.1:8787/ingest" in alfredrc.read_text(
        encoding="utf-8"
    )


def test_telemetry_on_rejects_tsv_breaking_schedule(tmp_path):
    result = _run(
        tmp_path,
        "on",
        "--url",
        "https://collector.example/ingest",
        "--schedule",
        "cron:9:10\tmalicious",
    )

    assert result.returncode == 2
    assert "tabs or newlines" in result.stderr


def test_telemetry_on_rejects_invalid_schedule_shape(tmp_path):
    result = _run(
        tmp_path,
        "on",
        "--url",
        "https://collector.example/ingest",
        "--schedule",
        "cron:24:00",
    )

    assert result.returncode == 2
    assert "hour must be 0-23" in result.stderr
