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

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_SH = REPO_ROOT / "systemd" / "render.sh"
SERVICE_TEMPLATE = REPO_ROOT / "systemd" / "_template.service"
TIMER_TEMPLATE = REPO_ROOT / "systemd" / "_template.timer"


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
    # render.sh from picking up the developer's real ~/.alfredrc.
    full_env = {**os.environ, "HOME": str(tmp_path / "fakehome"), **(env or {})}
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


def test_render_passes_custom_alfredrc_to_systemd_environment(tmp_path):
    custom_rc = tmp_path / "custom.alfredrc"
    custom_rc.write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    conf = "my.fleet.memory-auto-promote\tmemory-auto-promote.py\tinterval:3600\tno\n"

    out_dir = _render(tmp_path, conf, env={"ALFREDRC": str(custom_rc)})

    service = (out_dir / "my.fleet.memory-auto-promote.service").read_text()
    assert f"Environment=ALFREDRC={custom_rc}" in service


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
        env={**os.environ, "HOME": str(tmp_path / "fakehome")},
    )
    # render_one returns non-zero for an unknown schedule; the per-row error
    # is printed to stderr. The overall script keeps going (the while-loop
    # body swallows the failure), so just assert the diagnostic surfaced.
    assert "unknown schedule format" in res.stderr
