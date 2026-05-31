from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_issue_bridge import (  # noqa: E402
    BridgeConfig,
    SlackIssueBridge,
    build_issue_body,
    contains_approval_token,
    default_issue_creator,
)
from slack_listener import SlackPlanningListener  # noqa: E402
from slack_thread_registry import SlackThreadRegistry  # noqa: E402

ALLOWED_REPO = "acme-org/api"
OTHER_REPO = "acme-org/web"


class Poster:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True}


class RecordingCreator:
    """Captures gh issue create calls and returns a canned URL."""

    def __init__(self, url: str | None = "https://github.com/acme-org/api/issues/42") -> None:
        self.url = url
        self.calls: list[dict] = []

    def __call__(self, *, repo, title, body, labels):
        self.calls.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return self.url


def _config(**overrides) -> BridgeConfig:
    base = {
        "enabled": True,
        "repos": frozenset({ALLOWED_REPO}),
        "label": "agent:implement",
        "approval_phrases": ("ship it", "create issue", "file issue", "/ship"),
    }
    base.update(overrides)
    return BridgeConfig(**base)  # type: ignore[arg-type]


def _ready_payload(*, repo: str = ALLOWED_REPO, title: str = "Wire the bridge") -> dict:
    return {
        "draft": {"title": title, "repos": [repo]},
        "issue_body": "## Problem\n\nNeed the bridge.",
        "readiness": {"ok": True, "score": 94},
        "questions": [],
    }


def _make_listener(
    tmp_path: Path,
    *,
    creator: RecordingCreator,
    config: BridgeConfig | None = None,
    trusted=("U1",),
) -> tuple[SlackPlanningListener, Poster, SlackThreadRegistry]:
    poster = Poster()
    registry = SlackThreadRegistry(tmp_path / "threads")
    bridge = SlackIssueBridge(config=config or _config(), issue_creator=creator)
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=trusted,
        bridge=bridge,
    )
    return listener, poster, registry


def _seed_draft(
    listener: SlackPlanningListener,
    tmp_path: Path,
    *,
    repo: str = ALLOWED_REPO,
    user: str = "U1",
) -> str:
    """Create a ready planning draft via an app mention; return its draft path."""
    result = listener.handle_payload(
        {
            "event_id": "EvDraftSeed",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": user,
                "text": (
                    "<@UALFRED> title: Wire the Slack issue bridge\n"
                    "problem: Operators need approved drafts to become queued issues safely.\n"
                    f"repo: {repo}\n"
                    "desired: An explicit approval files one labeled GitHub issue.\n"
                    "acceptance: an approved draft produces exactly one labeled issue\n"
                    "test: run the bridge pytest suite\n"
                    "open questions: none"
                ),
                "ts": "1716480010.000001",
            },
        }
    )
    assert result.action == "draft_created"
    assert result.draft_path
    return result.draft_path


def _reply(text: str, *, event_id: str, user: str = "U1") -> dict:
    return {
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": "C1",
            "user": user,
            "text": text,
            "ts": f"1716480011.{event_id}",
            "thread_ts": "1716480010.000001",
        },
    }


def _reaction(name: str, *, event_id: str, user: str = "U1") -> dict:
    return {
        "event_id": event_id,
        "event": {
            "type": "reaction_added",
            "user": user,
            "reaction": name,
            "item": {"type": "message", "channel": "C1", "ts": "1716480010.000001"},
        },
    }


# ---------------------------------------------------------------------------
# Pure approval-token detection
# ---------------------------------------------------------------------------


def test_explicit_phrases_are_recognized() -> None:
    phrases = ("ship it", "create issue", "file issue", "/ship")
    assert contains_approval_token("ship it", phrases)
    assert contains_approval_token("Ship it!", phrases)
    assert contains_approval_token("create issue", phrases)
    assert contains_approval_token("file issue", phrases)
    assert contains_approval_token("/ship", phrases)


