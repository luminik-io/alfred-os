"""Tests for ``bin/alfred-label-state.py``.

The CLI is a thin argparse layer; the real work is in ``agent_runner``
and ``labels``. We load the CLI as a module under a sanitised name
(``alfred_label_state``) and monkeypatch the ``agent_runner`` functions
it imported so no GitHub calls happen.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin" / "alfred-label-state.py"
LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))


@pytest.fixture()
def cli_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Load bin/alfred-label-state.py as an importable module.

    The dash in the filename means it can't be imported as
    ``alfred-label-state``; ``importlib`` gives us a clean handle.
    """
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
    (tmp_path / ".alfred" / "lib").mkdir(parents=True)
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    monkeypatch.delenv("LABEL_STATE_SWEEP_REPOS", raising=False)

    spec = importlib.util.spec_from_file_location("alfred_label_state", BIN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alfred_label_state"] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_state(**overrides) -> dict:
    base = {
        "repo": "your-backend",
        "number": 42,
        "state": "OPEN",
        "labels": ["agent:implement"],
        "claimable": True,
        "in_flight": False,
        "pr_open": False,
        "do_not_pickup": False,
        "needs_human_scope": False,
        "repo_paused": False,
        "latest_claim": None,
    }
    base.update(overrides)
    return base


def test_parse_issue_ref_valid(cli_module) -> None:
    assert cli_module.parse_issue_ref("your-backend#42") == ("your-backend", 42)


def test_parse_issue_ref_invalid(cli_module) -> None:
    with pytest.raises(SystemExit):
        cli_module.parse_issue_ref("not-a-ref")


def test_doctor_mode_short_circuits(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALFRED_DOCTOR", "1")
    rc = cli_module.main(["claim", "your-backend#42"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ALFRED-LABEL-STATE-DOCTOR-OK]" in out


def test_claim_calls_gh_issue_edit_with_do_not_pickup(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    monkeypatch.setattr(cli_module, "issue_dedup_check", lambda r, n: _stub_state())
    monkeypatch.setattr(
        cli_module,
        "gh_issue_edit",
        lambda r, n, **kw: calls.append({"repo": r, "n": n, **kw}) or True,
    )
    rc = cli_module.main(["claim", "your-backend#42"])
    assert rc == 0
    assert calls == [{"repo": "your-backend", "n": 42, "add_labels": ["do-not-pickup"]}]
    assert "claimed your-backend#42" in capsys.readouterr().out


def test_claim_refused_when_pr_open(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module, "issue_dedup_check", lambda r, n: _stub_state(pr_open=True)
    )
    monkeypatch.setattr(cli_module, "gh_issue_edit", lambda *a, **k: True)
    rc = cli_module.main(["claim", "your-backend#42"])
    assert rc == 2
    assert "PR open" in capsys.readouterr().err


def test_claim_refused_when_in_flight_without_force(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module,
        "issue_dedup_check",
        lambda r, n: _stub_state(
            in_flight=True,
            latest_claim={"codename": "lucius", "firing_id": "20260501-1"},
        ),
    )
    monkeypatch.setattr(cli_module, "gh_issue_edit", lambda *a, **k: True)
    rc = cli_module.main(["claim", "your-backend#42"])
    assert rc == 2


def test_claim_overrides_in_flight_with_force(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module,
        "issue_dedup_check",
        lambda r, n: _stub_state(
            in_flight=True,
            latest_claim={"codename": "lucius", "firing_id": "20260501-1"},
        ),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        cli_module,
        "gh_issue_edit",
        lambda r, n, **kw: calls.append({"repo": r, **kw}) or True,
    )
    rc = cli_module.main(["claim", "your-backend#42", "--force"])
    assert rc == 0
    assert calls and calls[0]["add_labels"] == ["do-not-pickup"]


def test_release_clears_do_not_pickup(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        cli_module,
        "gh_issue_edit",
        lambda r, n, **kw: calls.append({"repo": r, "n": n, **kw}) or True,
    )
    rc = cli_module.main(["release", "your-backend#42"])
    assert rc == 0
    assert calls == [{"repo": "your-backend", "n": 42, "remove_labels": ["do-not-pickup"]}]
    assert "released your-backend#42" in capsys.readouterr().out


def test_dedup_check_exits_zero_when_claimable(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "issue_dedup_check", lambda r, n: _stub_state())
    assert cli_module.main(["dedup-check", "your-backend#42"]) == 0


def test_dedup_check_exits_nonzero_when_blocked(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module,
        "issue_dedup_check",
        lambda r, n: _stub_state(claimable=False, do_not_pickup=True),
    )
    assert cli_module.main(["dedup-check", "your-backend#42"]) == 1
    err = capsys.readouterr().err
    assert "do-not-pickup" in err


def test_dedup_check_json(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module,
        "issue_dedup_check",
        lambda r, n: _stub_state(claimable=False, in_flight=True),
    )
    cli_module.main(["dedup-check", "your-backend#42", "--json"])
    import json as _json

    out = capsys.readouterr().out
    payload = _json.loads(out)
    assert "reasons" in payload
    assert any("in-flight" in r for r in payload["reasons"])


def test_status_issue_prints_keys(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "issue_dedup_check", lambda r, n: _stub_state())
    assert cli_module.main(["status-issue", "your-backend#42"]) == 0
    out = capsys.readouterr().out
    assert "repo:" in out
    assert "state:" in out
    assert "labels:" in out


def test_repo_list_empty(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "list_paused_repos", lambda: [])
    assert cli_module.main(["repo", "list"]) == 0
    assert "no repos paused" in capsys.readouterr().out


def test_repo_pause_and_resume(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, list[str]] = {"paused": []}

    def fake_set(repo: str, paused: bool) -> list[str]:
        if paused:
            state["paused"] = sorted(set(state["paused"]) | {repo})
        else:
            state["paused"] = [r for r in state["paused"] if r != repo]
        return list(state["paused"])

    monkeypatch.setattr(cli_module, "set_repo_paused", fake_set)
    assert cli_module.main(["repo", "pause", "your-backend"]) == 0
    assert state["paused"] == ["your-backend"]
    out = capsys.readouterr().out
    assert "paused your-backend" in out

    assert cli_module.main(["repo", "resume", "your-backend"]) == 0
    assert state["paused"] == []


def test_repo_pause_without_arg_errors(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "set_repo_paused", lambda *a, **k: [])
    assert cli_module.main(["repo", "pause"]) == 2


def test_sweep_claims_dry_run_uses_env_repos(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LABEL_STATE_SWEEP_REPOS", "your-backend,your-frontend")
    monkeypatch.setattr(
        cli_module,
        "find_stale_claims",
        lambda repo, *, max_age_hours: [
            {
                "number": 100,
                "codename": "lucius",
                "firing_id": "x",
                "age_hours": 5.0,
                "title": "stuck issue",
            }
        ]
        if repo == "your-backend"
        else [],
    )

    force_calls: list[tuple[str, int]] = []

    def fake_force(repo: str, num: int, **kw) -> bool:
        force_calls.append((repo, num))
        return True

    monkeypatch.setattr(cli_module, "force_release_stale_claim", fake_force)

    rc = cli_module.main(["sweep-claims", "--dry-run"])
    assert rc == 0
    assert force_calls == [], "dry-run must not force-release"
    out = capsys.readouterr().out
    assert "your-backend: 1 stale claim" in out
    assert "dry-run" in out


def test_sweep_claims_force_releases(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module,
        "find_stale_claims",
        lambda repo, *, max_age_hours: [
            {
                "number": 99,
                "codename": "lucius",
                "firing_id": "x",
                "age_hours": 5.0,
                "title": "stuck",
            }
        ],
    )
    force_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        cli_module,
        "force_release_stale_claim",
        lambda repo, num, **kw: (force_calls.append((repo, num)), True)[1],
    )
    rc = cli_module.main(["sweep-claims", "--repo", "your-backend"])
    assert rc == 0
    assert force_calls == [("your-backend", 99)]


def test_sweep_claims_errors_when_no_repos(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LABEL_STATE_SWEEP_REPOS", raising=False)
    rc = cli_module.main(["sweep-claims"])
    assert rc == 2
    assert "no repos to sweep" in capsys.readouterr().err
