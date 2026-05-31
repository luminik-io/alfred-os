"""Turn an approved Slack planning draft into a labeled GitHub issue.

This module is the missing wire between Slack planning and the autonomous
fleet. The Slack planning listener (``slack_listener.py``) already refines a
trusted user's request into a saved planning draft. This bridge takes the
final, deliberate step: when a trusted user *explicitly approves* a draft in
its registered thread, it converts the saved draft JSON into a single GitHub
issue carrying the pickup label the fleet watches for (``agent:implement`` by
default).

CRITICAL SAFETY MODEL
=====================

This bridge does **not** run code, open worktrees, push branches, or spawn an
agent. It only creates one labeled GitHub issue. From there the existing
autonomous fleet (Lucius / Batman) discovers the issue through
``pick_issue()`` and claims it through *every* existing gate:

* the per-agent claim lock and claim/release state machine
  (``agent_runner.github.claim_issue``);
* spend caps, review, and Batman's multi-repo approval;
* repo pause overrides and ``do-not-pickup`` / ``needs:human-scope`` labels.

By producing an issue rather than executing anything, the bridge *reuses* the
fleet's safety machinery instead of bypassing it. A bug in this module can, at
worst, file an unwanted issue -- which still cannot ship without passing every
downstream gate.

Five independent gates are *all* required before an issue is created:

1. **Bridge enabled.** The operator must explicitly enable the bridge.
2. **Trusted user.** The approval must come from a configured trusted Slack
   user (the listener gates this in ``handle_payload``; the bridge re-checks).
3. **Explicit approval token.** The reply must contain an explicit approval
   phrase (default: ``ship it`` / ``create issue`` / ``file issue`` /
   ``/ship``) or be an approval reaction (``white_check_mark``). Ambiguous
   prose is never treated as approval -- it falls through to the normal refine
   path.
4. **Ready draft.** The saved draft must carry a readiness report with no
   blocking findings and a score at or above
   ``ALFRED_BRIDGE_MIN_READINESS_SCORE``.
5. **Allowed repo.** Every target repo must be present in
   ``ALFRED_BRIDGE_REPOS``.

No single gate alone suffices, and a non-trusted user can never trigger
creation regardless of text.

Configuration (all env-driven, 12-factor):

* ``ALFRED_BRIDGE_ENABLED`` -- ``1``/``true``/``yes``/``on`` to enable. Default
  off: an unset or false value makes every approval a no-op (refine only).
* ``ALFRED_BRIDGE_REPOS`` -- comma/space separated allowlist of ``owner/repo``
  slugs. A draft repo outside this list is refused. Required when enabled.
* ``ALFRED_BRIDGE_LABEL`` -- the pickup label applied to the created issue.
  Default ``agent:implement``.
* ``ALFRED_BRIDGE_APPROVAL_PHRASES`` -- optional comma/semicolon separated
  override of the approval phrase list.
* ``ALFRED_BRIDGE_MIN_READINESS_SCORE`` -- minimum saved readiness score before
  filing. Default ``80``.

The actual ``gh issue create`` call is injected (``issue_creator``) so tests
exercise the full path without the network. The default creator shells out to
``gh`` exactly like ``connectors/runner.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

ENV_ENABLED = "ALFRED_BRIDGE_ENABLED"
ENV_REPOS = "ALFRED_BRIDGE_REPOS"
ENV_LABEL = "ALFRED_BRIDGE_LABEL"
ENV_APPROVAL_PHRASES = "ALFRED_BRIDGE_APPROVAL_PHRASES"
ENV_MIN_READINESS_SCORE = "ALFRED_BRIDGE_MIN_READINESS_SCORE"

DEFAULT_LABEL = "agent:implement"
DEFAULT_MIN_READINESS_SCORE = 80

# Default explicit approval phrases. These are matched as whole, normalized
# tokens against the whole reply (see ``contains_approval_token``); they are
# deliberately action-oriented so ordinary refinement prose like "let's go with
# two repos" or "ship the docs separately" does NOT approve.
DEFAULT_APPROVAL_PHRASES: tuple[str, ...] = (
    "ship it",
    "create issue",
    "file issue",
    "/ship",
)

# Reactions that count as an explicit approval. Keep this intentionally narrow:
# a check mark is a deliberate approval marker in Alfred threads, while more
# expressive reactions are too easy to use as casual acknowledgement.
DEFAULT_APPROVAL_REACTIONS: frozenset[str] = frozenset({"white_check_mark"})

_MAX_TITLE = 256


class IssueCreator(Protocol):
    """Creates a GitHub issue and returns its URL (or ``None`` on failure)."""

    def __call__(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: list[str],
    ) -> str | None: ...


@dataclass(frozen=True)
class BridgeConfig:
    """Resolved, immutable bridge configuration for one listener instance."""

    enabled: bool
    repos: frozenset[str]
    label: str
    approval_phrases: tuple[str, ...]
    min_readiness_score: int = DEFAULT_MIN_READINESS_SCORE
    approval_reactions: frozenset[str] = DEFAULT_APPROVAL_REACTIONS

    @classmethod
    def from_env(cls) -> BridgeConfig:
        return cls(
            enabled=_env_flag(ENV_ENABLED),
            repos=frozenset(_parse_repo_allowlist(os.environ.get(ENV_REPOS))),
            label=(os.environ.get(ENV_LABEL) or "").strip() or DEFAULT_LABEL,
            approval_phrases=_parse_approval_phrases(os.environ.get(ENV_APPROVAL_PHRASES)),
            min_readiness_score=_parse_min_readiness_score(os.environ.get(ENV_MIN_READINESS_SCORE)),
        )


@dataclass(frozen=True)
class BridgeOutcome:
    """Result of one bridge conversion attempt."""

    created: bool
    status: str
    detail: str = ""
    issue_url: str = ""
    repo: str = ""

    @property
    def refused(self) -> bool:
        return not self.created and self.status not in {"not_approval", "disabled"}


# ---------------------------------------------------------------------------
# Approval detection (pure, no side effects)
# ---------------------------------------------------------------------------


def contains_approval_token(text: str, phrases: Iterable[str]) -> bool:
    """Return True iff ``text`` contains an explicit approval phrase.

    Matching is deliberately strict: each phrase must appear as a whole,
    word-bounded token in the normalized text. A bare ``go`` does not match
    inside ``going`` or ``good to go with edits``-style sentences that also
    carry other instructions, because we require the phrase to stand on its
    own line or be the entire trimmed message. This keeps refinement prose
    from being mistaken for approval.
    """
    normalized_lines = _approval_candidate_lines(text)
    if not normalized_lines:
        return False
    wanted = {_normalize_phrase(p) for p in phrases if _normalize_phrase(p)}
    if not wanted:
        return False
    return any(line in wanted for line in normalized_lines)


def _approval_candidate_lines(text: str) -> list[str]:
    """Normalized whole-message and per-line candidates for token matching.

    An approval token only counts when it is the entire message or an entire
    line of it (after stripping punctuation and mentions). This prevents
    ``go ahead and add repo: x`` -- which carries a real instruction -- from
    being read as a bare ``go`` approval.
    """
    cleaned = _strip_mentions(text)
    candidates: list[str] = []
    whole = _normalize_phrase(cleaned)
    if whole:
        candidates.append(whole)
    for line in cleaned.splitlines():
        norm = _normalize_phrase(line)
        if norm:
            candidates.append(norm)
    return candidates


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class SlackIssueBridge:
    """Convert an approved planning draft into a labeled GitHub issue.

    The bridge holds no Slack state of its own. The listener owns trust
    gating, the thread registry, and the saved draft; it calls
    :meth:`convert` only after deciding a trusted user explicitly approved a
    registered draft thread. The bridge re-verifies enablement, the approval
    token, and the repo allowlist before doing anything observable.
    """

    def __init__(
        self,
        *,
        config: BridgeConfig | None = None,
        issue_creator: IssueCreator | None = None,
    ) -> None:
        self.config = config or BridgeConfig.from_env()
        self._create_issue = issue_creator or default_issue_creator

    def is_approval(self, *, text: str = "", reaction: str = "") -> bool:
        """Return True iff ``text`` or ``reaction`` is an explicit approval.

        Pure predicate; does not consider trust or enablement. The listener
        combines this with its own trust check before calling :meth:`convert`.
        """
        reaction_name = (reaction or "").split("::", 1)[0].strip()
        if reaction_name and reaction_name in self.config.approval_reactions:
            return True
        return contains_approval_token(text, self.config.approval_phrases)

    def convert(
        self,
        draft_payload: dict,
        *,
        trusted: bool,
        thread_link: str = "",
        already_converted: bool = False,
    ) -> BridgeOutcome:
        """Create one labeled issue from a saved planning-draft payload.

        Args:
            draft_payload: the parsed ``planning-drafts/*.json`` content.
            trusted: whether the approving Slack user is a configured trusted
                user. The listener already gates this; the bridge refuses
                outright if it is ever False (defense in depth).
            thread_link: a permalink/back-reference to the Slack thread, added
                to the issue footer for traceability.
            already_converted: True when the draft/thread was already converted
                once; the bridge refuses to double-create (idempotency).

        Returns:
            A :class:`BridgeOutcome`. ``created`` is True only when an issue
            URL came back from the creator.
        """
        # SAFETY GATE 1: trusted user. A non-trusted approval can never create.
        if not trusted:
            return BridgeOutcome(False, "refused_untrusted", "approval from non-trusted user")

        # SAFETY GATE 2: feature must be explicitly enabled.
        if not self.config.enabled:
            return BridgeOutcome(False, "disabled", f"{ENV_ENABLED} is not enabled")

        # SAFETY GATE 3: idempotency -- never double-create for one draft.
        if already_converted:
            existing = _existing_issue_url(draft_payload)
            return BridgeOutcome(
                False,
                "already_converted",
                "draft already converted to an issue",
                issue_url=existing,
            )

        draft = draft_payload.get("draft")
        if not isinstance(draft, dict):
            return BridgeOutcome(False, "refused_no_draft", "saved draft is missing or malformed")

        # SAFETY GATE 4: saved readiness must be good enough to file.
        readiness_refusal = _readiness_refusal(
            draft_payload,
            min_score=self.config.min_readiness_score,
        )
        if readiness_refusal is not None:
            return readiness_refusal

        # SAFETY GATE 5: every target repo must be in the configured allowlist.
        repos = _draft_repos(draft)
        if not repos:
            return BridgeOutcome(
                False,
                "refused_no_repo",
                "draft has no concrete owner/repo scope to file against",
            )
        if not self.config.repos:
            return BridgeOutcome(
                False,
                "refused_allowlist_empty",
                f"no repos configured in {ENV_REPOS}; refusing to file anywhere",
            )
        disallowed = [repo for repo in repos if repo not in self.config.repos]
        if disallowed:
            return BridgeOutcome(
                False,
                "refused_repo_not_allowed",
                "repo(s) not in allowlist: " + ", ".join(disallowed),
            )

        target_repo = repos[0]
        title = _truncate_title(str(draft.get("title") or "").strip())
        if not title:
            return BridgeOutcome(False, "refused_no_title", "draft has no title")
        body = build_issue_body(draft_payload, thread_link=thread_link)

        try:
            url = self._create_issue(
                repo=target_repo,
                title=title,
                body=body,
                labels=[self.config.label],
            )
        except Exception as exc:  # creator must not crash the listener
            return BridgeOutcome(
                False,
                "create_failed",
                f"issue creator raised: {type(exc).__name__}: {exc}",
                repo=target_repo,
            )
        if not url:
            return BridgeOutcome(
                False,
                "create_failed",
                "gh issue create returned no URL",
                repo=target_repo,
            )
        return BridgeOutcome(
            True,
            "created",
            f"filed {target_repo} with label {self.config.label}",
            issue_url=url,
            repo=target_repo,
        )


# ---------------------------------------------------------------------------
# Issue body composition
# ---------------------------------------------------------------------------


def build_issue_body(draft_payload: dict, *, thread_link: str = "") -> str:
    """Build the GitHub issue body from a saved planning-draft payload.

    Prefers the structured ``issue_body`` already rendered by the planning
    assistant (acceptance criteria, repo scope, etc.). Appends the rendered
    spec body when present so the implementing agent gets the full guardrails,
    then a footer noting the Slack origin for traceability.
    """
    parts: list[str] = []
    issue_body = str(draft_payload.get("issue_body") or "").strip()
    if issue_body:
        parts.append(issue_body)
    spec_body = str(draft_payload.get("spec_body") or "").strip()
    already_present = "\n\n".join(parts)
    if spec_body and spec_body not in already_present:
        parts.append("## Development Spec\n\n" + spec_body)
    parts.append(_origin_footer(thread_link))
    return "\n\n".join(part for part in parts if part).rstrip() + "\n"


def _origin_footer(thread_link: str) -> str:
    lines = [
        "---",
        "",
        "Filed by the Alfred Slack issue bridge after an explicit, trusted in-thread approval.",
    ]
    link = (thread_link or "").strip()
    if link:
        lines.append(f"Source Slack thread: {link}")
    lines.append(
        "This issue enters the normal autonomous queue and is still subject to "
        "every claim, spend, and review gate before any change ships.",
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default gh-backed issue creator
# ---------------------------------------------------------------------------


def default_issue_creator(
    *,
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str | None:
    """Create an issue via the ``gh`` CLI and return its URL.

    Mirrors ``connectors/runner.py``: shells out to ``gh issue create`` with
    the full ``owner/repo`` slug and one ``--label`` per label. Returns the
    issue URL printed by ``gh`` on success, or ``None`` on any failure.
    """
    argv = ["gh", "issue", "create", "-R", repo, "--title", title, "--body", body]
    for label in labels:
        argv.extend(["--label", label])
    run = runner or _run_subprocess
    try:
        cp = run(argv, capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    return _extract_issue_url(cp.stdout or "")


def _run_subprocess(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=False, **kwargs)


def _extract_issue_url(stdout: str) -> str | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("https://"):
            return line
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _draft_repos(draft: dict) -> list[str]:
    raw = draft.get("repos")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        repo = str(item or "").strip()
        if repo and _is_repo_slug(repo) and repo not in seen:
            seen.add(repo)
            out.append(repo)
    return out


def _is_repo_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value.strip()))


def _existing_issue_url(draft_payload: dict) -> str:
    bridge = draft_payload.get("bridge")
    if isinstance(bridge, dict):
        url = str(bridge.get("issue_url") or "").strip()
        if url:
            return url
    return ""


def _truncate_title(title: str) -> str:
    title = (title or "").strip().replace("\n", " ").replace("\r", " ")
    if len(title) <= _MAX_TITLE:
        return title
    return title[: _MAX_TITLE - 1].rstrip() + "…"


def _parse_repo_allowlist(raw: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,;\s]+", str(raw or "")):
        repo = item.strip()
        if repo and _is_repo_slug(repo) and repo not in seen:
            seen.add(repo)
            out.append(repo)
    return out


def _parse_approval_phrases(raw: str | None) -> tuple[str, ...]:
    if raw is None or not str(raw).strip():
        return DEFAULT_APPROVAL_PHRASES
    out: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,;\n]+", str(raw)):
        phrase = _normalize_phrase(item)
        if phrase and phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
    return tuple(out) or DEFAULT_APPROVAL_PHRASES


def _parse_min_readiness_score(raw: str | None) -> int:
    if raw is None or not str(raw).strip():
        return DEFAULT_MIN_READINESS_SCORE
    try:
        score = int(str(raw).strip())
    except ValueError:
        return DEFAULT_MIN_READINESS_SCORE
    return max(0, min(100, score))


def _readiness_refusal(draft_payload: dict[str, Any], *, min_score: int) -> BridgeOutcome | None:
    readiness = draft_payload.get("readiness")
    if not isinstance(readiness, dict):
        return BridgeOutcome(
            False,
            "refused_readiness_missing",
            "draft has no readiness report; revise it in Slack or Compose before filing",
        )
    ok = bool(readiness.get("ok"))
    raw_score = readiness.get("score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, int | float | str):
        return BridgeOutcome(
            False,
            "refused_readiness_missing",
            "draft readiness score is missing or invalid; revise it before filing",
        )
    try:
        score = int(raw_score)
    except ValueError:
        return BridgeOutcome(
            False,
            "refused_readiness_missing",
            "draft readiness score is missing or invalid; revise it before filing",
        )
    if ok and score >= min_score:
        return None
    questions = draft_payload.get("questions")
    detail = f"draft readiness is {score}/100; required {min_score}/100"
    if isinstance(questions, list):
        clean_questions = [str(item).strip() for item in questions if str(item).strip()]
        if clean_questions:
            detail += ". Answer first: " + "; ".join(clean_questions[:3])
    if not ok:
        detail += ". Readiness still has blocking findings."
    return BridgeOutcome(False, "refused_not_ready", detail)


def _normalize_phrase(value: str) -> str:
    text = str(value or "").lower()
    # Keep leading slash for slash-style tokens like /ship, drop other punct.
    text = re.sub(r"[^\w/ ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_mentions(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", str(text or "")).strip()


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "DEFAULT_APPROVAL_PHRASES",
    "DEFAULT_APPROVAL_REACTIONS",
    "DEFAULT_LABEL",
    "DEFAULT_MIN_READINESS_SCORE",
    "ENV_APPROVAL_PHRASES",
    "ENV_ENABLED",
    "ENV_LABEL",
    "ENV_MIN_READINESS_SCORE",
    "ENV_REPOS",
    "BridgeConfig",
    "BridgeOutcome",
    "IssueCreator",
    "SlackIssueBridge",
    "build_issue_body",
    "contains_approval_token",
    "default_issue_creator",
]