def test_ambiguous_text_is_not_approval() -> None:
    phrases = ("ship it", "create issue", "file issue", "/ship")
    # These carry real instructions or prose and must NOT approve.
    assert not contains_approval_token("let's go with two repos", phrases)
    assert not contains_approval_token("go ahead and add repo: acme-org/web", phrases)
    assert not contains_approval_token("can we ship it next week?", phrases)
    assert not contains_approval_token("acceptance: ship it to staging first", phrases)
    assert not contains_approval_token("going to refine this more", phrases)


# ---------------------------------------------------------------------------
# Bridge.convert unit behavior
# ---------------------------------------------------------------------------


def test_convert_refuses_untrusted_user() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(), issue_creator=creator)
    payload = _ready_payload()
    outcome = bridge.convert(payload, trusted=False)
    assert outcome.created is False
    assert outcome.status == "refused_untrusted"
    assert creator.calls == []


def test_convert_disabled_creates_nothing() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(enabled=False), issue_creator=creator)
    payload = _ready_payload()
    outcome = bridge.convert(payload, trusted=True)
    assert outcome.created is False
    assert outcome.status == "disabled"
    assert creator.calls == []


def test_convert_refuses_repo_not_in_allowlist() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(), issue_creator=creator)
    payload = _ready_payload(repo=OTHER_REPO)
    outcome = bridge.convert(payload, trusted=True)
    assert outcome.created is False
    assert outcome.status == "refused_repo_not_allowed"
    assert OTHER_REPO in outcome.detail
    assert creator.calls == []


def test_convert_refuses_when_allowlist_empty() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(repos=frozenset()), issue_creator=creator)
    payload = _ready_payload()
    outcome = bridge.convert(payload, trusted=True)
    assert outcome.created is False
    assert outcome.status == "refused_allowlist_empty"
    assert creator.calls == []


def test_convert_creates_issue_with_repo_and_label() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(), issue_creator=creator)
    payload = _ready_payload()
    outcome = bridge.convert(payload, trusted=True, thread_link="slack://thread?x")
    assert outcome.created is True
    assert outcome.issue_url == "https://github.com/acme-org/api/issues/42"
    assert len(creator.calls) == 1
    call = creator.calls[0]
    assert call["repo"] == ALLOWED_REPO
    assert call["labels"] == ["agent:implement"]
    assert call["title"] == "Wire the bridge"
    assert "Need the bridge." in call["body"]
    assert "slack://thread?x" in call["body"]


def test_convert_accepts_numeric_readiness_score() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(), issue_creator=creator)
    payload = _ready_payload()
    payload["readiness"]["score"] = 94.0

    outcome = bridge.convert(payload, trusted=True)

    assert outcome.created is True
    assert len(creator.calls) == 1


def test_convert_refuses_under_scoped_draft_even_with_approval() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(), issue_creator=creator)
    payload = {
        "draft": {"title": "Make things better", "repos": [ALLOWED_REPO]},
        "issue_body": "## Problem\n\nTODO",
        "readiness": {"ok": False, "score": 34},
        "questions": ["What should be different when this ships?"],
    }
    outcome = bridge.convert(payload, trusted=True)
    assert outcome.created is False
    assert outcome.status == "refused_not_ready"
    assert "34/100" in outcome.detail
    assert "What should be different" in outcome.detail
    assert creator.calls == []


def test_convert_refuses_draft_without_readiness_report() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(), issue_creator=creator)
    payload = {"draft": {"title": "Wire the bridge", "repos": [ALLOWED_REPO]}}
    outcome = bridge.convert(payload, trusted=True)
    assert outcome.created is False
    assert outcome.status == "refused_readiness_missing"
    assert creator.calls == []


def test_convert_idempotent_when_already_converted() -> None:
    creator = RecordingCreator()
    bridge = SlackIssueBridge(config=_config(), issue_creator=creator)
    payload = _ready_payload(title="X") | {
        "bridge": {"converted": True, "issue_url": "https://github.com/acme-org/api/issues/7"}
    }
    outcome = bridge.convert(payload, trusted=True, already_converted=True)
    assert outcome.created is False
    assert outcome.status == "already_converted"
    assert outcome.issue_url == "https://github.com/acme-org/api/issues/7"
    assert creator.calls == []


