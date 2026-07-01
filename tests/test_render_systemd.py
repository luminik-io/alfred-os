"""Smoke tests for systemd/render.sh, the Linux scheduler renderer.

These mirror tests/test_render_role.py (the launchd renderer tests) for the
systemd --user lane. render.sh reads ``agents.conf`` from a caller-controlled
path (``../launchd/agents.conf`` relative to itself) and writes .service +
.timer pairs into a caller-controlled output dir, so the tests stand up a
tiny conf with one record and inspect the resulting unit files.

systemd unit files are INI text rather than XML/plist, so the assertions are
plain string checks against the rendered ``[Unit]`` / ``[Service]`` /
``[Timer]`` sections.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_SH = REPO_ROOT / "systemd" / "render.sh"
SERVICE_TEMPLATE = REPO_ROOT / "systemd" / "_template.service"
TIMER_TEMPLATE = REPO_ROOT / "systemd" / "_template.timer"


def _render_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("ALFREDRC", "ALFRED_HOME", "WORKSPACE_ROOT"):
        env.pop(key, None)
    env["HOME"] = str(tmp_path / "fakehome")
    env.update(extra or {})
    return env


def _render(tmp_path: Path, conf_text: str, env: dict[str, str] | None = None) -> Path:
    """Run systemd/render.sh against a temp tree. Returns the output dir.

    render.sh resolves agents.conf as ``$SCRIPT_DIR/../launchd/agents.conf``,
    so the temp tree mirrors the repo layout: a systemd/ dir next to a
    launchd/ dir.
    """
    systemd_dir = tmp_path / "systemd"
    launchd_dir = tmp_path / "launchd"
    # exist_ok: a single test may call _render more than once (e.g. to render
    # a needs_java and a no-java conf in the same tmp_path).
    systemd_dir.mkdir(exist_ok=True)
    launchd_dir.mkdir(exist_ok=True)
    shutil.copy(RENDER_SH, systemd_dir / "render.sh")
    shutil.copy(SERVICE_TEMPLATE, systemd_dir / "_template.service")
    shutil.copy(TIMER_TEMPLATE, systemd_dir / "_template.timer")
    (launchd_dir / "agents.conf").write_text(conf_text)
    out_dir = tmp_path / "out"
    # A throwaway HOME keeps the %h substitution deterministic and stops
    # render.sh from picking up the developer's real runtime config.
    full_env = _render_env(tmp_path, env)
    res = subprocess.run(
        ["bash", str(systemd_dir / "render.sh"), str(out_dir)],
        capture_output=True,
        text=True,
        env=full_env,
    )
    if res.returncode != 0:
        pytest.fail(f"render.sh failed: {res.stderr}")
    return out_dir


def test_render_emits_service_and_timer_pair(tmp_path):
    conf = "my.fleet.lucius\tlucius.py\tinterval:600\tno\t\tSingle-repo feature engineer\n"
    out_dir = _render(tmp_path, conf)
    assert (out_dir / "my.fleet.lucius.service").exists()
    assert (out_dir / "my.fleet.lucius.timer").exists()


def test_render_interval_maps_to_onunitactivesec(tmp_path):
    conf = "my.fleet.poller\tpoller.py\tinterval:1200\tno\n"
    out_dir = _render(tmp_path, conf)
    timer = (out_dir / "my.fleet.poller.timer").read_text()
    assert "OnUnitActiveSec=1200s" in timer
    assert "Unit=my.fleet.poller.service" in timer
    assert "WantedBy=timers.target" in timer


def test_render_daily_cron_maps_to_oncalendar(tmp_path):
    conf = "my.fleet.nightly\tnightly.py\tcron:2:00\tno\n"
    out_dir = _render(tmp_path, conf)
    timer = (out_dir / "my.fleet.nightly.timer").read_text()
    assert "OnCalendar=*-*-* 02:00:00" in timer


def test_render_weekly_cron_maps_to_oncalendar_with_weekday(tmp_path):
    # cron:<weekday>:<HH>:<MM>, weekday 1 == Monday.
    conf = "my.fleet.weekly\tweekly.py\tcron:1:8:15\tno\n"
    out_dir = _render(tmp_path, conf)
    timer = (out_dir / "my.fleet.weekly.timer").read_text()
    assert "OnCalendar=Mon *-*-* 08:15:00" in timer


def test_render_emits_alfred_role_env_when_role_column_set(tmp_path):
    conf = "my.fleet.lucius\tlucius.py\tinterval:600\tno\t\tSingle-repo feature engineer\n"
    out_dir = _render(tmp_path, conf)
    service = (out_dir / "my.fleet.lucius.service").read_text()
    # systemd Environment= line; the value is quoted because it has spaces.
    assert 'Environment=ALFRED_LUCIUS_ROLE="Single-repo feature engineer"' in service


def test_render_omits_role_block_when_column_empty(tmp_path):
    conf = "my.fleet.poller\tpoller.py\tinterval:1200\tno\n"
    out_dir = _render(tmp_path, conf)
    service = (out_dir / "my.fleet.poller.service").read_text()
    assert "_ROLE" not in service


def test_render_translates_dash_in_compound_codename(tmp_path):
    conf = "my.fleet.brand-mention-scanner\tscan.py\tinterval:7200\tno\t\tBrand mention monitor\n"
    out_dir = _render(tmp_path, conf)
    service = (out_dir / "my.fleet.brand-mention-scanner.service").read_text()
    assert "ALFRED_BRAND_MENTION_SCANNER_ROLE" in service
    assert "Environment=AGENT_CODENAME=brand-mention-scanner" in service


def test_render_invokes_agent_launch_and_sets_codename_env(tmp_path):
    conf = "my.fleet.marshall\tlucius.py\tinterval:600\tno\t\tFeature dev\n"
    out_dir = _render(tmp_path, conf)
    service = (out_dir / "my.fleet.marshall.service").read_text()
    assert "/agent-launch lucius.py" in service
    assert "Environment=AGENT_CODENAME=marshall" in service
    assert "Environment=LAUNCHD_LABEL=my.fleet.marshall" in service


def test_render_appends_enabled_custom_agents_from_manifest(tmp_path):
    runtime = tmp_path / "runtime"
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    shutil.copy(REPO_ROOT / "lib" / "custom_agents.py", lib_dir / "custom_agents.py")
    store = runtime / "state" / "custom-agents"
    store.mkdir(parents=True)
    (store / "custom-agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers for the operator.",
                        "engine": "hybrid",
                        "schedule": "interval:1800",
                        "repos": ["acme/api"],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conf = "my.fleet.lucius\tlucius.py\tinterval:600\tno\t\tFeature dev\n"

    out_dir = _render(tmp_path, conf, env={"ALFRED_HOME": str(runtime)})

    service = (out_dir / "alfred.release-captain.service").read_text()
    assert "/agent-launch custom-agent.py" in service
    assert "Environment=AGENT_CODENAME=release-captain" in service
    assert 'Environment=ALFRED_RELEASE_CAPTAIN_ROLE="Release coordinator"' in service


def test_render_skips_custom_agent_rows_that_collide_with_base_conf(tmp_path):
    runtime = tmp_path / "runtime"
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    shutil.copy(REPO_ROOT / "lib" / "custom_agents.py", lib_dir / "custom_agents.py")
    store = runtime / "state" / "custom-agents"
    store.mkdir(parents=True)
    (store / "custom-agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers for the operator.",
                        "engine": "hybrid",
                        "schedule": "interval:1800",
                        "repos": ["acme/api"],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conf = "alfred.release-captain\tlucius.py\tinterval:600\tno\t\tFeature dev\n"

    out_dir = _render(tmp_path, conf, env={"ALFRED_HOME": str(runtime)})

    service = (out_dir / "alfred.release-captain.service").read_text()
    assert "/agent-launch lucius.py" in service
    assert "custom-agent.py" not in service
    assert len(list(out_dir.glob("alfred.release-captain.service"))) == 1


def test_render_supports_custom_agents_without_base_agents_conf(tmp_path):
    runtime = tmp_path / "runtime"
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    shutil.copy(REPO_ROOT / "lib" / "custom_agents.py", lib_dir / "custom_agents.py")
    store = runtime / "state" / "custom-agents"
    store.mkdir(parents=True)
    (store / "custom-agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers for the operator.",
                        "engine": "hybrid",
                        "schedule": "interval:1800",
                        "repos": ["acme/api"],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    work = tmp_path / "systemd"
    launchd_dir = tmp_path / "launchd"
    work.mkdir()
    launchd_dir.mkdir()
    shutil.copy(RENDER_SH, work / "render.sh")
    shutil.copy(SERVICE_TEMPLATE, work / "_template.service")
    shutil.copy(TIMER_TEMPLATE, work / "_template.timer")
    out_dir = tmp_path / "out"

    res = subprocess.run(
        ["bash", str(work / "render.sh"), str(out_dir)],
        capture_output=True,
        text=True,
        env={**os.environ.copy(), "ALFRED_HOME": str(runtime)},
    )

    assert res.returncode == 0, res.stderr
    service = (out_dir / "alfred.release-captain.service").read_text()
    assert f"ExecStart={runtime}/bin/agent-launch custom-agent.py" in service
    assert "Environment=AGENT_CODENAME=release-captain" in service


def test_render_fails_when_custom_agent_manifest_is_malformed(tmp_path):
    runtime = tmp_path / "runtime"
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    shutil.copy(REPO_ROOT / "lib" / "custom_agents.py", lib_dir / "custom_agents.py")
    store = runtime / "state" / "custom-agents"
    store.mkdir(parents=True)
    (store / "custom-agents.json").write_text('{"version": 1, "agents": [', encoding="utf-8")
    work = tmp_path / "systemd"
    launchd_dir = tmp_path / "launchd"
    work.mkdir()
    launchd_dir.mkdir()
    shutil.copy(RENDER_SH, work / "render.sh")
    shutil.copy(SERVICE_TEMPLATE, work / "_template.service")
    shutil.copy(TIMER_TEMPLATE, work / "_template.timer")
    (launchd_dir / "agents.conf").write_text(
        "my.fleet.lucius\tlucius.py\tinterval:600\tno\t\tFeature dev\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    res = subprocess.run(
        ["bash", str(work / "render.sh"), str(out_dir)],
        capture_output=True,
        text=True,
        env={**_render_env(tmp_path), "ALFRED_HOME": str(runtime)},
    )

    assert res.returncode != 0
    assert "custom agent manifest invalid" in res.stderr
    assert "not valid JSON" in res.stderr


def test_render_quotes_execstart_when_alfred_home_has_spaces(tmp_path):
    custom_home = tmp_path / "runtime home"
    custom_home.mkdir()
    (custom_home / ".env").write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    conf = "my.fleet.memory-auto-promote\tmemory-auto-promote.py\tinterval:3600\tno\n"

    out_dir = _render(tmp_path, conf, env={"ALFRED_HOME": str(custom_home)})

    service = (out_dir / "my.fleet.memory-auto-promote.service").read_text()
    assert f'ExecStart="{custom_home}/bin/agent-launch" memory-auto-promote.py' in service
    assert f'Environment=ALFRED_HOME="{custom_home}"' in service


def test_render_ignores_legacy_alfredrc_environment(tmp_path):
    custom_rc = tmp_path / "custom.alfredrc"
    custom_rc.write_text("ALFRED_HOME=/stale\nWORKSPACE_ROOT=/stale\n", encoding="utf-8")
    conf = "my.fleet.memory-auto-promote\tmemory-auto-promote.py\tinterval:3600\tno\n"

    out_dir = _render(tmp_path, conf, env={"ALFREDRC": str(custom_rc)})

    service = (out_dir / "my.fleet.memory-auto-promote.service").read_text()
    assert "Environment=ALFREDRC=" not in service
    assert "Environment=ALFRED_HOME=/stale" not in service
    assert "Environment=WORKSPACE_ROOT=/stale" not in service


def test_render_loads_runtime_env_file(tmp_path):
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    (runtime / ".env").write_text(f"WORKSPACE_ROOT={workspace}\n", encoding="utf-8")
    conf = "my.fleet.memory-auto-promote\tmemory-auto-promote.py\tinterval:3600\tno\n"

    out_dir = _render(tmp_path, conf, env={"ALFRED_HOME": str(runtime)})

    service = (out_dir / "my.fleet.memory-auto-promote.service").read_text()
    assert "Environment=ALFREDRC=" not in service
    assert f"Environment=ALFRED_HOME={runtime}" in service
    assert f"Environment=WORKSPACE_ROOT={workspace}" in service


def test_render_preserves_process_layout_over_runtime_env(tmp_path):
    process_home = tmp_path / "process-runtime"
    process_workspace = tmp_path / "process-workspace"
    process_home.mkdir()
    (process_home / ".env").write_text("WORKSPACE_ROOT=/stale\n", encoding="utf-8")
    conf = "my.fleet.memory-auto-promote\tmemory-auto-promote.py\tinterval:3600\tno\n"

    out_dir = _render(
        tmp_path,
        conf,
        env={
            "ALFRED_HOME": str(process_home),
            "WORKSPACE_ROOT": str(process_workspace),
        },
    )

    service = (out_dir / "my.fleet.memory-auto-promote.service").read_text()
    assert f"Environment=ALFRED_HOME={process_home}" in service
    assert f"Environment=WORKSPACE_ROOT={process_workspace}" in service


def test_render_omits_java_home_when_needs_java_no(tmp_path):
    # render.sh derives JAVA_HOME from `command -v java` or /usr/lib/jvm; on a
    # host with no java it omits the block with a warning, so the positive
    # case is not portable. The negative is: needs_java=no agents never get a
    # JAVA_HOME line regardless of host.
    conf = "my.fleet.plain\tplain.py\tinterval:600\tno\n"
    out_dir = _render(tmp_path, conf)
    service = (out_dir / "my.fleet.plain.service").read_text()
    assert "JAVA_HOME" not in service
    # A needs_java=yes agent still renders a unit file even when java is
    # absent, the renderer just skips the JAVA_HOME line and warns.
    java_conf = "my.fleet.bane\tbane.py\tcron:2:00\tyes\t\tNightly test author\n"
    java_out = _render(tmp_path, java_conf)
    java_service = (java_out / "my.fleet.bane.service").read_text()
    assert java_service.strip()  # rendered non-empty


def test_render_substitutes_home_with_systemd_specifier(tmp_path):
    conf = "my.fleet.lucius\tlucius.py\tinterval:600\tno\n"
    out_dir = _render(tmp_path, conf)
    service = (out_dir / "my.fleet.lucius.service").read_text()
    # The render host's literal $HOME is replaced with systemd's %h so the
    # units are operator-agnostic. The fake HOME must not leak into the unit.
    assert str(tmp_path / "fakehome") not in service
    assert "%h/.alfred/bin/agent-launch" in service
    assert "WorkingDirectory=%h" in service


def test_render_unknown_schedule_fails(tmp_path):
    conf = "my.fleet.broken\tbroken.py\tevery-thursday\tno\n"
    systemd_dir = tmp_path / "systemd"
    launchd_dir = tmp_path / "launchd"
    systemd_dir.mkdir()
    launchd_dir.mkdir()
    shutil.copy(RENDER_SH, systemd_dir / "render.sh")
    shutil.copy(SERVICE_TEMPLATE, systemd_dir / "_template.service")
    shutil.copy(TIMER_TEMPLATE, systemd_dir / "_template.timer")
    (launchd_dir / "agents.conf").write_text(conf)
    res = subprocess.run(
        ["bash", str(systemd_dir / "render.sh"), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        env=_render_env(tmp_path),
    )
    # render_one returns non-zero for an unknown schedule; the per-row error
    # is printed to stderr. The overall script keeps going (the while-loop
    # body swallows the failure), so just assert the diagnostic surfaced.
    assert "unknown schedule format" in res.stderr
