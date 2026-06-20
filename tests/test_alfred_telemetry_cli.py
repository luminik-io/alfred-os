"""Operator CLI tests for ``alfred telemetry``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TELEMETRY_URL = "https://alfred-proof-telemetry.luminik.workers.dev/ingest"


def _capture_server(*, status: int = 200):
    received: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(size).decode("utf-8")
            received.append(
                {
                    "path": self.path,
                    "token": self.headers.get("X-Ingest-Token"),
                    "body": json.loads(raw),
                }
            )
            self.send_response(status)
            self.end_headers()
            body = b'{"ok":true}' if status < 400 else b'{"ok":false}'
            self.wfile.write(body)

        def log_message(self, *args):  # pragma: no cover - keep test output quiet
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


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
    for key in (
        "ALFRED_TELEMETRY_ENABLED",
        "ALFRED_TELEMETRY_URL",
        "ALFRED_TELEMETRY_TOKEN",
        "ALFRED_TELEMETRY_TRUSTED_TOKEN",
        "ALFRED_DEFAULT_TELEMETRY_URL",
    ):
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
    assert payload["endpoint"] == DEFAULT_TELEMETRY_URL
    assert payload["scheduler_row"] == "missing"
    assert payload["trusted_token_configured"] is False


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
    assert alfredrc.stat().st_mode & 0o777 == 0o600
    conf_text = agents_conf.read_text(encoding="utf-8")
    assert conf_text.count("alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\t") == 1

    status = _run(tmp_path, "status", "--json", alfredrc=alfredrc, agents_conf=agents_conf)
    payload = json.loads(status.stdout)
    assert payload["enabled"] is True
    assert payload["endpoint"] == "https://telemetry.example.com/ingest"
    assert payload["token_configured"] is True
    assert payload["trusted_token_configured"] is False
    assert payload["scheduler_row"] == "present"


def test_telemetry_status_reports_trusted_token_without_printing_it(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    alfredrc.write_text(
        "ALFRED_TELEMETRY_TRUSTED_TOKEN=trusted-secret\n",
        encoding="utf-8",
    )
    agents_conf = tmp_path / "agents.conf"

    result = _run(tmp_path, "status", "--json", alfredrc=alfredrc, agents_conf=agents_conf)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["trusted_token_configured"] is True
    assert "trusted-secret" not in result.stdout


def test_telemetry_on_uses_hosted_default_without_url(tmp_path):
    alfredrc = tmp_path / ".alfredrc"
    agents_conf = tmp_path / "agents.conf"

    result = _run(tmp_path, "on", alfredrc=alfredrc, agents_conf=agents_conf)

    assert result.returncode == 0, result.stderr
    rc_text = alfredrc.read_text(encoding="utf-8")
    assert "ALFRED_TELEMETRY_ENABLED=1" in rc_text
    assert f"ALFRED_TELEMETRY_URL={DEFAULT_TELEMETRY_URL}" in rc_text
    assert "alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\t" in agents_conf.read_text(
        encoding="utf-8"
    )


def test_telemetry_on_uses_default_endpoint_from_alfredrc(tmp_path):
    endpoint = "https://selfhosted.example.com/ingest"
    alfredrc = tmp_path / ".alfredrc"
    alfredrc.write_text(f"ALFRED_DEFAULT_TELEMETRY_URL={endpoint}\n", encoding="utf-8")
    agents_conf = tmp_path / "agents.conf"

    result = _run(tmp_path, "on", alfredrc=alfredrc, agents_conf=agents_conf)

    assert result.returncode == 0, result.stderr
    rc_text = alfredrc.read_text(encoding="utf-8")
    assert f"ALFRED_DEFAULT_TELEMETRY_URL={endpoint}" in rc_text
    assert f"ALFRED_TELEMETRY_URL={endpoint}" in rc_text


def test_telemetry_on_clear_token_resets_install_id_pair(tmp_path):
    server, received = _capture_server()
    url = f"http://127.0.0.1:{server.server_port}/ingest"
    state = tmp_path / "alfred" / "state"
    state.mkdir(parents=True)
    token = state / "telemetry-token"
    token_endpoint = state / "telemetry-token-endpoint"
    install_id = state / "telemetry-install-id"
    token.write_text("old-token\n", encoding="utf-8")
    token_endpoint.write_text(f"{url}\n", encoding="utf-8")
    install_id.write_text("old-install-id\n", encoding="utf-8")
    alfredrc = tmp_path / ".alfredrc"
    agents_conf = tmp_path / "agents.conf"

    try:
        result = _run(
            tmp_path,
            "on",
            "--clear-token",
            "--url",
            url,
            alfredrc=alfredrc,
            agents_conf=agents_conf,
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    assert not token.exists()
    assert not token_endpoint.exists()
    assert not install_id.exists()
    assert "ALFRED_TELEMETRY_TOKEN" not in alfredrc.read_text(encoding="utf-8")
    assert received[0]["path"] == "/ingest"
    assert received[0]["token"] == "old-token"
    assert received[0]["body"]["install_id"] == "old-install-id"
    assert received[0]["body"]["tombstone"] is True


def test_telemetry_on_clear_token_keeps_explicit_replacement_token(tmp_path):
    server, received = _capture_server()
    url = f"http://127.0.0.1:{server.server_port}/ingest"
    state = tmp_path / "alfred" / "state"
    state.mkdir(parents=True)
    token = state / "telemetry-token"
    token_endpoint = state / "telemetry-token-endpoint"
    install_id = state / "telemetry-install-id"
    token.write_text("old-token\n", encoding="utf-8")
    token_endpoint.write_text(f"{url}\n", encoding="utf-8")
    install_id.write_text("old-install-id\n", encoding="utf-8")
    alfredrc = tmp_path / ".alfredrc"
    agents_conf = tmp_path / "agents.conf"

    try:
        result = _run(
            tmp_path,
            "on",
            "--clear-token",
            "--token",
            "new-token",
            "--url",
            url,
            alfredrc=alfredrc,
            agents_conf=agents_conf,
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    assert received[0]["token"] == "old-token"
    assert not token.exists()
    assert not token_endpoint.exists()
    assert not install_id.exists()
    assert "ALFRED_TELEMETRY_TOKEN=new-token" in alfredrc.read_text(encoding="utf-8")


def test_telemetry_on_clear_token_keeps_install_id_when_clear_fails(tmp_path):
    server, _received = _capture_server(status=500)
    url = f"http://127.0.0.1:{server.server_port}/ingest"
    state = tmp_path / "alfred" / "state"
    state.mkdir(parents=True)
    token = state / "telemetry-token"
    token_endpoint = state / "telemetry-token-endpoint"
    install_id = state / "telemetry-install-id"
    token.write_text("old-token\n", encoding="utf-8")
    token_endpoint.write_text(f"{url}\n", encoding="utf-8")
    install_id.write_text("old-install-id\n", encoding="utf-8")
    alfredrc = tmp_path / ".alfredrc"
    alfredrc.write_text(f"ALFRED_TELEMETRY_URL={url}\n", encoding="utf-8")
    agents_conf = tmp_path / "agents.conf"

    try:
        result = _run(
            tmp_path,
            "on",
            "--clear-token",
            alfredrc=alfredrc,
            agents_conf=agents_conf,
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    assert not token.exists()
    assert not token_endpoint.exists()
    assert install_id.read_text(encoding="utf-8").strip() == "old-install-id"
    assert "kept install id" in result.stderr


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


def test_telemetry_off_clears_previous_usage_totals(tmp_path):
    server, received = _capture_server()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/ingest"
        alfredrc = tmp_path / ".alfredrc"
        alfredrc.write_text(
            "ALFRED_TELEMETRY_ENABLED=1\n"
            f"ALFRED_TELEMETRY_URL={endpoint}\n"
            "ALFRED_TELEMETRY_TOKEN=shared-secret\n",
            encoding="utf-8",
        )
        agents_conf = tmp_path / "agents.conf"
        agents_conf.write_text(
            "alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\tno\t"
            "alfred.proof-telemetry\tAnonymous usage totals\n",
            encoding="utf-8",
        )
        install_id = tmp_path / "alfred" / "state" / "telemetry-install-id"
        install_id.parent.mkdir(parents=True)
        install_id.write_text("install-cli-test\n", encoding="utf-8")
        token = tmp_path / "alfred" / "state" / "telemetry-token"
        token_endpoint = tmp_path / "alfred" / "state" / "telemetry-token-endpoint"
        token.write_text("persisted-install-token\n", encoding="utf-8")
        token_endpoint.write_text(f"{endpoint}\n", encoding="utf-8")

        result = _run(tmp_path, "off", alfredrc=alfredrc, agents_conf=agents_conf)

        assert result.returncode == 0, result.stderr
        assert "cleared previous usage totals" in result.stdout
        assert not token.exists()
        assert not token_endpoint.exists()
        assert received == [
            {
                "path": "/ingest",
                "token": "shared-secret",
                "body": {
                    "install_id": "install-cli-test",
                    "period": "lifetime",
                    "tombstone": True,
                },
            }
        ]
    finally:
        server.shutdown()


def test_telemetry_off_preserves_token_when_clear_fails(tmp_path):
    server, received = _capture_server(status=500)
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/ingest"
        alfredrc = tmp_path / ".alfredrc"
        alfredrc.write_text(
            "ALFRED_TELEMETRY_ENABLED=1\n"
            f"ALFRED_TELEMETRY_URL={endpoint}\n"
            "ALFRED_TELEMETRY_TOKEN=shared-secret\n",
            encoding="utf-8",
        )
        agents_conf = tmp_path / "agents.conf"
        agents_conf.write_text(
            "alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\tno\t"
            "alfred.proof-telemetry\tAnonymous usage totals\n",
            encoding="utf-8",
        )
        install_id = tmp_path / "alfred" / "state" / "telemetry-install-id"
        install_id.parent.mkdir(parents=True)
        install_id.write_text("install-cli-test\n", encoding="utf-8")
        token = tmp_path / "alfred" / "state" / "telemetry-token"
        token_endpoint = tmp_path / "alfred" / "state" / "telemetry-token-endpoint"
        token.write_text("persisted-install-token\n", encoding="utf-8")
        token_endpoint.write_text(f"{endpoint}\n", encoding="utf-8")

        result = _run(tmp_path, "off", alfredrc=alfredrc, agents_conf=agents_conf)

        assert result.returncode == 0
        assert "could not clear previous usage totals" in result.stderr
        assert received and received[0]["token"] == "shared-secret"
        assert token.exists()
        assert token_endpoint.exists()
        rc_text = alfredrc.read_text(encoding="utf-8")
        assert "ALFRED_TELEMETRY_ENABLED=0" in rc_text
        assert f"ALFRED_TELEMETRY_URL={endpoint}" in rc_text
        assert "ALFRED_TELEMETRY_TOKEN=shared-secret" in rc_text
    finally:
        server.shutdown()


def test_telemetry_off_deletes_install_id_after_clear_succeeds(tmp_path):
    server, _received = _capture_server()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/ingest"
        alfredrc = tmp_path / ".alfredrc"
        alfredrc.write_text(
            "ALFRED_TELEMETRY_ENABLED=1\n"
            f"ALFRED_TELEMETRY_URL={endpoint}\n"
            "ALFRED_TELEMETRY_TOKEN=shared-secret\n",
            encoding="utf-8",
        )
        agents_conf = tmp_path / "agents.conf"
        agents_conf.write_text(
            "alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\tno\t"
            "alfred.proof-telemetry\tAnonymous usage totals\n",
            encoding="utf-8",
        )
        install_id = tmp_path / "alfred" / "state" / "telemetry-install-id"
        install_id.parent.mkdir(parents=True)
        install_id.write_text("install-cli-test\n", encoding="utf-8")

        result = _run(
            tmp_path,
            "off",
            "--delete-install-id",
            alfredrc=alfredrc,
            agents_conf=agents_conf,
        )

        assert result.returncode == 0, result.stderr
        assert "removed install id" in result.stdout
        assert not install_id.exists()
    finally:
        server.shutdown()


def test_telemetry_off_preserves_install_id_when_clear_fails(tmp_path):
    server, _received = _capture_server(status=500)
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/ingest"
        alfredrc = tmp_path / ".alfredrc"
        alfredrc.write_text(
            "ALFRED_TELEMETRY_ENABLED=1\n"
            f"ALFRED_TELEMETRY_URL={endpoint}\n"
            "ALFRED_TELEMETRY_TOKEN=shared-secret\n",
            encoding="utf-8",
        )
        agents_conf = tmp_path / "agents.conf"
        agents_conf.write_text(
            "alfred.proof-telemetry\tproof-telemetry.py\tcron:9:10\tno\t"
            "alfred.proof-telemetry\tAnonymous usage totals\n",
            encoding="utf-8",
        )
        install_id = tmp_path / "alfred" / "state" / "telemetry-install-id"
        install_id.parent.mkdir(parents=True)
        install_id.write_text("install-cli-test\n", encoding="utf-8")

        result = _run(
            tmp_path,
            "off",
            "--delete-install-id",
            alfredrc=alfredrc,
            agents_conf=agents_conf,
        )

        assert result.returncode == 0
        assert "kept install id" in result.stderr
        assert install_id.exists()
        rc_text = alfredrc.read_text(encoding="utf-8")
        assert f"ALFRED_TELEMETRY_URL={endpoint}" in rc_text
        assert "ALFRED_TELEMETRY_TOKEN=shared-secret" in rc_text
    finally:
        server.shutdown()


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
