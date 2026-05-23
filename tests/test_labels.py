"""Tests for ``lib/labels.py`` — label constants and the transition table."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add lib/ to sys.path so the test imports the public module directly.
LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))

import labels  # noqa: E402


def test_lifecycle_label_strings_are_stable():
    """Locking in the wire-level strings — these are GitHub-side labels."""
    assert labels.IMPLEMENT == "agent:implement"
    assert labels.IN_FLIGHT == "agent:in-flight"
    assert labels.PR_OPEN == "agent:pr-open"
    assert labels.DONE == "agent:done"


def test_sticky_label_strings_are_stable():
    assert labels.DO_NOT_PICKUP == "do-not-pickup"
    assert labels.NEEDS_HUMAN_SCOPE == "needs:human-scope"


def test_bundle_label_helpers_round_trip():
    label = labels.bundle_label("oauth-rollout")
    assert label == "agent:bundle:oauth-rollout"
    assert labels.is_bundle_label(label)
    assert labels.bundle_slug(label) == "oauth-rollout"


def test_bundle_label_rejects_empty_and_whitespace():
    with pytest.raises(ValueError):
        labels.bundle_label("")
    with pytest.raises(ValueError):
        labels.bundle_label("has space")


def test_is_bundle_label_negatives():
    assert not labels.is_bundle_label("agent:bundle:")  # prefix only
    assert not labels.is_bundle_label("agent:implement")
    assert not labels.is_bundle_label("agent:bundle")  # missing trailing colon


def test_authored_label_is_stable():
    assert labels.AUTHORED == "agent:authored"


def test_claim_comment_prefixes_are_stable():
    assert labels.CLAIM_COMMENT_PREFIX == "<!-- agent-claim:"
    assert labels.RELEASE_COMMENT_PREFIX == "<!-- agent-release:"


def test_lifecycle_label_set_membership():
    assert labels.IMPLEMENT in labels.LIFECYCLE_LABEL_SET
    assert labels.DO_NOT_PICKUP not in labels.LIFECYCLE_LABEL_SET


def test_lifecycle_state_picks_most_advanced():
    assert labels.lifecycle_state({labels.IMPLEMENT}) == labels.IMPLEMENT
    assert labels.lifecycle_state({labels.IMPLEMENT, labels.IN_FLIGHT}) == labels.IN_FLIGHT
    assert labels.lifecycle_state({labels.IN_FLIGHT, labels.PR_OPEN, labels.DONE}) == labels.DONE
    assert labels.lifecycle_state(set()) is None
    assert labels.lifecycle_state({"random-label"}) is None


def test_has_blocker_detects_each_blocker():
    for blocker in (
        labels.IN_FLIGHT,
        labels.PR_OPEN,
        labels.DO_NOT_PICKUP,
        labels.NEEDS_HUMAN_SCOPE,
    ):
        assert labels.has_blocker({blocker})
    assert not labels.has_blocker({labels.IMPLEMENT})
    assert not labels.has_blocker(set())


def test_bundle_labels_extraction_and_sort():
    s = {
        "random",
        labels.bundle_label("zeta"),
        labels.bundle_label("alpha"),
        labels.IMPLEMENT,
    }
    extracted = labels.bundle_labels(s)
    assert extracted == [
        labels.bundle_label("alpha"),
        labels.bundle_label("zeta"),
    ]


def test_transition_table_documents_known_moves():
    # The doc-level claim: implement -> in-flight, in-flight -> pr-open,
    # in-flight -> implement (release None), pr-open -> done.
    assert labels.is_legal_transition(labels.IMPLEMENT, labels.IN_FLIGHT)
    assert labels.is_legal_transition(labels.IN_FLIGHT, labels.PR_OPEN)
    assert labels.is_legal_transition(labels.IN_FLIGHT, labels.IMPLEMENT)
    assert labels.is_legal_transition(labels.PR_OPEN, labels.DONE)


def test_transition_table_rejects_unknown_moves():
    # An issue can't jump directly from implement to done.
    assert not labels.is_legal_transition(labels.IMPLEMENT, labels.DONE)
    assert not labels.is_legal_transition(labels.DONE, labels.IMPLEMENT)


def test_legal_transitions_for_in_flight_covers_all_exits():
    src = labels.IN_FLIGHT
    dsts = {t.dst for t in labels.legal_transitions(src)}
    assert labels.IMPLEMENT in dsts  # release / sweep / race-yield
    assert labels.PR_OPEN in dsts


def test_all_transitions_returned_as_tuple():
    all_t = labels.all_transitions()
    assert isinstance(all_t, tuple)
    assert len(all_t) >= 8  # rough lower bound — we documented ~9


def test_lifecycle_label_defs_have_all_required_names():
    names = {d.name for d in labels.LIFECYCLE_LABEL_DEFS}
    assert labels.IN_FLIGHT in names
    assert labels.PR_OPEN in names
    assert labels.DONE in names
    assert labels.DO_NOT_PICKUP in names
    assert labels.NEEDS_HUMAN_SCOPE in names
    assert labels.LARGE_FEATURE in names
    assert labels.AUTHORED in names


def test_lifecycle_label_defs_have_six_char_hex_colors():
    for d in labels.LIFECYCLE_LABEL_DEFS:
        assert len(d.color) == 6, f"{d.name}: bad color {d.color!r}"
        int(d.color, 16)  # must parse as hex


def test_lifecycle_labels_tuples_backcompat_shape():
    # Back-compat with agent_runner.LIFECYCLE_LABELS shape:
    # tuple of (name, color, description).
    sample = labels.LIFECYCLE_LABELS_TUPLES[0]
    assert isinstance(sample, tuple)
    assert len(sample) == 3
    assert all(isinstance(x, str) for x in sample)


def test_label_state_config_from_env_parses_csv():
    cfg = labels.LabelStateConfig.from_env(
        {
            "GH_ORG": "your-org",
            "LABEL_STATE_SWEEP_REPOS": "your-backend, your-frontend ,",
            "ALFRED_HOME": "/tmp/alfred",
        }
    )
    assert cfg.gh_org == "your-org"
    assert cfg.sweep_repos == ("your-backend", "your-frontend")
    assert cfg.alfred_home == "/tmp/alfred"


def test_label_state_config_from_empty_env():
    cfg = labels.LabelStateConfig.from_env({})
    assert cfg.gh_org == ""
    assert cfg.sweep_repos == ()
    assert cfg.alfred_home == ""


def test_label_constants_match_agent_runner_existing_values():
    """Lock in: agent_runner.py predates labels.py; the strings must
    stay byte-identical so claim_issue/release_issue agree with labels.py.
    """
    import agent_runner

    assert agent_runner.CLAIM_COMMENT_PREFIX == labels.CLAIM_COMMENT_PREFIX
    assert agent_runner.RELEASE_COMMENT_PREFIX == labels.RELEASE_COMMENT_PREFIX
    # Lifecycle label string is referenced via literal in agent_runner;
    # confirm our constant matches what the live runner uses.
    runner_names = {row[0] for row in agent_runner.LIFECYCLE_LABELS}
    for required in (
        labels.IN_FLIGHT,
        labels.PR_OPEN,
        labels.DONE,
        labels.DO_NOT_PICKUP,
        labels.NEEDS_HUMAN_SCOPE,
    ):
        assert required in runner_names
