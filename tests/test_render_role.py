"""Smoke tests for launchd/render.sh, role-column wiring.

These shell out to bash. The render script reads ``agents.conf`` from a
caller-controlled path and writes plists into a caller-controlled output
dir, so the tests stand up a tiny conf with one record and inspect the
resulting plist.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_SH = REPO_ROOT / "launchd" / "render.sh"
TEMPLATE = REPO_ROOT / "launchd" / "_template.plist"


def _render(tmp_path: Path, conf_text: str, env: dict[str, str] | None = None) -> Path:
    """Run render.sh against a temp launchd dir. Returns the output dir."""
    work = tmp_path / "launchd"
    work.mkdir()
    shutil.copy(RENDER_SH, work / "render.sh")
    shutil.copy(TEMPLATE, work / "_template.plist")
    (work / "agents.conf").write_text(conf_text)
    out_dir = tmp_path / "out"
    full_env = os.environ.copy()
    full_env.pop("ALFREDRC", None)
    full_env.pop("ALFRED_HOME", None)
    full_env.pop("WORKSPACE_ROOT", None)
    full_env.update(env or {})
    res = subprocess.run(
        ["bash", str(work / "render.sh"), str(out_dir)],
        capture_output=True,
        text=True,
        env=full_env,
    )
    if res.returncode != 0:
        pytest.fail(f"render.sh failed: {res.stderr}")
    return out_dir


def test_render_emits_alfred_role_env_when_role_column_set(tmp_path):
    conf = "my.fleet.lucius\tlucius.py\tinterval:600\tno\t\tSingle-repo feature engineer\n"
    out_dir = _render(tmp_path, conf)
    plist = (out_dir / "my.fleet.lucius.plist").read_text()
    assert "ALFRED_LUCIUS_ROLE" in plist
    assert "<string>Single-repo feature engineer</string>" in plist


def test_render_omits_role_block_when_column_empty(tmp_path):
    conf = "my.fleet.poller\tpoller.py\tinterval:1200\tno\n"
    out_dir = _render(tmp_path, conf)
    plist = (out_dir / "my.fleet.poller.plist").read_text()
    # No ALFRED_*_ROLE key emitted when the column is empty.
    assert "ALFRED_POLLER_ROLE" not in plist
    assert "_ROLE" not in plist


def test_render_translates_dash_in_compound_codename(tmp_path):
    conf = "my.fleet.brand-mention-scanner\tscan.py\tinterval:7200\tno\t\tBrand mention monitor\n"
    out_dir = _render(tmp_path, conf)
    plist = (out_dir / "my.fleet.brand-mention-scanner.plist").read_text()
    assert "ALFRED_BRAND_MENTION_SCANNER_ROLE" in plist


def test_render_escapes_xml_special_characters_in_role(tmp_path):
    role = "Reviewer for PRs <80 lines & rising"
    conf = f"my.fleet.r\treview.py\tinterval:600\tno\t\t{role}\n"
    out_dir = _render(tmp_path, conf)
    plist = (out_dir / "my.fleet.r.plist").read_text()
    # The literal `<` / `&` must be escaped so the plist is still XML.
    assert "&lt;" in plist
    assert "&amp;" in plist
    assert "<80" not in plist


def test_render_invokes_agent_launch_and_sets_codename_env(tmp_path):
    conf = "my.fleet.marshall\tlucius.py\tinterval:600\tno\t\tFeature dev\n"
    out_dir = _render(tmp_path, conf)
    plist_data = plistlib.loads((out_dir / "my.fleet.marshall.plist").read_bytes())
    assert Path(plist_data["ProgramArguments"][0]).name == "agent-launch"
    assert plist_data["ProgramArguments"][1] == "lucius.py"
    env = plist_data["EnvironmentVariables"]
    assert env["AGENT_CODENAME"] == "marshall"
    assert env["LAUNCHD_LABEL"] == "my.fleet.marshall"


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

    plist_data = plistlib.loads((out_dir / "alfred.release-captain.plist").read_bytes())
    assert plist_data["ProgramArguments"][1] == "custom-agent.py"
    env = plist_data["EnvironmentVariables"]
    assert env["AGENT_CODENAME"] == "release-captain"
    assert env["ALFRED_RELEASE_CAPTAIN_ROLE"] == "Release coordinator"


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

    plist_data = plistlib.loads((out_dir / "alfred.release-captain.plist").read_bytes())
    assert plist_data["ProgramArguments"][1] == "lucius.py"
    assert len(list(out_dir.glob("alfred.release-captain.plist"))) == 1


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
    work = tmp_path / "launchd"
    work.mkdir()
    shutil.copy(RENDER_SH, work / "render.sh")
    shutil.copy(TEMPLATE, work / "_template.plist")
    out_dir = tmp_path / "out"

    res = subprocess.run(
        ["bash", str(work / "render.sh"), str(out_dir)],
        capture_output=True,
        text=True,
        env={**os.environ.copy(), "ALFRED_HOME": str(runtime)},
    )

    assert res.returncode == 0, res.stderr
    plist_data = plistlib.loads((out_dir / "alfred.release-captain.plist").read_bytes())
    assert plist_data["ProgramArguments"][1] == "custom-agent.py"


def test_render_fails_when_custom_agent_manifest_is_malformed(tmp_path):
    runtime = tmp_path / "runtime"
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    shutil.copy(REPO_ROOT / "lib" / "custom_agents.py", lib_dir / "custom_agents.py")
    store = runtime / "state" / "custom-agents"
    store.mkdir(parents=True)
    (store / "custom-agents.json").write_text('{"version": 1, "agents": [', encoding="utf-8")
    work = tmp_path / "launchd"
    work.mkdir()
    shutil.copy(RENDER_SH, work / "render.sh")
    shutil.copy(TEMPLATE, work / "_template.plist")
    (work / "agents.conf").write_text(
        "my.fleet.lucius\tlucius.py\tinterval:600\tno\t\tFeature dev\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"

    res = subprocess.run(
        ["bash", str(work / "render.sh"), str(out_dir)],
        capture_output=True,
        text=True,
        env={**os.environ.copy(), "ALFRED_HOME": str(runtime)},
    )

    assert res.returncode != 0
    assert "custom agent manifest invalid" in res.stderr
    assert "not valid JSON" in res.stderr


def test_render_ignores_legacy_alfredrc_environment(tmp_path):
    custom_rc = tmp_path / "custom.alfredrc"
    custom_rc.write_text("ALFRED_HOME=/stale\nWORKSPACE_ROOT=/stale\n", encoding="utf-8")
    conf = "my.fleet.memory-auto-promote\tmemory-auto-promote.py\tinterval:3600\tno\n"

    out_dir = _render(tmp_path, conf, env={"ALFREDRC": str(custom_rc)})

    plist_data = plistlib.loads((out_dir / "my.fleet.memory-auto-promote.plist").read_bytes())
    env = plist_data["EnvironmentVariables"]
    assert "ALFREDRC" not in env
    assert env["ALFRED_HOME"] != "/stale"
    assert env["WORKSPACE_ROOT"] != "/stale"


def test_render_loads_runtime_env_file(tmp_path):
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    runtime.mkdir()
    (runtime / ".env").write_text(f"WORKSPACE_ROOT={workspace}\n", encoding="utf-8")
    conf = "my.fleet.memory-auto-promote\tmemory-auto-promote.py\tinterval:3600\tno\n"

    out_dir = _render(tmp_path, conf, env={"ALFRED_HOME": str(runtime)})

    plist_data = plistlib.loads((out_dir / "my.fleet.memory-auto-promote.plist").read_bytes())
    env = plist_data["EnvironmentVariables"]
    assert "ALFREDRC" not in env
    assert env["ALFRED_HOME"] == str(runtime)
    assert env["WORKSPACE_ROOT"] == str(workspace)


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

    plist_data = plistlib.loads((out_dir / "my.fleet.memory-auto-promote.plist").read_bytes())
    env = plist_data["EnvironmentVariables"]
    assert env["ALFRED_HOME"] == str(process_home)
    assert env["WORKSPACE_ROOT"] == str(process_workspace)