# ---------------------------------------------------------------------------
# Listener integration
# ---------------------------------------------------------------------------


def test_trusted_explicit_approval_creates_issue(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, poster, registry = _make_listener(tmp_path, creator=creator)
    _seed_draft(listener, tmp_path)

    result = listener.handle_payload(_reply("ship it", event_id="000002"))

    assert result.handled is True
    assert result.action == "issue_created"
    assert len(creator.calls) == 1
    assert creator.calls[0]["repo"] == ALLOWED_REPO
    assert creator.calls[0]["labels"] == ["agent:implement"]
    assert "Issue created" in poster.messages[-1]["text"]
    assert "https://github.com/acme-org/api/issues/42" in poster.messages[-1]["text"]
    record = registry.lookup("C1", "1716480010.000001")
    assert record is not None
    assert record.status == "converted"
    assert record.metadata["bridge_issue_url"] == "https://github.com/acme-org/api/issues/42"


def test_conversion_registers_status_thread(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, _poster, _registry = _make_listener(tmp_path, creator=creator)
    _seed_draft(listener, tmp_path)

    result = listener.handle_payload(_reply("ship it", event_id="000002b"))
    assert result.action == "issue_created"

    status_record = listener.status_tracker._load(
        listener.status_tracker._path("C1", "1716480010.000001")
    )
    assert status_record is not None
    assert status_record.repo == ALLOWED_REPO
    assert status_record.issue_number == 42
    assert status_record.issue_url == "https://github.com/acme-org/api/issues/42"
    assert status_record.last_state == "filed"


def test_trusted_reaction_approval_creates_issue(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, _poster, _registry = _make_listener(tmp_path, creator=creator)
    _seed_draft(listener, tmp_path)

    result = listener.handle_payload(_reaction("white_check_mark", event_id="000003"))

    assert result.handled is True
    assert result.action == "issue_created"
    assert len(creator.calls) == 1


def test_non_trusted_reply_creates_nothing(tmp_path: Path) -> None:
    creator = RecordingCreator()
    # U1 owns the draft; U2 is not trusted.
    listener, poster, _registry = _make_listener(tmp_path, creator=creator, trusted=("U1",))
    _seed_draft(listener, tmp_path)
    before = len(poster.messages)

    result = listener.handle_payload(_reply("ship it", event_id="000004", user="U2"))

    assert result.handled is False
    assert result.action == "ignored"
    assert "untrusted" in result.detail
    assert creator.calls == []
    assert len(poster.messages) == before


def test_non_trusted_reaction_creates_nothing(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, _poster, _registry = _make_listener(tmp_path, creator=creator, trusted=("U1",))
    _seed_draft(listener, tmp_path)

    result = listener.handle_payload(_reaction("white_check_mark", event_id="000005", user="U2"))

    assert result.handled is False
    assert result.action == "ignored"
    assert creator.calls == []


def test_ambiguous_reply_refines_only(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, _poster, _registry = _make_listener(tmp_path, creator=creator)
    draft_path = _seed_draft(listener, tmp_path)

    result = listener.handle_payload(
        _reply("acceptance: also confirm the label is applied", event_id="000006")
    )

    assert result.handled is True
    assert result.action == "draft_revised"
    assert creator.calls == []
    payload = json.loads(Path(draft_path).read_text(encoding="utf-8"))
    assert "bridge" not in payload or not payload["bridge"].get("converted")
    assert any("label is applied" in item for item in payload["draft"]["acceptance_criteria"])


def test_repo_not_in_allowlist_is_refused(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, poster, _registry = _make_listener(tmp_path, creator=creator)
    _seed_draft(listener, tmp_path, repo=OTHER_REPO)

    result = listener.handle_payload(_reply("ship it", event_id="000007"))

    assert result.handled is True
    assert result.action == "approval_no_issue"
    assert creator.calls == []
    assert "Could not create the issue" in poster.messages[-1]["text"]
    assert OTHER_REPO in poster.messages[-1]["text"]


def test_double_approval_creates_single_issue(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, poster, registry = _make_listener(tmp_path, creator=creator)
    _seed_draft(listener, tmp_path)

    first = listener.handle_payload(_reply("ship it", event_id="000008"))
    second = listener.handle_payload(_reply("ship it", event_id="000009"))

    assert first.action == "issue_created"
    assert second.action == "issue_already_created"
    assert len(creator.calls) == 1
    assert "Already filed" in poster.messages[-1]["text"]
    record = registry.lookup("C1", "1716480010.000001")
    assert record is not None
    assert record.status == "converted"


def test_disabled_bridge_keeps_approval_inert(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, poster, _registry = _make_listener(
        tmp_path, creator=creator, config=_config(enabled=False)
    )
    _seed_draft(listener, tmp_path)

    result = listener.handle_payload(_reply("ship it", event_id="000010"))

    assert result.handled is True
    assert result.action == "approval_ignored"
    assert creator.calls == []
    assert "issue bridge is off" in poster.messages[-1]["text"]


def test_reaction_on_unregistered_thread_is_ignored(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, _poster, _registry = _make_listener(tmp_path, creator=creator)
    # No draft seeded; the thread is not registered.
    result = listener.handle_payload(_reaction("white_check_mark", event_id="000011"))

    assert result.handled is False
    assert creator.calls == []


def test_non_approval_reaction_does_not_create(tmp_path: Path) -> None:
    creator = RecordingCreator()
    listener, _poster, _registry = _make_listener(tmp_path, creator=creator)
    _seed_draft(listener, tmp_path)

    result = listener.handle_payload(_reaction("eyes", event_id="000012"))

    assert result.handled is False
    assert creator.calls == []


# ---------------------------------------------------------------------------
# Body + config + default creator
# ---------------------------------------------------------------------------


def test_issue_body_includes_footer_and_safety_note() -> None:
    payload = {"issue_body": "## Problem\n\nX", "spec_body": "guardrails here"}
    body = build_issue_body(payload, thread_link="https://slack/x")
    assert "## Problem" in body
    assert "guardrails here" in body
    assert "Alfred Slack issue bridge" in body
    assert "https://slack/x" in body
    assert "every claim, spend, and review gate" in body


def test_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "acme-org/api, acme-org/web")
    monkeypatch.setenv("ALFRED_BRIDGE_LABEL", "agent:implement")
    monkeypatch.setenv("ALFRED_BRIDGE_APPROVAL_PHRASES", "ship it; launch")
    monkeypatch.setenv("ALFRED_BRIDGE_MIN_READINESS_SCORE", "90")
    cfg = BridgeConfig.from_env()
    assert cfg.enabled is True
    assert cfg.repos == frozenset({"acme-org/api", "acme-org/web"})
    assert cfg.label == "agent:implement"
    assert "launch" in cfg.approval_phrases
    assert "ship it" in cfg.approval_phrases
    assert cfg.min_readiness_score == 90


def test_config_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ALFRED_BRIDGE_ENABLED", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    cfg = BridgeConfig.from_env()
    assert cfg.enabled is False
    assert cfg.repos == frozenset()
    assert cfg.label == "agent:implement"


def test_default_issue_creator_parses_url_from_gh_stdout() -> None:
    class FakeProc:
        returncode = 0
        stdout = "Creating issue...\nhttps://github.com/acme-org/api/issues/99\n"
        stderr = ""

    captured: dict = {}

    def fake_runner(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc()

    url = default_issue_creator(
        repo="acme-org/api",
        title="T",
        body="B",
        labels=["agent:implement"],
        runner=fake_runner,
    )
    assert url == "https://github.com/acme-org/api/issues/99"
    assert captured["argv"][:3] == ["gh", "issue", "create"]
    assert "-R" in captured["argv"]
    assert "acme-org/api" in captured["argv"]
    assert "--label" in captured["argv"]


def test_default_issue_creator_returns_none_on_failure() -> None:
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    url = default_issue_creator(
        repo="acme-org/api",
        title="T",
        body="B",
        labels=["agent:implement"],
        runner=lambda argv, **kwargs: FakeProc(),
    )
    assert url is None
