"""
ConnectorRunner: drain each registered connector, file ``agent:implement``
issues for new drafts.

Lifecycle of one ``sync()`` call::

    for connector in connectors:
        since = state["last_poll_at"]            # ISO timestamp or None
        drafts = connector.poll(since=since)
        for d in drafts:
            if d.source_id in state["seen_ids"]:
                continue                          # already filed
            issue = _file_issue(connector, d)     # gh issue create
            if issue.success:
                state["seen_ids"].append(d.source_id)
                connector.mark_seen(d)
        state["last_poll_at"] = now()
        save_state(...)

Design choices
--------------
* The runner is the single side-effect boundary: every ``gh issue create``
  call lives here, not in connectors. New connectors stay simple.
* Dedup runs twice - once against the runner-owned seen-cache, once via
  the connector's own ``mark_seen``. Belt-and-suspenders so a buggy
  connector cannot double-file.
* ``--dry-run`` mode short-circuits ``gh`` and prints what *would* be
  filed; the seen-cache still updates so a subsequent live run does not
  re-fire on the same drafts.
* Failures inside one connector never break the others. Each connector
  has its own try/except and contributes a row to the report.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import Connector, IssueDraft, Severity
from ._state import load_state, parse_last_poll, save_state

logger = logging.getLogger(__name__)

# Labels the runner *always* adds to every filed issue. Keep the list
# tiny: the queue label that the engineering fleet watches, plus a
# source-type tag so operators can filter.
ALWAYS_LABELS: list[str] = ["agent:implement", "connector"]

# Severity -> connector severity label. The agent fleet's own severity
# routing is independent; these labels only let operators filter.
SEVERITY_LABEL: dict[Severity, str] = {
    "info": "connector:info",
    "warning": "connector:warn",
    "blocker": "connector:blocker",
}


@dataclass
class FiledIssue:
    """One row of the runner report."""

    source: str
    source_id: str
    title: str
    target_repo: str
    issue_url: str | None
    labels: list[str]
    skipped_reason: str | None = None
    error: str | None = None


@dataclass
class SyncReport:
    """Aggregate result of one ``sync()`` call."""

    started_at: datetime
    finished_at: datetime | None = None
    filed: list[FiledIssue] = field(default_factory=list)
    skipped: list[FiledIssue] = field(default_factory=list)
    failed: list[FiledIssue] = field(default_factory=list)

    @property
    def filed_count(self) -> int:
        return len(self.filed)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def failed_count(self) -> int:
        return len(self.failed)


class ConnectorRunner:
    """Drain a fixed list of connectors and file GitHub issues for each draft.

    Parameters
    ----------
    connectors:
        Ordered list of ``Connector`` instances. The runner drains them
        in declaration order; a slow connector cannot starve a later one
        because each is processed in its own try/except scope.
    dry_run:
        When True, no ``gh`` calls are made; report rows still describe
        what *would* have been filed and the seen-cache is updated so a
        subsequent live run will not re-fire.
    gh_runner:
        Callable that takes a list of argv tokens and returns
        ``subprocess.CompletedProcess``. Defaults to ``subprocess.run``.
        Tests swap this for a fake to assert on emitted commands without
        touching the real ``gh`` CLI.
    """

    def __init__(
        self,
        connectors: Iterable[Connector],
        *,
        dry_run: bool = False,
        gh_runner=None,
    ) -> None:
        self.connectors = list(connectors)
        self.dry_run = dry_run
        self._gh = gh_runner or self._default_gh

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def sync(self) -> SyncReport:
        report = SyncReport(started_at=datetime.now(UTC))
        for connector in self.connectors:
            try:
                self._drain_one(connector, report)
            except Exception as e:
                logger.exception("connector %s crashed during sync", connector.name)
                report.failed.append(
                    FiledIssue(
                        source=connector.name,
                        source_id="<crash>",
                        title="(connector crash)",
                        target_repo="",
                        issue_url=None,
                        labels=[],
                        error=f"{type(e).__name__}: {e}",
                    )
                )
        report.finished_at = datetime.now(UTC)
        return report

    # ------------------------------------------------------------------
    # Per-connector loop
    # ------------------------------------------------------------------
    def _drain_one(self, connector: Connector, report: SyncReport) -> None:
        state = load_state(connector.name)
        since = parse_last_poll(state["last_poll_at"])
        seen: set[str] = set(state["seen_ids"])

        logger.info(
            "connector %s: poll(since=%s, seen=%d)",
            connector.name,
            since.isoformat() if since else "None",
            len(seen),
        )

        drafts = connector.poll(since=since)
        new_seen: list[str] = list(state["seen_ids"])
        poll_started_at = datetime.now(UTC)

        for draft in drafts:
            if draft.source_id in seen:
                report.skipped.append(
                    FiledIssue(
                        source=connector.name,
                        source_id=draft.source_id,
                        title=draft.title,
                        target_repo=self._resolve_repo(connector, draft),
                        issue_url=None,
                        labels=[],
                        skipped_reason="already-seen",
                    )
                )
                continue

            row = self._file_one(connector, draft)
            if row.error:
                report.failed.append(row)
                # Don't mark seen on a failure - try again next poll.
                continue

            report.filed.append(row)
            new_seen.append(draft.source_id)
            seen.add(draft.source_id)
            try:
                connector.mark_seen(draft)
            except Exception:
                logger.exception(
                    "connector %s.mark_seen(%s) raised; runner cache still updated",
                    connector.name,
                    draft.source_id,
                )

        save_state(connector.name, last_poll_at=poll_started_at, seen_ids=new_seen)

    # ------------------------------------------------------------------
    # File one issue
    # ------------------------------------------------------------------
    def _file_one(self, connector: Connector, draft: IssueDraft) -> FiledIssue:
        repo = self._resolve_repo(connector, draft)
        if not repo:
            return FiledIssue(
                source=connector.name,
                source_id=draft.source_id,
                title=draft.title,
                target_repo="",
                issue_url=None,
                labels=[],
                error="no target_repo configured for connector and no draft override",
            )

        labels = self._merge_labels(connector, draft)
        title = _truncate_title(draft.title)
        body = _compose_body(draft)

        if self.dry_run:
            logger.info(
                "[dry-run] gh issue create -R %s --title %r --label %s",
                repo,
                title,
                ",".join(labels),
            )
            return FiledIssue(
                source=connector.name,
                source_id=draft.source_id,
                title=title,
                target_repo=repo,
                issue_url=f"https://github.com/{repo}/issues/0",
                labels=labels,
            )

        argv = ["gh", "issue", "create", "-R", repo, "--title", title, "--body", body]
        for label in labels:
            argv.extend(["--label", label])

        try:
            cp = self._gh(argv)
        except Exception as e:
            return FiledIssue(
                source=connector.name,
                source_id=draft.source_id,
                title=title,
                target_repo=repo,
                issue_url=None,
                labels=labels,
                error=f"gh subprocess raised: {type(e).__name__}: {e}",
            )

        if cp.returncode != 0:
            return FiledIssue(
                source=connector.name,
                source_id=draft.source_id,
                title=title,
                target_repo=repo,
                issue_url=None,
                labels=labels,
                error=f"gh exited {cp.returncode}: {(cp.stderr or '').strip()[:300]}",
            )

        url = _extract_issue_url(cp.stdout or "")
        return FiledIssue(
            source=connector.name,
            source_id=draft.source_id,
            title=title,
            target_repo=repo,
            issue_url=url,
            labels=labels,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_repo(connector: Connector, draft: IssueDraft) -> str:
        return (draft.target_repo or connector.default_repo or "").strip()

    @staticmethod
    def _merge_labels(connector: Connector, draft: IssueDraft) -> list[str]:
        # Stable dedup-preserving merge: ALWAYS + connector defaults +
        # draft labels + severity.
        out: list[str] = []
        seen: set[str] = set()

        def _add(label: str) -> None:
            label = label.strip()
            if not label or label in seen:
                return
            seen.add(label)
            out.append(label)

        for src in (
            ALWAYS_LABELS,
            list(connector.default_labels or []),
            list(draft.labels or []),
            [SEVERITY_LABEL.get(draft.severity, "connector:info")],
        ):
            for label in src:
                _add(label)
        return out

    @staticmethod
    def _default_gh(argv: list[str]) -> subprocess.CompletedProcess:
        logger.debug("gh subprocess: %s", " ".join(shlex.quote(a) for a in argv))
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


# ---------------------------------------------------------------------------
# Pure helpers (free functions, easy to unit-test)
# ---------------------------------------------------------------------------


_MAX_TITLE = 200
_FOOTER_HEADER = "---"


def _truncate_title(title: str) -> str:
    title = (title or "").strip().replace("\n", " ").replace("\r", " ")
    if len(title) <= _MAX_TITLE:
        return title or "(untitled)"
    return title[: _MAX_TITLE - 1].rstrip() + "…"


def _compose_body(draft: IssueDraft) -> str:
    """Compose the GitHub issue body with a trailing source footer.

    The footer carries the upstream URL plus the ``source/source_id``
    pair so a human reading the GitHub issue can trace it back, and so
    later automation (close-on-resolve, etc.) can locate the upstream
    record without re-parsing the body.
    """
    body = (draft.body or "").rstrip()
    footer_lines = [
        "",
        _FOOTER_HEADER,
        f"Filed by Alfred connector `{draft.source}` from `{draft.source_id}`.",
    ]
    if draft.source_url:
        footer_lines.append(f"Source: {draft.source_url}")
    return body + "\n" + "\n".join(footer_lines) + "\n"


def _extract_issue_url(stdout: str) -> str | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("https://"):
            return line
    return None


__all__ = [
    "ALWAYS_LABELS",
    "SEVERITY_LABEL",
    "ConnectorRunner",
    "FiledIssue",
    "SyncReport",
]
