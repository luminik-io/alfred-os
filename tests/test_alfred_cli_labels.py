"""Tests for ``alfred labels`` operator commands."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin" / "alfred"
LIB = REPO_ROOT / "lib"
sys.path.insert(0, str(LIB))


@pytest.fixture()
def cli_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
    monkeypatch.setenv("GH_ORG", "acme")
    loader = SourceFileLoader("alfred_cli_labels", str(BIN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alfred_cli_labels"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_label_catalogue_includes_lifecycle_batman_and_operator_labels(cli_module) -> None:
    names = {name for name, _, _ in cli_module._label_bootstrap_catalog()}
    assert "agent:implement" in names
    assert "agent:large-feature" in names
    assert "agent:authored" in names
    assert "agent:plan-pending-approval" in names
    assert "do-not-merge" in names


def test_labels_check_reports_missing_without_creating(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "label", "list"]:
            existing = [{"name": "agent:implement", "color": "0e8a16", "description": ""}]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(existing), stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    rc = cli_module.main(["labels", "bootstrap", "your-backend", "--check"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "labels check on acme/your-backend" in out
    assert "agent:in-flight (MISSING)" in out
    assert all(cmd[:3] == ["gh", "label", "list"] for cmd in calls)


def test_labels_bootstrap_creates_missing_labels(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd[:3] == ["gh", "label", "create"]:
            created.append(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    rc = cli_module.main(["labels", "bootstrap", "your-backend"])

    assert rc == 0
    assert "agent:implement" in created
    assert "agent:large-feature" in created
    assert "do-not-merge" in created
    assert "labels bootstrap on acme/your-backend" in capsys.readouterr().out


def test_labels_all_reads_fleet_repo_env(cli_module, monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path(os.environ["ALFRED_HOME"])
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "GH_ORG=acme\nALFRED_LUCIUS_REPOS=api,web\nALFRED_RASALGHUL_REPOS=web,mobile\n",
        encoding="utf-8",
    )
    repos: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_labels_bootstrap_one",
        lambda repo, *, check, force: repos.append(repo) or 0,
    )

    assert cli_module.main(["labels", "check", "--all"]) == 0
    assert repos == ["api", "web", "mobile"]


def test_setup_token_forwards_paste_back_token(cli_module, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.main(["setup-token", "--token", "runtime-token-value"]) == 0
    assert calls == [
        [
            sys.executable,
            str(BIN.parent / "alfred-setup-token.py"),
            "--token",
            "runtime-token-value",
        ]
    ]


def test_labels_all_hydrates_fleet_repo_env_from_runtime_env(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(os.environ["ALFRED_HOME"])
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "GH_ORG=acme\nALFRED_LUCIUS_REPOS=api,web\nALFRED_RASALGHUL_REPOS=web,mobile\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ALFRED_LUCIUS_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_RASALGHUL_REPOS", raising=False)
    repos: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_labels_bootstrap_one",
        lambda repo, *, check, force: repos.append(repo) or 0,
    )

    assert cli_module.main(["labels", "check", "--all"]) == 0
    assert repos == ["api", "web", "mobile"]


def test_runtime_env_loader_expands_home_tokens_like_agent_launch(
    cli_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    home = Path(os.environ["ALFRED_HOME"])
    home.mkdir(parents=True)
    env_file = home / ".env"
    env_file.write_text(
        "WORKSPACE_ROOT=$HOME/work\n"
        "CODEX_HOME=${HOME}/codex\n"
        "ALFRED_LITERAL='$HOME/not-expanded'\n",
        encoding="utf-8",
    )

    values = cli_module._read_env_values(env_file)

    assert values["WORKSPACE_ROOT"] == str(tmp_path / "work")
    assert values["CODEX_HOME"] == str(tmp_path / "codex")
    assert values["ALFRED_LITERAL"] == "$HOME/not-expanded"


def test_runtime_env_file_does_not_clobber_process_overrides(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(os.environ["ALFRED_HOME"])
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "GH_ORG=acme\nALFRED_LUCIUS_REPOS=api,web\nALFRED_TELEMETRY_ENABLED=1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALFRED_LUCIUS_REPOS", "manual/repo")
    monkeypatch.setenv("ALFRED_TELEMETRY_ENABLED", "0")
    repos: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_labels_bootstrap_one",
        lambda repo, *, check, force: repos.append(repo) or 0,
    )

    assert cli_module.main(["labels", "check", "--all"]) == 0
    assert repos == ["manual/repo"]
    assert os.environ["ALFRED_TELEMETRY_ENABLED"] == "0"


def test_capabilities_command_does_not_import_agent_runner(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("HOME", None)
    env["ALFRED_HOME"] = str(tmp_path / ".alfred")
    env["CODEX_HOME"] = str(tmp_path / "codex")
    env["CLAUDE_HOME"] = str(tmp_path / "claude")
    env["PYTHONPATH"] = str(LIB)
    code = f"""
