"""Unit tests for the anonymous proof-telemetry reporter.

Covers the reporting guarantees:

* Explicit opt-out: no send, no install-id file, no network when the master
  switch is set to a disabled value.
* Default collector: with no custom URL, Alfred uses the hosted collector unless
  ALFRED_DEFAULT_TELEMETRY_URL is explicitly set empty.
* Correct, bounded payload shape when enabled.
* Fail-soft: a network error never raises; it returns a ``failed`` status.
* Idempotent-friendly install id: stable across calls, regenerated only when
  the file is missing.
* No PII in the payload (only anonymous counts plus install_id + period).

Nothing here touches the real ``$ALFRED_HOME`` or the network; the brain and
the HTTP poster are injected.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make ``lib/`` importable from the repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "lib"))

import proof_telemetry as pt  # noqa: E402


def _load_cli_wrapper():
    """Load the ``bin/proof-telemetry.py`` scheduler wrapper as a module.

    The filename has a hyphen, so it is not importable by name; load it from
    its path. This is the script doctor.sh / launchd actually invoke, and the
    home of the ALFRED_DOCTOR fast path under test.
    """
    path = _REPO / "bin" / "proof-telemetry.py"
    spec = importlib.util.spec_from_file_location("proof_telemetry_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli_wrapper()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakePR:
    """A cached github_items PR row.

    By default a FakePR is AGENT-AUTHORED: it carries the ``agent:authored``
    provenance label, so it is counted by the proof counter. Pass
    ``authored=False`` to model an operator-/bot-opened PR (no agent label, no
    agent branch prefix) that the poller cached but that must NOT be counted.
    The authorship signal can also be supplied directly via ``labels`` or
    ``head_ref`` to exercise the branch-prefix path.
    """

    def __init__(
        self,
        state: str,
        *,
        authored: bool = True,
        labels=None,
        head_ref=None,
        changed_files: int = 0,
        additions: int = 0,
        deletions: int = 0,
        created_at: datetime | None = None,
        closed_at: datetime | None = None,
        merged_at: datetime | None = None,
    ) -> None:
        self.state = state
        self.changed_files = changed_files
        self.additions = additions
        self.deletions = deletions
        self.created_at = created_at or datetime(2026, 6, 1, tzinfo=UTC)
        self.merged_at = merged_at or (
            datetime(2026, 6, 10, tzinfo=UTC) if state == "merged" else None
        )
        self.closed_at = closed_at or (
            datetime(2026, 6, 10, tzinfo=UTC) if state in {"merged", "closed"} else None
        )
        if labels is not None or head_ref is not None:
            self.labels = list(labels or [])
            self.head_ref = head_ref
        elif authored:
            self.labels = ["agent:authored"]
            self.head_ref = None
        else:
            self.labels = []
            self.head_ref = None


class FakeTouch:
    def __init__(self, touched_at: datetime | None = None) -> None:
        self.touched_at = touched_at or datetime(2026, 6, 5, tzinfo=UTC)


class FakeIssue:
    def __init__(
        self,
        state: str = "open",
        *,
        labels=None,
        created_at: datetime | None = None,
        closed_at: datetime | None = None,
    ) -> None:
        self.state = state
        self.labels = list(labels if labels is not None else ["agent:implement"])
        self.head_ref = None
        self.created_at = created_at or datetime(2026, 6, 2, tzinfo=UTC)
        self.closed_at = closed_at or (
            datetime(2026, 6, 11, tzinfo=UTC) if state == "closed" else None
        )


def _authored(prs):
    """Filter to the agent-authored subset the proof counter must count."""
    return [p for p in prs if pt._row_is_agent_authored(p)]


def _agent_labeled(rows):
    """Filter to rows carrying an agent:* label."""
    return [row for row in rows if pt._row_is_agent_labeled(row)]


class FakeBrain:
    """Stand-in for FleetBrain exposing the two methods derive_counts calls.

    Honors the ``limit`` (returns at most ``limit`` rows, like the real brain's
    top-N list), the ``state`` filter on ``list_github_items``, so the
    state-based counting path is exercised. ``raise_on`` forces a failure for
    fail-soft tests. ``list_github_items`` returns ALL cached PRs (authored or
    not); derive_counts applies the agent-authored filter itself via the
    list-fallback (this brain exposes no count_* method).
    """

    def __init__(self, prs=None, issues=None, touches=None, raise_on=None):
        self._prs = prs or []
        self._issues = issues or []
        self._touches = touches or []
        self._raise_on = raise_on or set()

    def list_github_items(self, *, kind=None, state=None, limit=50):
        if "prs" in self._raise_on:
            raise RuntimeError("brain unavailable")
        assert kind in {"pr", "issue"}
        rows = self._prs if kind == "pr" else self._issues
        if state is not None:
            rows = [p for p in rows if getattr(p, "state", None) == state]
        return list(rows)[:limit]

    def list_file_touches(self, *, limit=50):
        if "touches" in self._raise_on:
            raise RuntimeError("brain unavailable")
        return list(self._touches)[:limit]


class NoStateBrain:
    """Older brain whose list_github_items has no ``state`` kwarg.

    Verifies derive_counts falls back to an in-memory tally without raising when
    the state-filtered counting path is unavailable. The fallback still filters
    to agent-authored rows.
    """

    def __init__(self, prs=None, issues=None, touches=None):
        self._prs = prs or []
        self._issues = issues or []
        self._touches = touches or []

    def list_github_items(self, *, kind=None, limit=50):
        assert kind in {"pr", "issue"}
        rows = self._prs if kind == "pr" else self._issues
        return list(rows)[:limit]

    def list_file_touches(self, *, limit=50):
        return list(self._touches)[:limit]


class ClampingBrain:
    """Brain that models the REAL FleetBrain: list_* clamps limit to 500, and
    exact count_* methods exist (a SQL COUNT(*) that is NOT capped). The
    ``count_github_items`` accepts ``authored_only`` and applies the
    agent-authored filter in "SQL" (here, in-memory) exactly like the real store.

    Regression guard for finding #4: the old code counted by raising the list
    limit, which never works against a brain that re-clamps to 500. The fix
    prefers count_* so a busy install (>500 rows) reports the true total.
    """

    LIST_CAP = 500

    def __init__(self, prs=None, issues=None, touches=None):
        self._prs = prs or []
        self._issues = issues or []
        self._touches = touches or []

    def list_github_items(self, *, kind=None, state=None, limit=50):
        assert kind in {"pr", "issue"}
        rows = self._prs if kind == "pr" else self._issues
        if state is not None:
            rows = [p for p in rows if getattr(p, "state", None) == state]
        # Mirror FleetBrain.list_github_items: clamp the effective limit to 500.
        clamped = max(1, min(int(limit), self.LIST_CAP))
        return list(rows)[:clamped]

    def count_github_items(
        self,
        *,
        kind=None,
        state=None,
        authored_only=False,
        agent_labeled_only=False,
    ):
        assert kind in {"pr", "issue"}
        rows = self._prs if kind == "pr" else self._issues
        if state is not None:
            rows = [p for p in rows if getattr(p, "state", None) == state]
        if authored_only:
            rows = _authored(rows)
        if agent_labeled_only:
            rows = _agent_labeled(rows)
        return len(rows)  # exact COUNT(*), no cap

    def list_file_touches(self, *, limit=50):
        clamped = max(1, min(int(limit), self.LIST_CAP))
        return list(self._touches)[:clamped]

    def count_file_touches(self):
        return len(self._touches)  # exact COUNT(*), no cap

    def sum_github_changed_lines(
        self,
        *,
        kind=None,
        state=None,
        authored_only=False,
        agent_labeled_only=False,
    ):
        assert kind in {"pr", "issue"}
        rows = self._prs if kind == "pr" else self._issues
        if state is not None:
            rows = [p for p in rows if getattr(p, "state", None) == state]
        if authored_only:
            rows = _authored(rows)
        if agent_labeled_only:
            rows = _agent_labeled(rows)
        return sum(max(0, int(getattr(row, "additions", 0))) for row in rows) + sum(
            max(0, int(getattr(row, "deletions", 0))) for row in rows
        )


class GitHubFileCountsBrain(ClampingBrain):
    def sum_github_changed_files(
        self,
        *,
        kind=None,
        state=None,
        authored_only=False,
        agent_labeled_only=False,
        merged_since=None,
        **_filters,
    ):
        assert kind in {"pr", "issue"}
        rows = self._prs if kind == "pr" else self._issues
        if state is not None:
            rows = [p for p in rows if getattr(p, "state", None) == state]
        if authored_only:
            rows = _authored(rows)
        if agent_labeled_only:
            rows = _agent_labeled(rows)
        if merged_since is not None:
            rows = [
                p
                for p in rows
                if p.merged_at is not None and p.merged_at.astimezone(UTC) >= merged_since
            ]
        return sum(max(0, int(getattr(row, "changed_files", 0) or 0)) for row in rows)


class ClampingNoCountBrain:
    """Older brain: list_* clamps to 500 and there is NO count_* method.

    Verifies the paginating fallback degrades HONESTLY: it stops at the list
    clamp (the true max it can observe) rather than silently misreporting or
    looping forever. The total is the clamp, not a fabricated number.
    """

    LIST_CAP = 500

    def __init__(self, prs=None, issues=None, touches=None):
        self._prs = prs or []
        self._issues = issues or []
        self._touches = touches or []

    def list_github_items(self, *, kind=None, state=None, limit=50):
        assert kind in {"pr", "issue"}
        rows = self._prs if kind == "pr" else self._issues
        if state is not None:
            rows = [p for p in rows if getattr(p, "state", None) == state]
        clamped = max(1, min(int(limit), self.LIST_CAP))
        return list(rows)[:clamped]

    def list_file_touches(self, *, limit=50):
        clamped = max(1, min(int(limit), self.LIST_CAP))
        return list(self._touches)[:clamped]


class FailingBaseBrain:
    """Brain whose base (no-state) PR query RAISES, but state-filtered queries
    would succeed and return non-zero.

    Regression guard for the Greptile finding (#5): a failed base prs_opened
    query must suppress prs_merged/prs_reviewed, never emit prs_opened:0 with
    prs_merged:N.
    """

    def __init__(self, prs=None, touches=None):
        self._prs = prs or []
        self._touches = touches or []

    def list_github_items(self, *, kind=None, state=None, limit=50):
        assert kind == "pr"
        if state is None:
            # The base "all PRs" query is the one that fails.
            raise RuntimeError("base PR query unavailable")
        rows = [p for p in self._prs if getattr(p, "state", None) == state]
        return list(rows)[:limit]

    def count_github_items(
        self,
        *,
        kind=None,
        state=None,
        authored_only=False,
        agent_labeled_only=False,
    ):
        assert kind == "pr"
        if state is None:
            raise RuntimeError("base PR count unavailable")
        rows = [p for p in self._prs if getattr(p, "state", None) == state]
        if authored_only:
            rows = _authored(rows)
        return len(rows)

    def list_file_touches(self, *, limit=50):
        return list(self._touches)[:limit]

    def count_file_touches(self):
        return len(self._touches)


class PagingNoCountBrain:
    """Older brain with NO count_* method and NO list clamp: list_github_items
    honors any ``limit`` and returns that many RAW rows from the top.

    Models a brain whose stored PRs mix agent-authored and operator rows. The
    list-fallback counter must page on the RAW row count, not the post-filter
    authored count: a page can hold fewer AUTHORED matches than ``limit`` while
    the brain still has more rows. Regression guard for Codex finding #3 (early
    termination when a filtered lister returns a short page).
    """

    def __init__(self, prs=None, touches=None):
        self._prs = prs or []
        self._touches = touches or []

    def list_github_items(self, *, kind=None, state=None, limit=50):
        assert kind == "pr"
        rows = self._prs
        if state is not None:
            rows = [p for p in rows if getattr(p, "state", None) == state]
        # No clamp: return exactly the requested top-N raw rows.
        return list(rows)[:limit]

    def list_file_touches(self, *, limit=50):
        return list(self._touches)[:limit]


class RecordingPoster:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        return self.ok


def boom_poster(url, payload):
    raise OSError("network down")


FIXED = datetime(2026, 6, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# is_enabled: the opt-out switch
# ---------------------------------------------------------------------------
def test_enabled_when_env_unset():
    assert pt.is_enabled({}) is True


def test_disabled_for_explicit_opt_out_values():
    for value in [
        "0",
        "false",
        "False",
        "no",
        "off",
        "disabled",
        " DISABLED ",
        "0 # opt out",
        "false # disable reporting",
    ]:
        env = {pt.ENABLE_ENV: value}
        assert pt.is_enabled(env) is False, f"{value!r} must opt out"


def test_enabled_for_non_disabled_values():
    for value in ["1", "true", "yes", "on", "TRUE", "1 ", " ", "", "2", "10"]:
        env = {pt.ENABLE_ENV: value}
        assert pt.is_enabled(env) is True, f"{value!r} should stay enabled"


# ---------------------------------------------------------------------------
# report_once: disabled / no-url paths send nothing
# ---------------------------------------------------------------------------
def test_report_once_disabled_is_a_no_op():
    poster = RecordingPoster()
    result = pt.report_once(env={pt.ENABLE_ENV: "0"}, brain=FakeBrain(), poster=poster)
    assert result == {"status": "disabled", "sent": False}
    assert poster.calls == [], "disabled telemetry must not call the network"


def test_report_once_disabled_does_not_create_install_id(tmp_path, monkeypatch):
    # Point the install-id path at a temp dir and assert nothing is written.
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    poster = RecordingPoster()
    pt.report_once(env={pt.ENABLE_ENV: "0"}, brain=FakeBrain(), poster=poster)
    assert not (tmp_path / "state" / "telemetry-install-id").exists()


def test_direct_dry_run_loads_alfredrc_opt_out(tmp_path):
    home = tmp_path / "home"
    alfred_home = tmp_path / "alfred"
    home.mkdir()
    alfred_home.mkdir()
    (home / ".alfredrc").write_text(
        "ALFRED_TELEMETRY_ENABLED=0\nALFRED_TELEMETRY_URL=https://telemetry.example.com/ingest\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ALFRED_HOME"] = str(alfred_home)
    env["PYTHONPATH"] = str(_REPO / "lib")
    for key in (pt.ENABLE_ENV, pt.URL_ENV, pt.TOKEN_ENV):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, str(_REPO / "bin" / "proof-telemetry.py"), "--dry-run"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert "[PROOF-TELEMETRY-DISABLED]" in result.stdout
    assert not (alfred_home / "state" / "telemetry-install-id").exists()


def test_script_prefers_checkout_lib_over_stale_alfred_home_lib(tmp_path):
    home = tmp_path / "home"
    alfred_home = tmp_path / "alfred"
    stale_lib = alfred_home / "lib"
    home.mkdir()
    stale_lib.mkdir(parents=True)
    (stale_lib / "proof_telemetry.py").write_text(
        "def is_enabled():\n    return True\ndef telemetry_url():\n    return ''\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ALFRED_HOME"] = str(alfred_home)
    env["ALFRED_DOCTOR"] = "1"
    env.pop("PYTHONPATH", None)
    for key in (pt.ENABLE_ENV, pt.URL_ENV, pt.TOKEN_ENV, pt.DEFAULT_URL_ENV):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, str(_REPO / "bin" / "proof-telemetry.py")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert "[PROOF-TELEMETRY-DOCTOR-OK]" in result.stdout
    assert "[PROOF-TELEMETRY-NO-URL]" not in result.stdout


def test_cli_maps_stale_counts_to_non_error_sentinel(monkeypatch, capsys):
    monkeypatch.setattr(pt, "report_once", lambda: {"status": "stale_counts", "sent": False})

    assert cli.main([]) == 0

    out = capsys.readouterr().out
    assert "[PROOF-TELEMETRY-STALE-COUNTS]" in out
    assert "[PROOF-TELEMETRY-ERROR]" not in out


def test_report_once_enabled_with_default_url_registers_and_sends(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    poster = RecordingPoster()
    registrar_calls = []

    def registrar(url, install_id):
        registrar_calls.append((url, install_id))
        return "install-token"

    result = pt.report_once(env={}, brain=FakeBrain(), poster=poster, registrar=registrar)
    assert result["status"] == "sent"
    assert result["sent"] is True
    assert len(registrar_calls) == 1
    assert registrar_calls[0][0] == pt.DEFAULT_INGEST_URL
    assert poster.calls and poster.calls[0][0] == pt.DEFAULT_INGEST_URL


def test_report_once_enabled_without_default_url_is_a_no_op():
    poster = RecordingPoster()
    result = pt.report_once(env={pt.DEFAULT_URL_ENV: ""}, brain=FakeBrain(), poster=poster)
    assert result["status"] == "no_url"
    assert result["sent"] is False
    assert poster.calls == []


# ---------------------------------------------------------------------------
# report_once: enabled path sends the right shape
# ---------------------------------------------------------------------------
def test_report_once_enabled_sends_expected_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    brain = FakeBrain(
        prs=[FakePR("merged"), FakePR("merged"), FakePR("closed"), FakePR("open")],
        issues=[
            FakeIssue(),
            FakeIssue("closed", labels=["agent:triage"]),
            FakeIssue("closed", labels=["bug"]),
        ],
        touches=[FakeTouch(), FakeTouch(), FakeTouch()],
    )
    poster = RecordingPoster(ok=True)
    env = {pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"}
    registrar_calls = []

    def registrar(url, install_id):
        registrar_calls.append((url, install_id))
        return "custom-install-token"

    result = pt.report_once(
        env=env,
        brain=brain,
        poster=poster,
        registrar=registrar,
        now=FIXED,
    )

    assert result["status"] == "sent"
    assert result["sent"] is True
    assert len(registrar_calls) == 1
    assert registrar_calls[0][0] == "https://telemetry.example.com/ingest"
    assert len(poster.calls) == 1
    url, payload = poster.calls[0]
    assert url == "https://telemetry.example.com/ingest"

    # Exact payload keys: nothing extra, no PII.
    assert set(payload.keys()) == {
        "install_id",
        "period",
        "prs_opened",
        "prs_merged",
        "prs_reviewed",
        "issues_opened",
        "issues_closed",
        "files_changed",
        "lines_changed",
        "loc_added",
        "last_30_days",
    }
    # Period is the stable lifetime bucket, not a calendar month, so a calendar
    # rollover never re-adds the cumulative total on the Worker.
    assert payload["period"] == "lifetime"
    assert payload["prs_opened"] == 4
    assert payload["prs_merged"] == 2
    # reviewed = merged + closed (terminal), never exceeds opened.
    assert payload["prs_reviewed"] == 3
    assert payload["issues_opened"] == 2
    assert payload["issues_closed"] == 1
    assert payload["files_changed"] == 3
    assert payload["lines_changed"] == 0
    assert payload["loc_added"] == 3
    assert payload["last_30_days"] == {
        "window_days": 30,
        "prs_opened": 4,
        "prs_merged": 2,
        "prs_reviewed": 3,
        "issues_opened": 2,
        "issues_closed": 1,
        "files_changed": 3,
        "lines_changed": 0,
    }
    assert isinstance(payload["install_id"], str) and payload["install_id"]


def test_report_once_custom_url_registers_even_with_persisted_hosted_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    assert pt.persist_token("hosted-token")

    poster = RecordingPoster(ok=True)
    registrar_calls = []

    def registrar(url, install_id):
        registrar_calls.append((url, install_id))
        return "custom-install-token"

    env = {pt.URL_ENV: "https://custom.example.com/ingest"}
    result = pt.report_once(env=env, brain=FakeBrain(), poster=poster, registrar=registrar)

    assert result["status"] == "sent"
    assert len(registrar_calls) == 1
    assert registrar_calls[0][0] == "https://custom.example.com/ingest"
    assert poster.calls and poster.calls[0][0] == "https://custom.example.com/ingest"


def test_report_once_custom_url_falls_back_when_registration_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    poster = RecordingPoster(ok=True)
    registrar_calls = []

    def registrar(url, install_id):
        registrar_calls.append((url, install_id))
        return None

    result = pt.report_once(
        env={pt.URL_ENV: "https://custom.example.com/ingest"},
        brain=FakeBrain(),
        poster=poster,
        registrar=registrar,
    )

    assert result["status"] == "sent"
    assert result["sent"] is True
    assert result["registration"] == "failed"
    assert len(registrar_calls) == 1
    assert poster.calls and poster.calls[0][0] == "https://custom.example.com/ingest"


def test_report_once_hosted_registration_failure_does_not_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    poster = RecordingPoster(ok=True)

    def registrar(_url, _install_id):
        return None

    result = pt.report_once(env={}, brain=FakeBrain(), poster=poster, registrar=registrar)

    assert result == {"status": "failed", "sent": False, "registration": "failed"}
    assert poster.calls == []


def test_report_once_default_url_registers_even_with_persisted_custom_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    assert pt.persist_token(
        "custom-token",
        endpoint="https://custom.example.com/ingest",
    )

    poster = RecordingPoster(ok=True)
    registrar_calls = []

    def registrar(url, install_id):
        registrar_calls.append((url, install_id))
        return "hosted-install-token"

    result = pt.report_once(env={}, brain=FakeBrain(), poster=poster, registrar=registrar)

    assert result["status"] == "sent"
    assert len(registrar_calls) == 1
    assert registrar_calls[0][0] == pt.DEFAULT_INGEST_URL
    assert poster.calls and poster.calls[0][0] == pt.DEFAULT_INGEST_URL


def test_report_once_custom_url_uses_explicit_shared_token_without_registration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    poster = RecordingPoster(ok=True)

    def registrar(url, install_id):
        raise AssertionError("custom URL with explicit shared token should not register")

    env = {pt.URL_ENV: "https://custom.example.com/ingest", pt.TOKEN_ENV: "shared-token"}
    result = pt.report_once(env=env, brain=FakeBrain(), poster=poster, registrar=registrar)

    assert result["status"] == "sent"
    assert poster.calls and poster.calls[0][0] == "https://custom.example.com/ingest"


def test_report_once_failed_post_returns_failed_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    brain = FakeBrain(prs=[FakePR("merged")], touches=[FakeTouch()])
    poster = RecordingPoster(ok=False)
    env = {pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"}

    result = pt.report_once(env=env, brain=brain, poster=poster, now=FIXED)
    assert result["status"] == "failed"
    assert result["sent"] is False


def test_report_once_is_fail_soft_on_poster_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    brain = FakeBrain(prs=[FakePR("merged")], touches=[FakeTouch()])
    env = {pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"}

    # poster raises; report_once must swallow it and report failure/error.
    result = pt.report_once(env=env, brain=brain, poster=boom_poster, now=FIXED)
    assert result["sent"] is False
    assert result["status"] in {"failed", "error"}


def test_report_once_is_fail_soft_on_brain_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    # Brain raises on local reads; report_once must not overwrite a previously
    # accepted non-zero report with fallback zeroes.
    brain = FakeBrain(raise_on={"prs", "touches"})
    poster = RecordingPoster(ok=True)
    env = {pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"}

    result = pt.report_once(env=env, brain=brain, poster=poster, now=FIXED)
    assert result["status"] == "stale_counts"
    assert result["sent"] is False
    assert poster.calls == []


# ---------------------------------------------------------------------------
# derive_counts
# ---------------------------------------------------------------------------
def test_derive_counts_maps_states_correctly():
    brain = FakeBrain(
        prs=[
            FakePR("open"),
            FakePR("merged"),
            FakePR("merged"),
            FakePR("closed"),
            FakePR("unknown"),
        ],
        issues=[
            FakeIssue(),
            FakeIssue("closed", labels=["agent:bundle:billing"]),
            FakeIssue("closed", labels=["help wanted"]),
        ],
        touches=[FakeTouch()] * 7,
    )
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 5
    assert counts.prs_merged == 2
    assert counts.prs_reviewed == 3  # 2 merged + 1 closed
    assert counts.issues_opened == 2
    assert counts.issues_closed == 1
    assert counts.files_changed == 7
    assert counts.lines_changed == 0
    assert counts.loc_added == 7


def test_derive_counts_clamps_to_max():
    big = [FakePR("merged")] * (pt._MAX_PER_FIELD + 50)
    brain = FakeBrain(prs=big, touches=[])
    counts = pt.derive_counts(brain)
    # Counting paginates up to the hard limit and the clamp helper caps the
    # field; a field never exceeds the bound no matter how many rows the brain
    # holds.
    assert counts.prs_merged <= pt._MAX_PER_FIELD
    assert counts.prs_opened == pt._MAX_PER_FIELD


def test_derive_counts_does_not_silently_cap_at_500():
    # A busy install with more than 500 PRs must report the true total, not 500.
    # This is the regression guard for finding #4: the brain models the REAL
    # FleetBrain (list_* CLAMPS to 500), and derive_counts must still report the
    # true total by using the exact count_* path. The old paginate-the-list
    # approach would have frozen every total at 500 here.
    prs = [FakePR("merged")] * 700 + [FakePR("open")] * 200  # 900 total, 700 merged
    touches = [FakeTouch()] * 612
    brain = ClampingBrain(prs=prs, touches=touches)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 900, "must count past the real 500 list cap via count_*"
    assert counts.prs_merged == 700
    assert counts.prs_reviewed == 700  # 700 merged + 0 closed
    assert counts.loc_added == 612


def test_derive_counts_uses_exact_count_methods_over_list():
    # Prove count_* (not list_*) is the source of truth: if the list were used it
    # would clamp at 500, but count_github_items returns the exact total.
    prs = [FakePR("open")] * 1234
    touches = [FakeTouch()] * 999
    brain = ClampingBrain(prs=prs, touches=touches)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 1234
    assert counts.loc_added == 999


def test_derive_counts_uses_agent_authored_github_line_totals():
    brain = ClampingBrain(
        prs=[
            FakePR("merged", additions=10, deletions=3),
            FakePR("open", additions=7, deletions=1),
            FakePR("merged", authored=False, additions=999, deletions=999),
        ],
        touches=[FakeTouch()] * 2,
    )
    counts = pt.derive_counts(brain)
    assert counts.lines_changed == 21


def test_derive_counts_prefers_agent_authored_github_file_totals():
    brain = GitHubFileCountsBrain(
        prs=[
            FakePR("merged", changed_files=4),
            FakePR("open", changed_files=2),
            FakePR("merged", authored=False, changed_files=200),
        ],
        touches=[FakeTouch()] * 999,
    )

    counts = pt.derive_counts(brain)

    assert counts.files_changed == 4
    assert counts.loc_added == 4


def test_derive_counts_falls_back_to_file_touches_when_github_file_totals_are_empty():
    brain = GitHubFileCountsBrain(
        prs=[FakePR("merged", changed_files=0)],
        touches=[FakeTouch()] * 7,
    )

    counts = pt.derive_counts(brain)

    assert counts.files_changed == 7
    assert counts.loc_added == 7


def test_derive_window_counts_prefers_agent_authored_github_file_totals():
    now = datetime(2026, 6, 21, tzinfo=UTC)
    brain = GitHubFileCountsBrain(
        prs=[
            FakePR(
                "merged",
                changed_files=4,
                merged_at=datetime(2026, 6, 10, tzinfo=UTC),
            ),
            FakePR(
                "merged",
                changed_files=9,
                merged_at=datetime(2026, 4, 10, tzinfo=UTC),
            ),
            FakePR(
                "merged",
                authored=False,
                changed_files=200,
                merged_at=datetime(2026, 6, 10, tzinfo=UTC),
            ),
        ],
        touches=[FakeTouch()] * 999,
    )

    counts = pt.derive_window_counts(brain, now=now)

    assert counts.files_changed == 4


def test_derive_window_counts_falls_back_to_file_touches_when_github_files_are_empty():
    now = datetime(2026, 6, 21, tzinfo=UTC)
    brain = GitHubFileCountsBrain(
        prs=[
            FakePR(
                "merged",
                changed_files=0,
                merged_at=datetime(2026, 6, 10, tzinfo=UTC),
            ),
        ],
        touches=[
            FakeTouch(touched_at=datetime(2026, 6, 12, tzinfo=UTC)),
            FakeTouch(touched_at=datetime(2026, 4, 12, tzinfo=UTC)),
        ],
    )

    counts = pt.derive_window_counts(brain, now=now)

    assert counts.files_changed == 1


def test_derive_counts_marks_line_field_stale_when_line_total_query_fails():
    class MissingLineColumnsBrain(ClampingBrain):
        def sum_github_changed_lines(self, **_filters):
            raise RuntimeError("no such column: additions")

    brain = MissingLineColumnsBrain(
        prs=[FakePR("merged"), FakePR("open")],
        issues=[FakeIssue("closed", labels=["agent:authored"])],
        touches=[FakeTouch()] * 3,
    )
    counts = pt.derive_counts(brain)

    assert counts.prs_opened == 2
    assert counts.prs_merged == 1
    assert counts.issues_opened == 1
    assert counts.issues_closed == 1
    assert counts.files_changed == 3
    assert counts.lines_changed == 0
    assert counts.read_complete is True
    assert counts.stale_fields == ("lines_changed",)


def test_derive_counts_marks_line_field_stale_when_authored_prs_have_default_zero_lines():
    brain = ClampingBrain(
        prs=[FakePR("merged", additions=0, deletions=0)],
        touches=[FakeTouch()] * 2,
    )

    counts = pt.derive_counts(brain)

    assert counts.prs_opened == 1
    assert counts.prs_merged == 1
    assert counts.files_changed == 2
    assert counts.lines_changed == 0
    assert counts.read_complete is True
    assert counts.stale_fields == ("lines_changed",)


def test_derive_counts_marks_line_field_stale_when_rolling_lines_are_missing():
    class MissingRollingLineBrain(GitHubFileCountsBrain):
        def sum_github_changed_lines(self, **filters):
            if filters.get("merged_since") is not None:
                return 0
            return super().sum_github_changed_lines(**filters)

    now = datetime(2026, 6, 21, tzinfo=UTC)
    brain = MissingRollingLineBrain(
        prs=[
            FakePR(
                "merged",
                additions=100,
                deletions=20,
                changed_files=3,
                merged_at=datetime(2026, 6, 18, tzinfo=UTC),
            )
        ],
    )

    counts = pt.derive_counts(brain, now=now)

    assert counts.lines_changed == 120
    assert counts.last_30_days is not None
    assert counts.last_30_days.prs_merged == 1
    assert counts.last_30_days.files_changed == 3
    assert counts.last_30_days.lines_changed == 0
    assert counts.stale_fields == ("lines_changed",)


def test_report_once_does_not_zero_lines_when_line_total_query_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))

    class BrokenLineCountsBrain(ClampingBrain):
        def sum_github_changed_lines(self, **_filters):
            raise RuntimeError("temporary read error")

    brain = BrokenLineCountsBrain(prs=[FakePR("merged")], touches=[FakeTouch()])
    poster = RecordingPoster(ok=True)
    result = pt.report_once(
        env={pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"},
        brain=brain,
        poster=poster,
        now=FIXED,
    )

    assert result["status"] == "sent"
    assert result["sent"] is True
    assert poster.calls
    assert poster.calls[0][1]["stale_fields"] == ["lines_changed"]
    assert poster.calls[0][1]["lines_changed"] == 0


def test_derive_counts_fallback_stops_honestly_at_list_clamp():
    # An older brain with NO count_* method and a list that clamps at 500: the
    # paginating fallback cannot see past the clamp, so it reports the clamp (500)
    # rather than looping forever or fabricating a number. This documents the true
    # max for brains that predate the exact-count methods.
    prs = [FakePR("open")] * 900
    touches = [FakeTouch()] * 700
    brain = ClampingNoCountBrain(prs=prs, touches=touches)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == ClampingNoCountBrain.LIST_CAP
    assert counts.loc_added == ClampingNoCountBrain.LIST_CAP


def test_derive_counts_suppresses_dependents_when_base_query_fails():
    # Greptile finding #5: when the base (no-state) PR query raises, prs_opened
    # is 0 AND the dependent merged/reviewed counts MUST be suppressed, even
    # though the state-filtered queries would succeed. We must never emit the
    # impossible prs_opened:0 with prs_merged:N.
    prs = [FakePR("merged")] * 5 + [FakePR("closed")] * 3 + [FakePR("open")] * 2
    brain = FailingBaseBrain(prs=prs, touches=[FakeTouch()] * 4)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 0, "base query failed, so opened is 0"
    assert counts.prs_merged == 0, "dependent count must be suppressed on base failure"
    assert counts.prs_reviewed == 0, "dependent count must be suppressed on base failure"
    # An independent field (file touches) is unaffected by the PR failure.
    assert counts.loc_added == 4
    assert counts.read_complete is False


def test_derive_counts_real_zero_is_distinct_from_failure():
    # A brain that genuinely holds zero PRs reports all-zero WITHOUT the failure
    # path: the distinction matters only so a FAILED base query suppresses
    # dependents; a real zero already yields zero dependents naturally.
    brain = ClampingBrain(prs=[], touches=[FakeTouch()] * 2)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 0
    assert counts.prs_merged == 0
    assert counts.prs_reviewed == 0
    assert counts.loc_added == 2
    assert counts.read_complete is True


def test_derive_counts_falls_back_when_brain_has_no_state_kwarg():
    # An older brain whose list_github_items takes no `state` kwarg must still
    # count correctly via the in-memory tally fallback, without raising.
    prs = [
        FakePR("open"),
        FakePR("merged"),
        FakePR("merged"),
        FakePR("closed"),
    ]
    brain = NoStateBrain(prs=prs, touches=[FakeTouch()] * 3)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 4
    assert counts.prs_merged == 2
    assert counts.prs_reviewed == 3
    assert counts.loc_added == 3


# ---------------------------------------------------------------------------
# derive_counts: count ONLY Alfred-authored PRs (Codex finding #2)
# ---------------------------------------------------------------------------
def test_row_is_agent_authored_by_label_and_branch():
    # The authorship predicate matches the agent:authored label OR an agent
    # branch prefix on head_ref, and matches neither for an operator PR.
    assert pt._row_is_agent_authored(FakePR("open")) is True  # default: authored label
    assert pt._row_is_agent_authored(FakePR("open", authored=False)) is False
    assert (
        pt._row_is_agent_authored(FakePR("merged", labels=[], head_ref="lucius/fix-thing")) is True
    )
    assert (
        pt._row_is_agent_authored(FakePR("merged", labels=[], head_ref="feature/by-a-human"))
        is False
    )
    assert pt._row_is_agent_authored(FakePR("merged", labels=["bug"], head_ref="batman/x")) is True


def test_derive_counts_counts_only_authored_prs_real_brain():
    # The poller caches EVERY PR (gh pr list), including operator- and bot-opened
    # ones. The proof counter must count only agent-authored PRs. This brain
    # models the REAL FleetBrain (count_github_items(authored_only=...) filters in
    # SQL), so the totals must exclude the non-authored rows.
    prs = (
        [FakePR("merged")] * 3  # authored, merged
        + [FakePR("closed")] * 2  # authored, closed
        + [FakePR("open")] * 1  # authored, open
        + [FakePR("merged", authored=False)] * 4  # NOT authored, must not count
        + [FakePR("open", authored=False)] * 5  # NOT authored, must not count
    )
    brain = ClampingBrain(prs=prs, touches=[FakeTouch()] * 2)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 6, "only the 6 agent-authored PRs are counted"
    assert counts.prs_merged == 3, "only authored+merged counted, not the 4 non-authored merged"
    assert counts.prs_reviewed == 5, "authored terminal: 3 merged + 2 closed"
    assert counts.loc_added == 2


def test_derive_counts_counts_only_authored_prs_branch_signal():
    # Authorship via the agent BRANCH PREFIX alone (no label), against the real
    # brain. Rows on a human branch with no agent label must not be counted.
    prs = [
        FakePR("merged", labels=[], head_ref="lucius/a"),
        FakePR("merged", labels=[], head_ref="robin/b"),
        FakePR("open", labels=[], head_ref="rasalghul/c"),
        FakePR("merged", labels=[], head_ref="feature/human-1"),  # not authored
        FakePR("open", labels=[], head_ref=None),  # not authored
    ]
    brain = ClampingBrain(prs=prs, touches=[])
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 3
    assert counts.prs_merged == 2
    assert counts.prs_reviewed == 2  # 2 merged + 0 closed, all authored


def test_derive_counts_counts_only_authored_prs_list_fallback():
    # An older brain with NO count_* method: the list-fallback path must STILL
    # filter to agent-authored rows, so a poller that cached operator PRs does not
    # inflate the counter even on a brain that predates the SQL authored filter.
    prs = (
        [FakePR("open")] * 4  # authored
        + [FakePR("open", authored=False)] * 6  # not authored
    )
    brain = ClampingNoCountBrain(prs=prs, touches=[FakeTouch()] * 3)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 4, "list fallback still excludes non-authored PRs"
    assert counts.loc_added == 3


def test_derive_counts_no_state_kwarg_brain_filters_authored():
    # The no-state-kwarg in-memory tally fallback must also exclude non-authored
    # PRs so prs_merged/prs_reviewed are subsets of the authored opened total.
    prs = [
        FakePR("merged"),  # authored
        FakePR("closed"),  # authored
        FakePR("open"),  # authored
        FakePR("merged", authored=False),  # not authored
        FakePR("closed", authored=False),  # not authored
    ]
    brain = NoStateBrain(prs=prs, touches=[FakeTouch()] * 2)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 3, "only authored PRs counted via the tally fallback"
    assert counts.prs_merged == 1
    assert counts.prs_reviewed == 2  # 1 merged + 1 closed, authored only


# ---------------------------------------------------------------------------
# _count_rows pagination must not stop early on a filtered short page
# (Codex finding #3)
# ---------------------------------------------------------------------------
def test_count_rows_keys_continuation_on_raw_page_not_filtered_matches():
    # Direct test of _count_rows with a predicate. The lister returns a FULL page
    # of raw rows but only HALF match the predicate, so the matched count on the
    # first page (page/2) is far below `limit`. The old code stopped on the
    # post-filter count and would undercount; the fix pages on the raw count.
    page = pt._COUNT_PAGE
    total_rows = page * 2 + 10  # spans 3 pages of raw fetches
    # Alternate authored / not-authored, so every page is ~half matches.
    rows = []
    for i in range(total_rows):
        rows.append(FakePR("open") if i % 2 == 0 else FakePR("open", authored=False))
    expected_authored = sum(1 for r in rows if pt._row_is_agent_authored(r))

    def lister(limit):
        return rows[:limit]

    counted = pt._count_rows(lister, pt._row_is_agent_authored)
    assert counted == expected_authored, (
        "must page on the raw fetch count, not the filtered match count, "
        "so a half-filtered first page does not stop the count early"
    )


def test_count_rows_stops_sparse_predicate_at_raw_hard_cap():
    calls: list[int] = []

    def lister(limit: int):
        calls.append(limit)
        return [FakePR("open", authored=False)] * limit

    total = pt._count_rows(lister, pt._row_is_agent_authored)

    assert total == 0
    assert calls[-1] == pt._COUNT_HARD_LIMIT
    assert all(limit <= pt._COUNT_HARD_LIMIT for limit in calls)


def test_derive_counts_no_early_stop_when_filter_removes_rows_list_fallback():
    # End-to-end via derive_counts. A brain with NO count_* method holds more than
    # one page of PRs, half authored and half operator-opened. The list fallback
    # must count EVERY authored PR, not stop after the first under-full filtered
    # page. Mirrors the finding's 1200-PR (600 authored / 600 operator) scenario,
    # scaled to the page size so the test is fast.
    page = pt._COUNT_PAGE
    authored = [FakePR("merged")] * page  # one full page of authored, merged PRs
    operator = [FakePR("merged", authored=False)] * page  # one page of operator PRs
    # Interleave so neither a raw page nor a filtered page lines up with a clean
    # boundary; the count must still reach every authored row.
    prs = []
    for a, o in zip(authored, operator, strict=True):
        prs.append(a)
        prs.append(o)
    brain = PagingNoCountBrain(prs=prs, touches=[])
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == page, (
        f"all {page} authored PRs counted; the operator rows must not cause an "
        "early stop in the paginating list fallback"
    )
    assert counts.prs_merged == page, "authored+merged subset fully counted"
    assert counts.prs_reviewed == page, "authored terminal subset fully counted"


# ---------------------------------------------------------------------------
# subset invariant holds even when prs_opened == 0 (Codex finding #2)
# ---------------------------------------------------------------------------
class OpenedZeroRaceBrain:
    """Brain that races the GitHub poller: the base (authored, no-state) count
    returns 0, yet the state-filtered counts still see merged/closed rows.

    This reproduces the opened==0 / merged>0 window: the base count is taken
    before the poller has written the open rows (or an older brain returns
    state-filtered counts after the base came back 0), so prs_opened is a genuine
    0 (the base query did NOT fail) while the dependents are non-zero. The subset
    clamp must still pull merged/reviewed down to 0, never storing the impossible
    prs_opened:0 with prs_merged:N.
    """

    def __init__(self, *, merged=0, closed=0):
        self._merged = merged
        self._closed = closed

    def count_github_items(
        self,
        *,
        kind=None,
        state=None,
        authored_only=False,
        agent_labeled_only=False,
    ):
        assert kind == "pr"
        if state is None:
            return 0  # the base "all authored PRs" count races to 0
        if state == "merged":
            return self._merged
        if state == "closed":
            return self._closed
        return 0

    def count_file_touches(self):
        return 0


def test_derive_counts_clamps_dependents_to_zero_when_opened_is_zero():
    # The base count is a real 0 (no failure), but a racing state-filtered count
    # still returns merged/closed rows. The subset invariant (merged, reviewed <=
    # opened) must be enforced EVEN at opened==0, so the payload never ships
    # prs_opened:0 alongside prs_merged>0.
    brain = OpenedZeroRaceBrain(merged=3, closed=2)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 0, "base count raced to zero (no failure)"
    assert counts.prs_merged == 0, "merged must be clamped to opened even when opened is 0"
    assert counts.prs_reviewed == 0, "reviewed must be clamped to opened even when opened is 0"


class ClosedIssueRaceBrain:
    """Brain that reports closed agent issues during an opened-count race."""

    def count_github_items(
        self,
        *,
        kind=None,
        state=None,
        authored_only=False,
        agent_labeled_only=False,
    ):
        if kind == "issue":
            if state is None:
                return 0
            if state == "closed":
                return 5
        return 0

    def count_file_touches(self):
        return 0


def test_derive_counts_clamps_closed_issues_to_opened_issues():
    brain = ClosedIssueRaceBrain()
    counts = pt.derive_counts(brain)
    assert counts.issues_opened == 0
    assert counts.issues_closed == 0


# ---------------------------------------------------------------------------
# current_period: stable lifetime bucket (no calendar dependence)
# ---------------------------------------------------------------------------
def test_current_period_is_stable_lifetime_bucket():
    # The bucket never depends on the clock, so a calendar rollover cannot make
    # the Worker treat the same cumulative total as a fresh bucket.
    june = pt.current_period(datetime(2026, 6, 15, tzinfo=UTC))
    july = pt.current_period(datetime(2026, 7, 1, tzinfo=UTC))
    assert june == july == "lifetime"
    assert pt.current_period() == "lifetime"


# ---------------------------------------------------------------------------
# build_payload + clamping
# ---------------------------------------------------------------------------
def test_build_payload_clamps_negatives_and_caps():
    counts = pt.TelemetryCounts(
        prs_opened=-5,
        prs_merged=10,
        prs_reviewed=pt._MAX_PER_FIELD + 1,
        issues_opened=11,
        issues_closed=12,
        files_changed=22,
        lines_changed=33,
        loc_added=0,
    )
    payload = pt.build_payload("id-token", counts, "2026-06")
    assert payload["prs_opened"] == 0
    assert payload["prs_merged"] == 10
    assert payload["prs_reviewed"] == pt._MAX_PER_FIELD
    assert payload["issues_opened"] == 11
    assert payload["issues_closed"] == 12
    assert payload["files_changed"] == 22
    assert payload["lines_changed"] == 33
    assert payload["loc_added"] == 0


def test_build_payload_preserves_explicit_zero_files_changed():
    counts = pt.TelemetryCounts(files_changed=0, loc_added=9)
    payload = pt.build_payload("id-token", counts, "lifetime")
    assert payload["files_changed"] == 0
    assert payload["loc_added"] == 9


def test_build_tombstone_payload_contains_no_counts():
    payload = pt.build_tombstone_payload("id-token")
    assert payload == {
        "install_id": "id-token",
        "period": "lifetime",
        "tombstone": True,
    }


# ---------------------------------------------------------------------------
# ingest token (optional shared write gate)
# ---------------------------------------------------------------------------
def test_telemetry_token_reads_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    assert pt.telemetry_token({}) == ""
    assert pt.telemetry_token({pt.TOKEN_ENV: " tok "}) == "tok"


def test_persisted_telemetry_token_is_endpoint_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))

    assert pt.persist_token("custom-token", endpoint="https://custom.example.com/ingest")

    assert pt.telemetry_token({}, endpoint="https://custom.example.com/ingest/") == "custom-token"
    assert pt.telemetry_token({}, endpoint=pt.DEFAULT_INGEST_URL) == ""


def test_legacy_persisted_telemetry_token_is_hosted_default_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))

    assert pt.persist_token("legacy-hosted-token")

    assert pt.telemetry_token({}, endpoint=pt.DEFAULT_INGEST_URL) == "legacy-hosted-token"
    assert pt.telemetry_token({}, endpoint="https://custom.example.com/ingest") == ""


def test_persist_token_removes_token_when_endpoint_marker_write_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    marker = tmp_path / "state" / "telemetry-token-endpoint"
    marker.mkdir(parents=True)

    assert not pt.persist_token("custom-token", endpoint="https://custom.example.com/ingest")

    assert not (tmp_path / "state" / "telemetry-token").exists()


def test_trusted_telemetry_token_reads_env():
    assert pt.trusted_telemetry_token({}) == ""
    assert pt.trusted_telemetry_token({pt.TRUSTED_TOKEN_ENV: " trusted "}) == "trusted"


def test_trusted_telemetry_token_is_scoped_to_hosted_collector():
    env = {pt.TRUSTED_TOKEN_ENV: " trusted "}

    assert pt.trusted_telemetry_token_for_url(pt.DEFAULT_INGEST_URL, env) == "trusted"
    assert pt.trusted_telemetry_token_for_url(f"{pt.DEFAULT_INGEST_URL}/", env) == "trusted"
    assert pt.trusted_telemetry_token_for_url("https://custom.example.com/ingest", env) == ""


def test_register_install_sends_trusted_token_only_to_hosted_collector(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    monkeypatch.setenv(pt.TRUSTED_TOKEN_ENV, "trusted-secret")
    captured = []

    def fake_post_json(url, payload, **kwargs):
        captured.append((url, payload, kwargs.get("trusted_token", "")))
        return True, {"install_id": payload["install_id"], "token": "install-token"}

    monkeypatch.setattr(pt, "_post_json", fake_post_json)

    hosted = pt.register_install(pt.DEFAULT_INGEST_URL, "install-reg-recover")
    custom = pt.register_install("https://custom.example.com/ingest", "install-reg-custom")

    assert hosted == "install-token"
    assert custom == "install-token"
    assert captured == [
        (
            pt.register_url_for_ingest(pt.DEFAULT_INGEST_URL),
            {"install_id": "install-reg-recover"},
            "trusted-secret",
        ),
        (
            "https://custom.example.com/register",
            {"install_id": "install-reg-custom"},
            "",
        ),
    ]


def test_post_sends_token_header_when_set(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return FakeResp()

    monkeypatch.setattr(pt.urllib.request, "urlopen", fake_urlopen)
    ok = pt._post("https://w.example.com/ingest", {"x": 1}, token="s3cr3t")
    assert ok is True
    # urllib title-cases header keys.
    assert captured["headers"].get("X-ingest-token") == "s3cr3t"


def test_post_accepts_plain_2xx_body(monkeypatch):
    class FakeResp:
        status = 202

        def read(self):
            return b"OK"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pt.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())

    assert pt._post("https://w.example.com/ingest", {"x": 1}) is True


def test_post_json_still_requires_json_by_default(monkeypatch):
    class FakeResp:
        status = 200

        def read(self):
            return b"OK"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pt.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())

    ok, body = pt._post_json("https://w.example.com/register", {"install_id": "id"})

    assert ok is False
    assert body == {}


def test_post_sends_trusted_token_header_when_set(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return FakeResp()

    monkeypatch.setattr(pt.urllib.request, "urlopen", fake_urlopen)
    ok = pt._post(
        "https://w.example.com/ingest",
        {"x": 1},
        token="install-token",
        trusted_token="trusted-token",
    )
    assert ok is True
    assert captured["headers"].get("X-ingest-token") == "install-token"
    assert captured["headers"].get("X-alfred-trusted-token") == "trusted-token"


def test_post_omits_token_header_when_unset(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return FakeResp()

    monkeypatch.setattr(pt.urllib.request, "urlopen", fake_urlopen)
    pt._post("https://w.example.com/ingest", {"x": 1})
    assert "X-ingest-token" not in captured["headers"]
    assert "X-alfred-trusted-token" not in captured["headers"]


def test_clear_report_sends_tombstone_with_existing_install_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    install_id_path = tmp_path / "state" / "telemetry-install-id"
    install_id_path.parent.mkdir()
    install_id_path.write_text("existing-token\n", encoding="utf-8")
    poster = RecordingPoster(ok=True)
    registrar_calls = []

    def registrar(url, install_id):
        registrar_calls.append((url, install_id))
        return "delete-token"

    result = pt.clear_report(
        env={pt.URL_ENV: "https://telemetry.example.com/ingest"},
        poster=poster,
        registrar=registrar,
    )

    assert result == {"status": "sent", "sent": True}
    assert registrar_calls == [("https://telemetry.example.com/ingest", "existing-token")]
    assert poster.calls == [
        (
            "https://telemetry.example.com/ingest",
            {
                "install_id": "existing-token",
                "period": "lifetime",
                "tombstone": True,
            },
        )
    ]


def test_clear_report_custom_url_falls_back_when_registration_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    install_id_path = tmp_path / "state" / "telemetry-install-id"
    install_id_path.parent.mkdir()
    install_id_path.write_text("existing-token\n", encoding="utf-8")
    poster = RecordingPoster(ok=True)
    registrar_calls = []

    def registrar(url, install_id):
        registrar_calls.append((url, install_id))
        return None

    result = pt.clear_report(
        env={pt.URL_ENV: "https://custom.example.com/ingest"},
        poster=poster,
        registrar=registrar,
    )

    assert result == {"status": "sent", "sent": True}
    assert registrar_calls == [("https://custom.example.com/ingest", "existing-token")]
    assert poster.calls and poster.calls[0][0] == "https://custom.example.com/ingest"


def test_clear_report_hosted_registration_failure_returns_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    install_id_path = tmp_path / "state" / "telemetry-install-id"
    install_id_path.parent.mkdir()
    install_id_path.write_text("existing-token\n", encoding="utf-8")
    poster = RecordingPoster(ok=True)

    def registrar(_url, _install_id):
        return None

    result = pt.clear_report(env={}, poster=poster, registrar=registrar)

    assert result == {"status": "no_token", "sent": False}
    assert poster.calls == []


def test_clear_report_does_not_create_install_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    poster = RecordingPoster(ok=True)

    result = pt.clear_report(
        env={pt.URL_ENV: "https://telemetry.example.com/ingest"},
        poster=poster,
    )

    assert result == {"status": "no_install_id", "sent": False}
    assert poster.calls == []
    assert not (tmp_path / "state" / "telemetry-install-id").exists()


# ---------------------------------------------------------------------------
# install id
# ---------------------------------------------------------------------------
def test_install_id_is_stable_across_calls(tmp_path):
    path = tmp_path / "state" / "telemetry-install-id"
    first = pt.load_or_create_install_id(path)
    second = pt.load_or_create_install_id(path)
    assert first == second
    assert path.exists()
    assert len(first) >= 16


def test_install_id_regenerates_when_file_missing(tmp_path):
    path = tmp_path / "state" / "telemetry-install-id"
    first = pt.load_or_create_install_id(path)
    path.unlink()
    second = pt.load_or_create_install_id(path)
    assert first != second  # new random token


def test_install_id_is_not_derived_from_host(tmp_path):
    # Two distinct paths -> two distinct random ids, proving the id is random
    # rather than a deterministic function of the host.
    a = pt.load_or_create_install_id(tmp_path / "a")
    b = pt.load_or_create_install_id(tmp_path / "b")
    assert a != b


# ---------------------------------------------------------------------------
# persisted-only install id: skip reporting when the id cannot be persisted
# (Codex finding #1: never report with an unpersisted, ephemeral id)
# ---------------------------------------------------------------------------
def test_persisted_install_id_returns_none_when_unwritable(tmp_path, monkeypatch):
    # When the install-id file cannot be read OR written, the persisted-only
    # loader returns None rather than minting an ephemeral token.
    #
    # We force the write to fail by monkeypatching Path.write_text to raise
    # OSError. This is robust on ANY uid: a chmod(0o500) directory does not stop
    # root (root bypasses file permission bits in containers/CI), so it would not
    # exercise the no-id branch when the suite runs as root. Raising directly from
    # the write call deterministically forces the "cannot persist" path.
    target = tmp_path / "ro" / "telemetry-install-id"

    def _boom(self, *args, **kwargs):
        raise OSError("write blocked for test")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = pt.load_or_create_persisted_install_id(target)
    assert result is None, "an unpersistable id must be None, not an ephemeral token"


def test_persisted_install_id_reads_back_existing(tmp_path):
    path = tmp_path / "state" / "telemetry-install-id"
    first = pt.load_or_create_persisted_install_id(path)
    assert first is not None and path.exists()
    second = pt.load_or_create_persisted_install_id(path)
    assert first == second, "a persisted id is stable across calls"


def test_report_once_skips_when_install_id_cannot_persist(monkeypatch):
    # The reporter must NOT POST when the install id cannot be persisted: an
    # ephemeral id would look like a new install every run and inflate the
    # distinct-install count. It returns status no_install_id and never calls the
    # network.
    monkeypatch.setattr(pt, "load_or_create_persisted_install_id", lambda *a, **k: None)
    poster = RecordingPoster(ok=True)
    brain = FakeBrain(prs=[FakePR("merged")], touches=[FakeTouch()])
    env = {pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"}

    result = pt.report_once(env=env, brain=brain, poster=poster, now=FIXED)
    assert result == {"status": "no_install_id", "sent": False}
    assert poster.calls == [], "no POST when the install id is not persisted"


def test_report_once_does_not_mint_fresh_id_per_call_on_persist_failure(tmp_path, monkeypatch):
    # End-to-end against an unwritable state dir: the reporter must skip rather
    # than mint a fresh id on each call. We assert no id file is created and no
    # POST happens across repeated calls (the Worker would otherwise see N new
    # installs from one host).
    #
    # The write failure is simulated by monkeypatching Path.write_text to raise
    # OSError rather than by chmod(0o500): root bypasses permission bits, so a
    # chmod-based denial would silently keep the dir writable under root-based CI
    # and the no-id branch would never run. Raising on write forces the persist
    # failure deterministically on any uid.
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("ALFRED_HOME", str(home))

    def _boom(self, *args, **kwargs):
        raise OSError("write blocked for test")

    monkeypatch.setattr(Path, "write_text", _boom)
    poster = RecordingPoster(ok=True)
    brain = FakeBrain(prs=[FakePR("merged")], touches=[FakeTouch()])
    env = {pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"}
    r1 = pt.report_once(env=env, brain=brain, poster=poster, now=FIXED)
    r2 = pt.report_once(env=env, brain=brain, poster=poster, now=FIXED)
    assert r1["status"] == "no_install_id" and r1["sent"] is False
    assert r2["status"] == "no_install_id" and r2["sent"] is False
    assert poster.calls == [], "persist failure must never POST an ephemeral id"
    assert not (state / "telemetry-install-id").exists()


# ---------------------------------------------------------------------------
# ALFRED_DOCTOR fast path: bin/doctor.sh runs every agent with ALFRED_DOCTOR=1
# and must get a quick recognized sentinel, never a real telemetry POST.
# ---------------------------------------------------------------------------
def test_doctor_fast_path_explicit_opt_out_emits_disabled_sentinel(monkeypatch, capsys):
    monkeypatch.setenv("ALFRED_DOCTOR", "1")
    monkeypatch.setenv(pt.ENABLE_ENV, "0")
    # If the fast path fell through to report_once, this would blow up loudly.
    monkeypatch.setattr(pt, "report_once", _fail_if_called("report_once must not run under doctor"))
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PROOF-TELEMETRY-DISABLED]" in out
    # doctor.sh classifies any ``*-DISABLED`` sentinel as "⚪ disabled" (healthy).
    assert "DISABLED]" in out


def test_doctor_fast_path_enabled_without_url_uses_default_collector(monkeypatch, capsys):
    monkeypatch.setenv("ALFRED_DOCTOR", "1")
    monkeypatch.delenv(pt.ENABLE_ENV, raising=False)
    monkeypatch.delenv(pt.URL_ENV, raising=False)
    monkeypatch.delenv(pt.DEFAULT_URL_ENV, raising=False)
    monkeypatch.setattr(pt, "report_once", _fail_if_called("report_once must not run under doctor"))
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PROOF-TELEMETRY-DOCTOR-OK]" in out


def test_doctor_fast_path_enabled_with_default_disabled_emits_no_url(monkeypatch, capsys):
    monkeypatch.setenv("ALFRED_DOCTOR", "1")
    monkeypatch.delenv(pt.ENABLE_ENV, raising=False)
    monkeypatch.delenv(pt.URL_ENV, raising=False)
    monkeypatch.setenv(pt.DEFAULT_URL_ENV, "")
    monkeypatch.setattr(pt, "report_once", _fail_if_called("report_once must not run under doctor"))
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PROOF-TELEMETRY-NO-URL]" in out


def test_doctor_fast_path_enabled_with_url_emits_doctor_ok_without_posting(monkeypatch, capsys):
    monkeypatch.setenv("ALFRED_DOCTOR", "1")
    monkeypatch.delenv(pt.ENABLE_ENV, raising=False)
    monkeypatch.setenv(pt.URL_ENV, "https://telemetry.example.com/ingest")
    # The whole point: an ENABLED install under a health check must NOT report.
    monkeypatch.setattr(pt, "report_once", _fail_if_called("doctor must never trigger a report"))
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PROOF-TELEMETRY-DOCTOR-OK]" in out
    # doctor.sh greps for ``DOCTOR-OK`` to mark the agent ✅ ok.
    assert "DOCTOR-OK" in out


def test_doctor_fast_path_takes_precedence_over_dry_run(monkeypatch, capsys):
    # Even with --dry-run, ALFRED_DOCTOR=1 short-circuits to the fast path so a
    # doctor sweep never builds a payload or reads the brain.
    monkeypatch.setenv("ALFRED_DOCTOR", "1")
    monkeypatch.delenv(pt.ENABLE_ENV, raising=False)
    monkeypatch.setenv(pt.DEFAULT_URL_ENV, "")
    rc = cli.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PROOF-TELEMETRY-NO-URL]" in out
    assert "DRY-RUN" not in out


def _fail_if_called(message):
    def _boom(*args, **kwargs):
        raise AssertionError(message)

    return _boom
