"""Unit tests for the opt-in proof-telemetry reporter.

Covers the privacy-load-bearing guarantees:

* OFF by default: no send, no install-id file, no network when the master
  switch is unset or set to anything other than exactly ``"1"``.
* Correct, bounded payload shape when enabled.
* Fail-soft: a network error never raises; it returns a ``failed`` status.
* Idempotent-friendly install id: stable across calls, regenerated only when
  the file is missing.
* No PII in the payload (only the four counts plus install_id + period).

Nothing here touches the real ``$ALFRED_HOME`` or the network; the brain and
the HTTP poster are injected.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# Make ``lib/`` importable from the repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "lib"))

import proof_telemetry as pt  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakePR:
    def __init__(self, state: str) -> None:
        self.state = state


class FakeTouch:
    pass


class FakeBrain:
    """Stand-in for FleetBrain exposing the two methods derive_counts calls.

    Honors the ``limit`` (returns at most ``limit`` rows, like the real brain's
    top-N list) and the ``state`` filter on ``list_github_items`` so the
    state-based counting path is exercised. ``raise_on`` forces a failure for
    fail-soft tests.
    """

    def __init__(self, prs=None, touches=None, raise_on=None):
        self._prs = prs or []
        self._touches = touches or []
        self._raise_on = raise_on or set()

    def list_github_items(self, *, kind=None, state=None, limit=50):
        if "prs" in self._raise_on:
            raise RuntimeError("brain unavailable")
        assert kind == "pr"
        rows = self._prs
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
    the state-filtered counting path is unavailable.
    """

    def __init__(self, prs=None, touches=None):
        self._prs = prs or []
        self._touches = touches or []

    def list_github_items(self, *, kind=None, limit=50):
        assert kind == "pr"
        return list(self._prs)[:limit]

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
# is_enabled: the master switch
# ---------------------------------------------------------------------------
def test_disabled_when_env_unset():
    assert pt.is_enabled({}) is False


def test_disabled_for_anything_but_exactly_one():
    for value in ["0", "true", "yes", "on", "TRUE", "1 ", " ", "", "2", "10"]:
        env = {pt.ENABLE_ENV: value}
        # "1 " is stripped to "1" and is allowed; everything else is off.
        expected = value.strip() == "1"
        assert pt.is_enabled(env) is expected, f"{value!r} -> {expected}"


def test_enabled_only_for_one():
    assert pt.is_enabled({pt.ENABLE_ENV: "1"}) is True


# ---------------------------------------------------------------------------
# report_once: off-by-default path sends nothing
# ---------------------------------------------------------------------------
def test_report_once_disabled_is_a_no_op():
    poster = RecordingPoster()
    result = pt.report_once(env={}, brain=FakeBrain(), poster=poster)
    assert result == {"status": "disabled", "sent": False}
    assert poster.calls == [], "disabled telemetry must not call the network"


def test_report_once_disabled_does_not_create_install_id(tmp_path, monkeypatch):
    # Point the install-id path at a temp dir and assert nothing is written.
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    poster = RecordingPoster()
    pt.report_once(env={}, brain=FakeBrain(), poster=poster)
    assert not (tmp_path / "state" / "telemetry-install-id").exists()


def test_report_once_enabled_without_url_is_a_no_op():
    poster = RecordingPoster()
    result = pt.report_once(env={pt.ENABLE_ENV: "1"}, brain=FakeBrain(), poster=poster)
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
        touches=[FakeTouch(), FakeTouch(), FakeTouch()],
    )
    poster = RecordingPoster(ok=True)
    env = {pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"}

    result = pt.report_once(env=env, brain=brain, poster=poster, now=FIXED)

    assert result["status"] == "sent"
    assert result["sent"] is True
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
        "loc_added",
    }
    # Period is the stable lifetime bucket, not a calendar month, so a calendar
    # rollover never re-adds the cumulative total on the Worker.
    assert payload["period"] == "lifetime"
    assert payload["prs_opened"] == 4
    assert payload["prs_merged"] == 2
    # reviewed = merged + closed (terminal), never exceeds opened.
    assert payload["prs_reviewed"] == 3
    assert payload["loc_added"] == 3
    assert isinstance(payload["install_id"], str) and payload["install_id"]


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
    # Brain raises on both queries; derive_counts swallows -> zero counts, and
    # the report still sends (zeros are harmless and the server clamps).
    brain = FakeBrain(raise_on={"prs", "touches"})
    poster = RecordingPoster(ok=True)
    env = {pt.ENABLE_ENV: "1", pt.URL_ENV: "https://telemetry.example.com/ingest"}

    result = pt.report_once(env=env, brain=brain, poster=poster, now=FIXED)
    assert result["status"] == "sent"
    _, payload = poster.calls[0]
    assert payload["prs_opened"] == 0
    assert payload["loc_added"] == 0


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
        touches=[FakeTouch()] * 7,
    )
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 5
    assert counts.prs_merged == 2
    assert counts.prs_reviewed == 3  # 2 merged + 1 closed
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
    # This is the regression guard for the old hard limit=500 that froze the
    # lifetime aggregate at 500 on the Worker.
    prs = [FakePR("merged")] * 700 + [FakePR("open")] * 200  # 900 total, 700 merged
    touches = [FakeTouch()] * 612
    brain = FakeBrain(prs=prs, touches=touches)
    counts = pt.derive_counts(brain)
    assert counts.prs_opened == 900, "must count past the old 500 cap"
    assert counts.prs_merged == 700
    assert counts.prs_reviewed == 700  # 700 merged + 0 closed
    assert counts.loc_added == 612


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
        loc_added=0,
    )
    payload = pt.build_payload("id-token", counts, "2026-06")
    assert payload["prs_opened"] == 0
    assert payload["prs_merged"] == 10
    assert payload["prs_reviewed"] == pt._MAX_PER_FIELD
    assert payload["loc_added"] == 0


# ---------------------------------------------------------------------------
# ingest token (optional shared write gate)
# ---------------------------------------------------------------------------
def test_telemetry_token_reads_env():
    assert pt.telemetry_token({}) == ""
    assert pt.telemetry_token({pt.TOKEN_ENV: " tok "}) == "tok"


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