import builtins
import importlib.util
import pathlib
import sys
from importlib.machinery import SourceFileLoader

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "agent_runner" or name.startswith("agent_runner.") or name == "scheduler":
        raise RuntimeError("blocked import should not be needed")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
pathlib.Path.home = staticmethod(
    lambda: (_ for _ in ()).throw(RuntimeError("no home"))
)
loader = SourceFileLoader("alfred_cli_no_agent_runner", {str(BIN)!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = module
spec.loader.exec_module(module)
raise SystemExit(module.main(["capabilities", "--json"]))
"""

    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)

    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["summary"]["total"] == 3


def test_clear_lock_clears_dead_lock(
    cli_module, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    posts: list[str] = []
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (12345, False, None))
    monkeypatch.setattr(cli_module, "_matching_worktree_risks", lambda agent, **kw: [])
    monkeypatch.setattr(
        cli_module, "_clear_lock_scheduler_health", lambda agent: "scheduler: loaded"
    )
    monkeypatch.setattr(
        cli_module.agent_runner, "slack_post", lambda text: posts.append(text) or True
    )

    assert cli_module.main(["clear-lock", "lucius"]) == 0
    assert not lock_dir.exists()
    out = capsys.readouterr().out
    assert "cleared" in out
    assert "scheduler: loaded" in out
    assert posts == [f"alfred clear-lock: cleared lucius lock at {lock_dir}; scheduler: loaded"]


def test_clear_lock_quiet_skips_slack_post(
    cli_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (12345, False, None))
    monkeypatch.setattr(cli_module, "_matching_worktree_risks", lambda agent, **kw: [])
    monkeypatch.setattr(
        cli_module, "_clear_lock_scheduler_health", lambda agent: "scheduler: loaded"
    )
    monkeypatch.setattr(
        cli_module.agent_runner,
        "slack_post",
        lambda text: (_ for _ in ()).throw(AssertionError("unexpected Slack post")),
    )

    assert cli_module.main(["clear-lock", "lucius", "--quiet"]) == 0
    assert not lock_dir.exists()


def test_clear_lock_refuses_live_matching_holder(
    cli_module, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (12345, True, True))
    monkeypatch.setattr(cli_module, "_matching_worktree_risks", lambda agent, **kw: [])

    assert cli_module.main(["clear-lock", "lucius"]) == 1
    assert lock_dir.exists()
    assert "refusing to clear" in capsys.readouterr().out


def test_clear_lock_refuses_unknown_holder(
    cli_module, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (None, False, None))
    monkeypatch.setattr(cli_module, "_matching_worktree_risks", lambda agent, **kw: [])

    assert cli_module.main(["clear-lock", "lucius"]) == 1
    assert lock_dir.exists()
    out = capsys.readouterr().out
    assert "pid is unknown" in out
    assert "refusing to clear" in out


def test_clear_lock_refuses_matching_unpushed_worktree(
    cli_module, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (12345, False, None))
    monkeypatch.setattr(
        cli_module,
        "_matching_worktree_risks",
        lambda agent, **kw: ["/tmp/wt-lucius (lucius/42, ahead of remote)"],
    )

    assert cli_module.main(["clear-lock", "lucius"]) == 1
    assert lock_dir.exists()
    out = capsys.readouterr().out
    assert "worktree risk" in out
    assert "refusing to clear" in out


def test_brain_command_forwards_to_brain_cli(cli_module, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.main(["brain", "lessons", "lucius", "org/api"]) == 0
    assert calls == [
        [
            sys.executable,
            str(REPO_ROOT / "bin" / "alfred-brain.py"),
            "lessons",
            "lucius",
            "org/api",
        ]
    ]


def test_code_memory_command_forwards_to_launcher(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.main(["code-memory", "doctor"]) == 0
    assert calls == [[str(REPO_ROOT / "bin" / "code-memory-mcp"), "doctor"]]


def test_code_memory_command_defaults_to_doctor(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.main(["code-memory"]) == 0
    assert calls == [[str(REPO_ROOT / "bin" / "code-memory-mcp"), "doctor"]]


def test_doctor_command_forwards_to_doctor_script(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.main(["doctor", "--dev", "--lifecycle"]) == 0
    assert calls == [["bash", str(REPO_ROOT / "bin" / "doctor.sh"), "--dev", "--lifecycle"]]


def test_capabilities_command_emits_json(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server import setup as setup_mod

    payload = {
        "version": 1,
        "summary": {"ready": 1, "actionable": 0, "disabled": 0, "total": 1},
        "capabilities": [
            {
                "key": "code_graph",
                "title": "Code graph memory",
                "category": "memory",
                "recommended": True,
                "state": "ready",
                "installed": True,
                "enabled": True,
                "detail": "ready",
                "detected": {},
                "install_hint": "none",
                "source": {"source": "DeusData/codebase-memory-mcp"},
            }
        ],
    }
    monkeypatch.setattr(setup_mod, "capability_status", lambda: payload)

    assert cli_module.main(["capabilities", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_capabilities_command_import_survives_unresolvable_home(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    (codex_home / "skills" / "gstack").mkdir(parents=True)
    (claude_home / "skills").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = {
        **os.environ,
        "ALFRED_HOME": str(runtime),
        "CODEX_HOME": str(codex_home),
        "CLAUDE_HOME": str(claude_home),
        "PYTHONPATH": str(LIB),
    }
    env.pop("HOME", None)
    code = f"""
import importlib.util
import pathlib
import sys
from importlib.machinery import SourceFileLoader

pathlib.Path.home = staticmethod(
    lambda: (_ for _ in ()).throw(RuntimeError("no home"))
)
loader = SourceFileLoader("alfred_cli_cold_capabilities", {str(BIN)!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = mod
spec.loader.exec_module(mod)
raise SystemExit(mod.main(["capabilities", "--json"]))
"""

    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )

    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    skills = {item["key"]: item for item in payload["capabilities"]}["engineering_skills"]
    assert skills["state"] == "ready"
    assert skills["detected"]["paths"] == [str(codex_home / "skills" / "gstack")]


def test_claude_home_does_not_override_primary_auth_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    claude_home = tmp_path / "skill-claude-home"
    home.mkdir()
    runtime.mkdir()
    claude_home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "ALFRED_HOME": str(runtime),
        "CLAUDE_HOME": str(claude_home),
        "PYTHONPATH": str(LIB),
    }
    code = f"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader

loader = SourceFileLoader("alfred_cli_claude_home_auth", {str(BIN)!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = mod
spec.loader.exec_module(mod)
print(mod.PRIMARY_CLAUDE_DIR)
"""

    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(home / ".claude")
