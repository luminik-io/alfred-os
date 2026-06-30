"""Alfred's fleet-brain: a local procedural-learning memory layer.

``fleet_brain`` records what each agent firing learned about a repo
or codename. It keeps reviewable candidates, firing history, file
touches, GitHub cache rows, and local evidence under ``$ALFRED_HOME``.
Redis Agent Memory is the default recalled-lesson layer for new
installs; FleetBrain is the local ledger behind that review loop.

Quick start::

    from fleet_brain import FleetBrain

    brain = FleetBrain()
    brain.reflect(
        codename="lucius",
        repo="your-org/api",
        body="GraphQL schema lives in src/schema.graphql; tests live next to it.",
        tags=["graphql", "layout"],
    )
    lessons = brain.recall(codename="lucius", repo="your-org/api")
    for L in lessons:
        print(L.body)

Public surface:

* :class:`FleetBrain`: the main API: ``recall``, ``reflect``,
  ``firing_log``, ``record_file_touch``, ``note_repo``, ``forget``,
  ``export``.
* :class:`fleet_brain.store.Lesson`, :class:`FiringLog`,
  :class:`FileTouch`, :class:`RepoNote`: entity dataclasses,
  re-exported here.
* :class:`fleet_brain.store.Store`: the Protocol the public API
  depends on. The default local ledger implementation is
  :class:`SQLiteStore`.

Privacy: the FleetBrain ledger is a SQLite file in your
``$ALFRED_HOME``. It never leaves your machine. The only outbound
surface is prompt context sent to Claude Code or Codex on your
existing CLI auth, plus anonymous usage totals if telemetry is left
on. No raw prompts, transcripts, or candidate text are sent by
telemetry.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .graph import (
    CodeOwnerRule,
    GraphEdge,
    densify_enabled,
    edges_for_file_touch,
    file_node,
    owners_for_path,
    parse_codeowners,
)
from .store import (
    BundleItem,
    CodeOwnerRow,
    FailureEvent,
    FileChangeType,
    FileTouch,
    FiringLog,
    FiringStatus,
    GitHubItem,
    GitHubItemKind,
    GitHubItemState,
    GraphEdgeRow,
    Lesson,
    MemoryCandidate,
    MemoryCandidateStatus,
    RepoNote,
    Severity,
    SQLiteStore,
    Store,
    WorkerHeartbeat,
    WorkerStatus,
    default_db_path,
    new_id,
)

__all__ = [
    "BundleItem",
    "CodeOwnerRow",
    "CodeOwnerRule",
    "FailureEvent",
    "FileChangeType",
    "FileTouch",
    "FiringLog",
    "FiringStatus",
    "FleetBrain",
    "GitHubItem",
    "GitHubItemKind",
    "GitHubItemState",
    "GraphEdge",
    "GraphEdgeRow",
    "Lesson",
    "MemoryCandidate",
    "MemoryCandidateStatus",
    "MemoryPromotionError",
    "RepoNote",
    "SQLiteStore",
    "Severity",
    "Store",
    "WorkerHeartbeat",
    "WorkerStatus",
    "default_db_path",
    "densify_enabled",
    "direct_auto_promote_env",
    "edges_for_file_touch",
    "new_id",
    "owners_for_path",
    "parse_codeowners",
]


_LOG = logging.getLogger(__name__)

# Cap recall output so a runaway codename can't blow up a prompt.
_RECALL_DEFAULT = 8
_RECALL_MAX = 50
_NON_ACTIONABLE_FAILURE_SUBTYPES = {
    "already_implemented",
    "already-implemented",
    "daily-cap",
    "dedup-skip",
    "dedup_skip",
    "fixes-landed",
    "green",
    "idle-no-candidates",
    "idle-no-comments",
    "idle-no-pr",
    "noop",
    "ok",
    "pr-opened",
    "review-cap",
    "review-posted",
    "silent-no-work",
    "silent_no_work",
    "success",
    "test-ok",
    "test_ok",
    "triage-cap",
    "triaged",
}

# Auto-promotion defaults. Every one is env-tunable so a deployment can tune
# the gate without a code change. Auto-promotion is ON by default when the flag
# is unset/blank or a recognized truthy value: the LLM judge is the primary
# save/skip decision, while ``ALFRED_AUTO_PROMOTE=0``, malformed nonblank
# values, and ``ALFRED_AUTO_PROMOTE_KILL=1`` fail closed.
# The threshold is a LIGHT pre-filter, not the decision: any evidenced
# candidate (candidates default to confidence 0.5) must reach the LLM judge,
# which makes the real save/skip call. Memory has to capture AND save
# autonomously via the model; a high bar that dumps observed lessons to a
# human queue just piles up and never gets reviewed.
AUTO_PROMOTE_DEFAULT_THRESHOLD = 0.5
AUTO_PROMOTE_DEFAULT_MAX_PER_RUN = 5
AUTO_PROMOTE_DEFAULT_MAX_JUDGE_CALLS = 25
# When the LLM judge is explicitly disabled, the structural confidence is the
# ONLY gate, so the low judge-era bar would auto-promote every evidenced
# default-confidence candidate with no review. Hold a conservative floor in
# that case (env-tunable) so heuristic-only promotion stays selective.
AUTO_PROMOTE_NO_JUDGE_THRESHOLD = 0.9

# A candidate the auto-promoter has set aside for a human keeps status
# ``candidate`` (so it stays in the review queue and the dedup index) but its
# review_note is stamped with this marker. Subsequent runs see the marker and
# never re-judge the row, so a held candidate cannot starve the per-run judge
# budget or re-post the same alert every run.
_AUTO_HELD_MARKER = "[held-for-review]"
_TRUTHY_ENV_TOKENS = {"1", "true", "yes", "on", "enabled"}
_FALSY_ENV_TOKENS = {"0", "false", "no", "off", "disabled"}
_RECOGNIZED_ENV_TOKENS = _TRUTHY_ENV_TOKENS | _FALSY_ENV_TOKENS
_AUTO_PROMOTE_STOP_KEYS = {
    "ALFRED_AUTO_PROMOTE",
    "ALFRED_AUTO_PROMOTE_KILL",
    "ALFRED_AUTO_PROMOTE_LLM_JUDGE",
}

# Promoted lessons are written to Redis AMS under a deterministic id derived
# from the candidate. This makes the write idempotent (a re-promote upserts the
# same record) and lets the revert lever forget exactly the lesson it wrote.
_LESSON_MEMORY_ID_PREFIX = "lesson:memory_candidate:"


def _lesson_memory_id(candidate_id: str) -> str:
    """Deterministic AMS memory id for a promoted candidate."""
    return f"{_LESSON_MEMORY_ID_PREFIX}{candidate_id}"


class MemoryPromotionError(RuntimeError):
    """Raised when a candidate could not be written to Redis AMS.

    The candidate is left untouched (still ``candidate``/pending) so it can be
    re-promoted on a later run. There is no silent local fallback: a promoted
    lesson lives in AMS or nowhere.
    """


def _env_kill_switch_on(name: str, env: Mapping[str, str] | None = None) -> bool:
    """Default off, but treat malformed nonblank values as enabled."""
    src = env if env is not None else os.environ
    raw = src.get(name)
    value = _env_token(raw)
    if raw is None or not value:
        return False
    return value not in _FALSY_ENV_TOKENS


def _env_flag_default_on(name: str, env: Mapping[str, str] | None = None) -> bool:
    """Default to ON, but fail closed for any unrecognized nonblank value."""
    src = env if env is not None else os.environ
    raw = src.get(name)
    value = _env_token(raw)
    if raw is None or not value:
        return True
    if value in _TRUTHY_ENV_TOKENS:
        return True
    if value in _FALSY_ENV_TOKENS:
        return False
    return False


def _env_flag_recognized_or_blank(name: str, env: Mapping[str, str] | None = None) -> bool:
    """True when a flag is absent, blank, or a recognized truthy/falsy token."""
    src = env if env is not None else os.environ
    raw = src.get(name)
    value = _env_token(raw)
    if raw is None or not value:
        return True
    return value in _RECOGNIZED_ENV_TOKENS


def _llm_judge_flag_allows_auto_promote(env: Mapping[str, str] | None = None) -> bool:
    return _env_flag_recognized_or_blank("ALFRED_AUTO_PROMOTE_LLM_JUDGE", env)


def _auto_promote_switches_allow_learning(env: Mapping[str, str] | None = None) -> bool:
    if _env_kill_switch_on("ALFRED_AUTO_PROMOTE_KILL", env):
        return False
    if not _llm_judge_flag_allows_auto_promote(env):
        return False
    return _env_flag_default_on("ALFRED_AUTO_PROMOTE", env)


def _strip_shell_inline_comment(value: str) -> str:
    """Strip shell-style inline comments while preserving quoted hashes."""
    quote: str | None = None
    escaped = False
    for index, ch in enumerate(value):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == "#" and index > 0 and value[index - 1].isspace():
            return value[:index].rstrip()
    return value


def _env_token(raw: object) -> str:
    """Normalize env flag values, accepting shell-style trailing comments."""
    value = _strip_shell_inline_comment(str(raw)).strip()
    return value.strip().lower()


def _auto_promote_stop_control_active(name: str, raw: object) -> bool:
    if name not in _AUTO_PROMOTE_STOP_KEYS:
        return False
    value = _env_token(raw)
    if not value:
        return False
    if name in {"ALFRED_AUTO_PROMOTE", "ALFRED_AUTO_PROMOTE_LLM_JUDGE"}:
        return value not in _TRUTHY_ENV_TOKENS
    if name == "ALFRED_AUTO_PROMOTE_KILL":
        return value not in _FALSY_ENV_TOKENS
    return False


def _decode_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("'\"'\"'", "'")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _expand_home(value: str) -> str:
    return value.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))


def _load_auto_promote_env_file(
    path: Path,
    env: dict[str, str],
    *,
    override_existing: bool = False,
    protected_keys: set[str] | None = None,
    protected_key_overrides: set[str] | None = None,
) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    protected = protected_keys or set()
    protected_overrides = protected_key_overrides or set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, _, raw_value = line.partition("=")
        key = key.strip()
        if not key or key[0].isdigit() or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        raw_value = _strip_shell_inline_comment(raw_value).strip()
        value = _decode_env_value(raw_value)
        if not (raw_value.startswith("'") and raw_value.endswith("'")):
            value = _expand_home(value)
        if key in env:
            if _auto_promote_stop_control_active(key, env[key]):
                continue
            if not _auto_promote_stop_control_active(key, value) and (
                not override_existing or (key in protected and key not in protected_overrides)
            ):
                continue
        env[key] = value


def direct_auto_promote_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ALFREDRC", None)
    process_keys = set(os.environ)
    if not env.get("ALFRED_HOME", "").strip():
        env["ALFRED_HOME"] = str(Path("~/.alfred").expanduser())
    else:
        env["ALFRED_HOME"] = str(Path(env["ALFRED_HOME"]).expanduser())
    _load_auto_promote_env_file(
        Path(env["ALFRED_HOME"]).expanduser() / ".env",
        env,
        protected_keys=process_keys,
        protected_key_overrides=set(),
    )
    return env


_direct_auto_promote_env = direct_auto_promote_env


def _env_float(name: str, default: float, env: Mapping[str, str] | None = None) -> float:
    """Read a float from the environment, falling back on missing/bad input."""
    src = env if env is not None else os.environ
    raw = src.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _auto_dedup_key(body: str) -> str:
    """Normalize a candidate body to a conflict key.

    The OSS ledger has no precomputed ``dedup_hash`` column, so derive a stable
    key from the body: lowercased with collapsed whitespace. Two pending
    candidates that normalize to the same key are treated as a conflict (two
    unreviewed versions of one lesson) and both are left for a human."""
    return re.sub(r"\s+", " ", (body or "").strip().lower())


class FleetBrain:
    """Local procedural-memory layer for the Alfred fleet.

    Operates on a SQLite file by default; tests can inject a custom
    :class:`Store` through the constructor.

    Method names map to the operator-facing verbs:

    * :meth:`reflect`: file a lesson the firing learned.
    * :meth:`recall`: pull lessons relevant to the next firing.
    * :meth:`firing_log`: record one firing's audit row.
    * :meth:`record_file_touch`: record a file changed by an agent.
    * :meth:`propose_memory`: stage a lesson candidate for review.
    * :meth:`record_failure`: normalize non-success outcomes for later diagnosis.
    * :meth:`upsert_github_item`: cache GitHub issue/PR state from a poller.
    * :meth:`upsert_worker_heartbeat`: record worker liveness.
    * :meth:`note_repo`: upsert a free-text repo summary.
    * :meth:`health`: confirm the local ledger is reachable.
    * :meth:`forget`: remove a lesson by id.
    * :meth:`export`: JSON-serializable snapshot for backup or
      cross-host export (the operator must do the transfer; the
      brain never phones home).
    """

    def __init__(
        self,
        store: Store | None = None,
        *,
        db_path: Path | str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if store is not None:
            self.store: Store = store
        else:
            resolved = Path(db_path) if db_path is not None else default_db_path()
            self.store = SQLiteStore(db_path=resolved)
        # Optional env override for config-driven toggles (e.g. graph
        # densification). ``None`` means read the live process environment.
        self._env: Mapping[str, str] | None = env
        self.store.ensure_schema()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FleetBrain:
        """Build a brain from the public environment contract."""
        if env is None:
            return cls()
        explicit = env.get("ALFRED_FLEET_BRAIN_DB", "").strip()
        if explicit:
            return cls(db_path=Path(explicit).expanduser(), env=env)
        alfred_home = env.get("ALFRED_HOME", "").strip()
        if alfred_home:
            return cls(db_path=Path(alfred_home).expanduser() / "fleet-brain.db", env=env)
        return cls(db_path=Path.home() / ".alfred" / "fleet-brain.db", env=env)

    # ----- write paths --------------------------------------------------

    def reflect(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        firing_id: str | None = None,
        severity: Severity = "info",
        lesson_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Lesson:
        """File a lesson the firing learned. Returns the persisted row.

        ``severity`` follows the same taxonomy as the fleet's Slack
        severity routing: ``info`` (recall-only context), ``warning``
        (worth bubbling into a future prompt), ``blocker`` (the next
        firing must read this before doing anything).
        """
        if not codename or not repo or not body:
            raise ValueError("reflect: codename, repo, and body are required")
        if severity not in ("info", "warning", "blocker"):
            raise ValueError(f"reflect: unknown severity {severity!r}")
        lesson = Lesson(
            id=lesson_id or new_id(),
            codename=codename,
            repo=repo,
            body=body.strip(),
            tags=sorted({t.strip() for t in (tags or []) if t.strip()}),
            created_at=created_at or datetime.now(UTC),
            firing_id=firing_id,
            severity=severity,
        )
        _LOG.debug("reflect: codename=%s repo=%s tags=%s", codename, repo, lesson.tags)
        return self.store.insert_lesson(lesson)

    def firing_log(
        self,
        *,
        firing_id: str,
        codename: str,
        status: FiringStatus,
        summary: str = "",
        repo: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        cost_cents: int = 0,
        pr_url: str | None = None,
        sentinel: str | None = None,
    ) -> FiringLog:
        """Persist one firing's audit row. Upserts on ``firing_id``."""
        if not firing_id or not codename:
            raise ValueError("firing_log: firing_id and codename are required")
        if status not in ("ok", "blocked", "partial", "silent"):
            raise ValueError(f"firing_log: unknown status {status!r}")
        now = datetime.now(UTC)
        log = FiringLog(
            firing_id=firing_id,
            codename=codename,
            repo=repo,
            status=status,
            summary=summary or "",
            started_at=started_at or now,
            finished_at=finished_at or now,
            cost_cents=int(cost_cents),
            pr_url=pr_url,
            sentinel=sentinel,
        )
        return self.store.insert_firing_log(log)

    def note_repo(self, *, repo: str, body: str, updated_at: datetime | None = None) -> RepoNote:
        """Upsert the free-text rollup for ``repo``."""
        if not repo or not body:
            raise ValueError("note_repo: repo and body are required")
        note = RepoNote(
            repo=repo,
            body=body.strip(),
            updated_at=updated_at or datetime.now(UTC),
        )
        return self.store.upsert_repo_note(note)

    def record_file_touch(
        self,
        *,
        repo: str,
        path: str,
        codename: str,
        firing_id: str | None = None,
        pr_url: str | None = None,
        change_type: FileChangeType = "modified",
        touch_id: str | None = None,
        touched_at: datetime | None = None,
    ) -> FileTouch:
        """Persist one repo file touched by an agent firing or PR."""
        if not repo or not path or not codename:
            raise ValueError("record_file_touch: repo, path, and codename are required")
        if change_type not in ("added", "modified", "deleted", "renamed", "unknown"):
            raise ValueError(f"record_file_touch: unknown change_type {change_type!r}")
        touch = FileTouch(
            id=touch_id or new_id(),
            repo=repo.strip(),
            path=path.strip(),
            codename=codename.strip(),
            firing_id=firing_id,
            pr_url=pr_url,
            change_type=change_type,
            touched_at=touched_at or datetime.now(UTC),
        )
        stored = self.store.insert_file_touch(touch)
        # Densify the graph with the edges this touch implies. Best-effort
        # and gated by ALFRED_GRAPH_DENSIFY (default on); a projection error
        # must never lose the recorded touch, which is the load-bearing row.
        if densify_enabled(self._env):
            try:
                self.project_file_touch_edges(stored)
            except Exception:  # densification is advisory; never lose the touch
                _LOG.warning("graph densify failed for touch %s", stored.id, exc_info=True)
        return stored

    # ----- graph densification ------------------------------------------

    def project_file_touch_edges(
        self, touch: FileTouch, *, now: datetime | None = None
    ) -> list[GraphEdgeRow]:
        """Materialize the fleet-authored edges implied by a file touch.

        Writes ``file -[in]-> repo`` always, ``PR -[changed]-> file`` when
        the touch carries a ``pr_url``, and ``file -[owned_by]-> owner`` for
        every CODEOWNERS owner currently resolved for the path. Idempotent:
        re-projecting the same touch bumps ``last_seen``/``weight`` rather
        than duplicating edges.
        """
        ts = now or touch.touched_at or datetime.now(UTC)
        owners = self.who_owns(repo=touch.repo, path=touch.path)
        specs = edges_for_file_touch(
            repo=touch.repo,
            path=touch.path,
            pr_url=touch.pr_url,
            owners=owners,
        )
        written: list[GraphEdgeRow] = []
        for spec in specs:
            row = GraphEdgeRow(
                id=new_id(),
                kind=spec.kind,
                src_type=spec.src_type,
                src=spec.src,
                dst_type=spec.dst_type,
                dst=spec.dst,
                repo=spec.repo,
                first_seen=ts,
                last_seen=ts,
                weight=1,
            )
            written.append(self.store.upsert_graph_edge(row))
        return written

    def ingest_codeowners(
        self, *, repo: str, content: str, updated_at: datetime | None = None
    ) -> int:
        """Parse and persist a repo's CODEOWNERS file.

        The new file replaces any earlier rules for the repo (CODEOWNERS is
        the single source of truth). After ingest, ``who_owns`` and future
        ``owned_by`` projections resolve against these rules. Returns the
        number of stored ``(pattern, owner)`` rules.
        """
        if not repo or not repo.strip():
            raise ValueError("ingest_codeowners: repo is required")
        repo = repo.strip()
        ts = updated_at or datetime.now(UTC)
        rules = parse_codeowners(repo, content or "")
        rows = [
            CodeOwnerRow(
                id=new_id(),
                repo=rule.repo,
                pattern=rule.pattern,
                owner=rule.owner,
                rank=rule.rank,
                updated_at=ts,
            )
            for rule in rules
        ]
        return self.store.replace_code_owners(repo, rows)

    def who_owns(self, *, repo: str, path: str) -> list[str]:
        """Return the CODEOWNERS owner(s) for ``repo``/``path``.

        Resolves against the rules ingested via :meth:`ingest_codeowners`
        using CODEOWNERS "last matching pattern wins" semantics. Returns an
        empty list when the repo has no CODEOWNERS data or nothing matches.
        """
        if not repo or not path:
            return []
        stored = self.store.list_code_owners(repo.strip())
        if not stored:
            return []
        rules = [
            CodeOwnerRule(repo=row.repo, pattern=row.pattern, owner=row.owner, rank=row.rank)
            for row in stored
        ]
        return owners_for_path(path, rules)

    def recent_changes_near(self, *, repo: str, path: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent file touches in the same directory as ``path``.

        "Near" means siblings under the same directory prefix in the same
        repo, most-recent-first. This is the graph read that answers "what
        else has the fleet been changing around here lately" without an AST.
        """
        if not repo or not path:
            return []
        repo = repo.strip()
        directory = path.strip().rsplit("/", 1)[0] if "/" in path.strip() else ""
        clamped = max(1, min(int(limit), 200))
        # Pull a generous window, then filter to the directory in Python so we
        # do not push a LIKE prefix scan into the hot list path.
        touches = self.store.list_file_touches(repo=repo, limit=500)
        out: list[dict[str, Any]] = []
        for touch in touches:
            touch_dir = touch.path.rsplit("/", 1)[0] if "/" in touch.path else ""
            if touch_dir != directory:
                continue
            out.append(
                {
                    "repo": touch.repo,
                    "path": touch.path,
                    "codename": touch.codename,
                    "change_type": touch.change_type,
                    "firing_id": touch.firing_id,
                    "pr_url": touch.pr_url,
                    "touched_at": touch.touched_at.astimezone(UTC).isoformat(),
                    "is_self": touch.path == path.strip(),
                }
            )
            if len(out) >= clamped:
                break
        return out

    def prs_touching(self, *, repo: str, path: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the pull requests that changed ``repo``/``path``.

        Reads the materialized ``PR -[changed]-> file`` edges. Falls back to
        scanning ``file_touches`` for a ``pr_url`` when graph projection is
        off, so the helper still answers correctly on a non-densified brain.
        Most-recently-seen first.
        """
        if not repo or not path:
            return []
        repo = repo.strip()
        fnode = file_node(repo, path)
        edges = self.store.list_graph_edges(kind="changed", dst=fnode, limit=500)
        clamped = max(1, min(int(limit), 200))
        if edges:
            out = [
                {
                    "pr": edge.src.split(":", 1)[1] if ":" in edge.src else edge.src,
                    "repo": edge.repo,
                    "weight": edge.weight,
                    "last_seen": edge.last_seen.astimezone(UTC).isoformat(),
                }
                for edge in edges
            ]
            return out[:clamped]
        # Fallback: derive from raw touches when no edges were projected.
        seen: dict[str, dict[str, Any]] = {}
        for touch in self.store.list_file_touches(repo=repo, path=path.strip(), limit=500):
            if not touch.pr_url:
                continue
            existing = seen.get(touch.pr_url)
            iso = touch.touched_at.astimezone(UTC).isoformat()
            if existing is None:
                seen[touch.pr_url] = {
                    "pr": touch.pr_url,
                    "repo": touch.repo,
                    "weight": 1,
                    "last_seen": iso,
                }
            else:
                existing["weight"] = int(existing["weight"]) + 1
                if iso > str(existing["last_seen"]):
                    existing["last_seen"] = iso
        ordered = sorted(seen.values(), key=lambda item: str(item["last_seen"]), reverse=True)
        return ordered[:clamped]

    def propose_memory(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        severity: Severity = "info",
        source: str = "manual",
        source_firing_id: str | None = None,
        evidence: str = "",
        confidence: float = 0.5,
        candidate_id: str | None = None,
        created_at: datetime | None = None,
    ) -> MemoryCandidate:
        """Stage a lesson candidate without adding it to prompt recall.

        ``reflect`` is intentionally direct for trusted operator input.
        ``propose_memory`` is the safer path for automated summaries,
        imported notes, and speculative engine reflections: the row is
        visible to ``alfred brain candidates`` and can later be promoted
        into a real lesson.
        """
        if not codename or not repo or not body:
            raise ValueError("propose_memory: codename, repo, and body are required")
        if severity not in ("info", "warning", "blocker"):
            raise ValueError(f"propose_memory: unknown severity {severity!r}")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("propose_memory: confidence must be between 0 and 1")
        candidate = MemoryCandidate(
            id=candidate_id or new_id(),
            codename=codename.strip(),
            repo=repo.strip(),
            body=body.strip(),
            tags=sorted({t.strip() for t in (tags or []) if t.strip()}),
            severity=severity,
            source=(source or "manual").strip(),
            source_firing_id=source_firing_id,
            evidence=evidence.strip(),
            confidence=float(confidence),
            status="candidate",
            created_at=created_at or datetime.now(UTC),
        )
        return self.store.insert_memory_candidate(candidate)

    def _lesson_provider(self, env: Mapping[str, str] | None = None) -> Any:
        """Build the Redis AMS provider, the promoted-lesson backend.

        Imported lazily to avoid an import cycle: the provider imports
        ``Lesson`` from this package.
        """
        from memory.redis_agent_memory import RedisAgentMemoryProvider

        return RedisAgentMemoryProvider.from_env(env=env)

    def promote_memory_candidate(
        self,
        candidate_id: str,
        *,
        reviewer: str = "operator",
        review_note: str = "",
        reviewed_at: datetime | None = None,
        lesson_writer: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Lesson:
        """Promote a candidate into a trusted lesson, written to Redis AMS.

        The candidate review queue, dedup index, and operational state stay in
        the local FleetBrain ledger; only the promoted LESSON moves, and it is
        written to Redis AMS, the semantic-recall backend.

        The AMS write happens FIRST and there is no local fallback: the
        candidate is flipped to ``validated`` only after the lesson is durably
        in AMS, so an unreachable AMS leaves the candidate ``candidate``
        (pending) and re-promotable rather than silently losing it. On an AMS
        write failure this raises :class:`MemoryPromotionError`.

        ``lesson_writer`` is the AMS provider (``reflect``-shaped, accepting a
        ``memory_id``); tests inject a stub. When omitted it is built from env.
        """
        candidate = self.store.get_memory_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"promote_memory_candidate: unknown candidate {candidate_id!r}")
        if candidate.status != "candidate":
            raise ValueError(
                f"promote_memory_candidate: candidate {candidate_id!r} is {candidate.status}"
            )

        # AMS write FIRST. No local fallback: if this fails the candidate stays
        # pending (no store update) and is re-promotable on a later run. Provider
        # CONSTRUCTION is inside the try too, so a bad AMS env value surfaces as a
        # retryable MemoryPromotionError (candidate stays pending, batch counts an
        # ams_write_error) rather than a raw exception / CLI traceback.
        try:
            if lesson_writer is None:
                lesson_writer = self._lesson_provider(env=env)
            lesson = lesson_writer.reflect(
                codename=candidate.codename,
                repo=candidate.repo,
                body=candidate.body,
                tags=candidate.tags,
                firing_id=candidate.source_firing_id,
                severity=candidate.severity,
                memory_id=_lesson_memory_id(candidate.id),
            )
        except Exception as exc:
            _LOG.exception(
                "promote_memory_candidate: AMS lesson write failed for "
                "candidate %s; leaving it pending",
                candidate_id,
            )
            raise MemoryPromotionError(
                f"promote_memory_candidate: AMS write failed for {candidate_id!r}"
            ) from exc

        # Lesson is durable in AMS -> flip the candidate to validated.
        self.store.update_memory_candidate(
            replace(
                candidate,
                status="validated",
                reviewed_at=reviewed_at or datetime.now(UTC),
                reviewed_by=reviewer.strip() or "operator",
                review_note=review_note.strip() or None,
                promoted_lesson_id=lesson.id,
            )
        )
        return lesson

    def reject_memory_candidate(
        self,
        candidate_id: str,
        *,
        reviewer: str = "operator",
        review_note: str = "",
        reviewed_at: datetime | None = None,
    ) -> MemoryCandidate:
        """Reject a candidate so it remains auditable but never enters recall."""
        candidate = self.store.get_memory_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"reject_memory_candidate: unknown candidate {candidate_id!r}")
        if candidate.status != "candidate":
            raise ValueError(
                f"reject_memory_candidate: candidate {candidate_id!r} is {candidate.status}"
            )
        updated = replace(
            candidate,
            status="rejected",
            reviewed_at=reviewed_at or datetime.now(UTC),
            reviewed_by=reviewer.strip() or "operator",
            review_note=review_note.strip() or None,
        )
        return self.store.update_memory_candidate(updated)

    def auto_promote_enabled(self, env: Mapping[str, str] | None = None) -> bool:
        """True unless explicitly disabled or kill-switched.

        Memory should learn autonomously: evidenced candidates reach the LLM
        judge by default and the judge decides whether to save. Operators can
        set ``ALFRED_AUTO_PROMOTE=0`` for a normal opt-out; malformed nonblank
        values fail closed too. ``ALFRED_AUTO_PROMOTE_KILL=1`` wins over
        everything so a bad batch can be halted without editing the rest of the
        deployment config."""
        env_src = self._auto_promote_env(env)
        return _auto_promote_switches_allow_learning(env_src)

    def _auto_promote_env(self, env: Mapping[str, str] | None = None) -> Mapping[str, str]:
        if env is not None:
            return env
        if self._env is not None:
            return self._env
        return _direct_auto_promote_env()

    def hold_candidate_for_review(
        self, candidate_id: str, *, note: str = ""
    ) -> MemoryCandidate | None:
        """Set a candidate aside for a human without promoting or rejecting it.

        The row keeps status ``candidate`` (so it stays in the review queue and
        the dedup index) but its review_note is stamped with the held marker so
        later auto-promote runs skip it. Returns None if the candidate is gone
        or already left the candidate state."""
        candidate = self.store.get_memory_candidate(candidate_id)
        if candidate is None or candidate.status != "candidate":
            return None
        held = f"{_AUTO_HELD_MARKER} {note}".strip()
        return self.store.update_memory_candidate(
            replace(
                candidate,
                reviewed_at=datetime.now(UTC),
                reviewed_by="auto",
                review_note=held[:500],
            )
        )

    def auto_promote_candidates(
        self,
        *,
        threshold: float | None = None,
        max_per_run: int | None = None,
        reviewer: str = "auto",
        judge: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Promote high-confidence, corroborated, non-conflicting candidates.

        Structural gate (every condition must hold):

          * the opt-out flag is not off and the kill-switch is off
            (``auto_promote_enabled``); otherwise this is a NO-OP that touches
            nothing and the manual queue is unchanged;
          * the candidate is still ``candidate`` and not already held for a
            human on a prior run;
          * the candidate carries evidence (no bare assertion auto-enters
            recall);
          * it does not conflict with another pending candidate that normalizes
            to the same body (two unreviewed versions => leave both for a
            human);
          * ``confidence >= threshold`` (default 0.5, env-tunable) -- a light
            pre-filter so any evidenced candidate reaches the judge, which is
            the real save/skip decision (autonomous LLM-driven capture+save).

        LLM judge (additive, default ON, gated behind
        ``ALFRED_AUTO_PROMOTE_LLM_JUDGE``): for each candidate that clears the
        structural gate, an LLM is asked whether the lesson is safe to save.
        The verdict shapes the outcome:

          * ``changes_agent_behavior`` => still AUTO-SAVED like any other safe
            verdict (the judge decides; the save is reversible), just recorded
            with a distinct note and counted under ``auto_saved_behavior_change``
            so the audit trail flags it. It no longer holds for a human;
          * ``is_duplicate``           => held for a human (dedup owns merging);
          * the judge confidence is taken as the LOWER of itself and the
            structural confidence (never a rescue), and a candidate that falls
            below the bar after that is held for a human;
          * FAIL-SOFT: any LLM error/timeout/parse/empty judgment leaves the
            candidate PENDING. A candidate is NEVER auto-saved on a failed or
            empty judgment, only on an explicit verdict that also clears the
            threshold. With the judge disabled, the heuristic alone gates.

        Promotions are capped per run (``max_per_run``) and recorded with
        ``reviewer="auto"`` so the whole batch stays auditable. ``judge`` is an
        injectable ``str -> str|None`` seam; tests pass a stub so no real model
        process is spawned. Returns a summary dict (always safe to log)."""
        env_src = self._auto_promote_env(env)
        summary: dict[str, Any] = {
            "enabled": self.auto_promote_enabled(env_src),
            "judge_enabled": False,
            "threshold": None,
            "cap": None,
            "considered": 0,
            "promoted": [],
            "skipped_low_confidence": 0,
            "skipped_no_evidence": 0,
            "skipped_conflict": 0,
            "skipped_duplicate": 0,
            "skipped_flagged": 0,
            "auto_saved_behavior_change": 0,
            # Kept at 0 for back-compat: behavior-changing verdicts are now
            # auto-saved (counted under ``auto_saved_behavior_change``) rather
            # than held, so nothing increments this any more.
            "flagged_behavior_change": 0,
            "held_low_confidence": 0,
            "judge_errors": 0,
            "judge_calls": 0,
            "judge_budget_exhausted": False,
            "ams_write_errors": 0,
        }
        if not summary["enabled"]:
            # No-op when explicitly disabled: do not even read the queue.
            return summary

        from memory_judge import judge_candidate, judge_enabled

        use_judge = judge_enabled(env_src)
        summary["judge_enabled"] = use_judge

        bar = (
            float(threshold)
            if threshold is not None
            else _env_float(
                "ALFRED_AUTO_PROMOTE_THRESHOLD", AUTO_PROMOTE_DEFAULT_THRESHOLD, env_src
            )
        )
        # The low default bar only makes sense because the LLM judge is the
        # real decider. With the judge off, raise the bar to a conservative
        # floor so default-confidence candidates are not blindly promoted with
        # no model or human review.
        if not use_judge:
            bar = max(
                bar,
                _env_float(
                    "ALFRED_AUTO_PROMOTE_NO_JUDGE_THRESHOLD",
                    AUTO_PROMOTE_NO_JUDGE_THRESHOLD,
                    env_src,
                ),
            )
        cap = (
            int(max_per_run)
            if max_per_run is not None
            else int(
                _env_float(
                    "ALFRED_AUTO_PROMOTE_MAX_PER_RUN",
                    AUTO_PROMOTE_DEFAULT_MAX_PER_RUN,
                    env_src,
                )
            )
        )
        # Per-run judge-call budget. The promotion ``cap`` only limits successful
        # promotions, but a rejected/duplicate/flagged row still costs a judge
        # call, so judging is bounded by this instead. Never below the promotion
        # cap (you must be able to judge enough to fill it).
        max_judge_calls = max(
            cap,
            int(
                _env_float(
                    "ALFRED_AUTO_PROMOTE_MAX_JUDGE_CALLS",
                    AUTO_PROMOTE_DEFAULT_MAX_JUDGE_CALLS,
                    env_src,
                )
            ),
        )
        summary["threshold"] = bar
        summary["cap"] = cap
        summary["max_judge_calls"] = max_judge_calls
        judge_calls = 0

        candidates = self.list_memory_candidates(status="candidate", limit=500)
        summary["considered"] = len(candidates)
        # Count normalized bodies so genuine conflicts (>1 unreviewed version)
        # are left for a human.
        seen: dict[str, int] = {}
        for cand in candidates:
            key = _auto_dedup_key(cand.body)
            seen[key] = seen.get(key, 0) + 1
        conflict_keys = {key for key, count in seen.items() if count > 1}

        promoted = 0
        for candidate in candidates:
            if promoted >= cap:
                break
            if (candidate.review_note or "").startswith(_AUTO_HELD_MARKER):
                # Already held for a human on a prior run; never reprocess.
                summary["skipped_flagged"] += 1
                continue
            if not (candidate.evidence or "").strip():
                summary["skipped_no_evidence"] += 1
                continue
            if _auto_dedup_key(candidate.body) in conflict_keys:
                summary["skipped_conflict"] += 1
                continue
            try:
                confidence = float(candidate.confidence)
            except (TypeError, ValueError):
                confidence = 0.0

            # Structural confidence is a prerequisite, and the judge can only
            # LOWER it (never rescue), so a below-bar candidate can never pass.
            # Skip it BEFORE spending a judge call so a queue of newer
            # low-confidence rows cannot exhaust the budget and starve older
            # promotable candidates.
            if confidence < bar:
                summary["skipped_low_confidence"] += 1
                continue

            note = f"auto-promoted (confidence={confidence:.3f} >= {bar:.3f})"
            if use_judge:
                if judge_calls >= max_judge_calls:
                    # Spent the per-run judge budget. Stop here so the run stays
                    # bounded; remaining rows are picked up next run.
                    summary["judge_budget_exhausted"] = True
                    break
                judge_calls += 1
                verdict = judge_candidate(
                    topic=(candidate.body or "").split("\n", 1)[0][:200],
                    body=candidate.body or "",
                    evidence=candidate.evidence or "",
                    judge=judge,
                    env=env_src,
                )
                if verdict is None:
                    # FAIL-SOFT: a failed/empty/unparseable judgment must NEVER
                    # auto-promote. Leave the candidate pending for the human.
                    summary["judge_errors"] += 1
                    continue
                if verdict.is_duplicate:
                    # Hold (not reject): a rejected row drops out of the dedup
                    # index, so the next harvest would re-propose, re-create, and
                    # re-judge the same lesson. Held keeps it in the index while
                    # keeping it out of the re-judge loop.
                    self.hold_candidate_for_review(
                        candidate.id,
                        note=f"LLM judge: duplicate {verdict.rationale}".strip(),
                    )
                    summary["skipped_duplicate"] += 1
                    continue
                # Safe verdict. Take the LOWER of structural and judge
                # confidence so a high judge score can never lift a candidate
                # that failed the structural bar.
                confidence = min(confidence, verdict.confidence)
                if confidence < bar:
                    # The judge lowered confidence under the bar. Unlike a purely
                    # structural skip (which leaves the row pending for the next
                    # run), this row was JUDGED and is HELD for a human, so count
                    # it as a hold, not a transient low-confidence skip.
                    self.hold_candidate_for_review(
                        candidate.id,
                        note=(f"LLM judge confidence {confidence:.3f} < {bar:.3f}"),
                    )
                    summary["held_low_confidence"] += 1
                    continue
                if verdict.changes_agent_behavior:
                    # Behavior-changing but otherwise safe and above the bar:
                    # AUTO-SAVE it (the judge decided; every auto-save is
                    # reversible) with a distinct note so the audit trail flags
                    # it. Counted separately from ordinary saves.
                    summary["auto_saved_behavior_change"] += 1
                    note = (
                        f"auto-saved (behavior-changing; structural + LLM judge "
                        f"confidence={confidence:.3f} >= {bar:.3f})"
                    )
                else:
                    note = (
                        f"auto-promoted (structural + LLM judge "
                        f"confidence={confidence:.3f} >= {bar:.3f})"
                    )

            try:
                self.promote_memory_candidate(
                    candidate.id,
                    reviewer=reviewer,
                    review_note=note,
                    env=env_src,
                )
            except ValueError:
                # The candidate changed under us (already promoted/rejected by a
                # concurrent reviewer). Skip without counting it.
                continue
            except MemoryPromotionError:
                # The AMS write failed: the candidate is left pending (no local
                # fallback, no silent loss) and will be retried on a later run.
                summary["ams_write_errors"] = summary.get("ams_write_errors", 0) + 1
                continue
            promoted += 1
            summary["promoted"].append(candidate.id)

        summary["judge_calls"] = judge_calls
        return summary

    def revert_auto_promotions(
        self,
        *,
        reviewer: str = "auto-revert",
        note: str = "",
        lesson_forgetter: Any | None = None,
    ) -> list[str]:
        """Forget every auto-promoted lesson from Redis AMS and reopen it.

        The reversal lever the auto-promotion guardrails promise: forgets each
        auto-promoted lesson from Redis AMS (the promoted-lesson backend) and
        flips its candidate back to ``candidate`` so the operator can
        re-review. Auto-promotions are the validated candidates the auto-promoter
        wrote (``reviewed_by == "auto"`` with a recorded ``promoted_lesson_id``).

        A candidate is reopened ONLY once its lesson is actually forgotten from
        AMS: if the forget fails (a transient outage, or forgetting disabled
        server-side) the candidate is left validated and logged, so the local
        ledger never claims a revert while the lesson is still live in AMS
        recall. The sweep paginates (reverting flips a candidate out of the
        validated set) so it drains more than one page. ``lesson_forgetter`` is
        the AMS provider; tests inject a stub. Returns the candidate ids that
        were actually reverted.
        """
        reverted: list[str] = []
        forget_failed: set[str] = set()
        if lesson_forgetter is None:
            lesson_forgetter = self._lesson_provider()
        # Phase 1: enumerate EVERY validated auto-promotion via offset paging.
        # Reading is non-mutating, so offsets stay stable and a newest page full
        # of human-reviewed or undeletable rows cannot hide older auto-promotions
        # (the bug a "loop until the set shrinks" approach had).
        targets: list[MemoryCandidate] = []
        page = 500
        offset = 0
        while True:
            batch = self.list_memory_candidates(status="validated", limit=page, offset=offset)
            targets.extend(
                cand
                for cand in batch
                if cand.reviewed_by == "auto" and cand.promoted_lesson_id is not None
            )
            if len(batch) < page:
                break
            offset += page
        # Phase 2: forget then reopen each, reopening ONLY when the lesson is
        # actually gone so the ledger never records a revert while the lesson is
        # still live in AMS recall.
        for candidate in targets:
            cid = candidate.id
            forgotten = True
            if lesson_forgetter is not None:
                try:
                    forgotten = bool(lesson_forgetter.forget_lesson(_lesson_memory_id(cid)))
                except Exception:
                    _LOG.exception(
                        "revert_auto_promotions: AMS forget failed for candidate %s",
                        cid,
                    )
                    forgotten = False
            if not forgotten:
                forget_failed.add(cid)
                continue
            self.store.update_memory_candidate(
                replace(
                    candidate,
                    status="candidate",
                    reviewed_at=datetime.now(UTC),
                    reviewed_by=reviewer.strip() or "auto-revert",
                    review_note=note.strip() or None,
                    promoted_lesson_id=None,
                )
            )
            reverted.append(cid)
        if forget_failed:
            _LOG.warning(
                "revert_auto_promotions: left %d candidate(s) validated because the "
                "AMS lesson could not be forgotten: %s",
                len(forget_failed),
                ", ".join(sorted(forget_failed)),
            )
        return reverted

    def record_failure(
        self,
        *,
        codename: str,
        subtype: str,
        summary: str,
        repo: str | None = None,
        firing_id: str | None = None,
        engine: str | None = None,
        severity: Severity = "warning",
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> FailureEvent:
        """Persist a normalized non-success event for later diagnosis."""
        if not codename or not subtype:
            raise ValueError("record_failure: codename and subtype are required")
        if severity not in ("info", "warning", "blocker"):
            raise ValueError(f"record_failure: unknown severity {severity!r}")
        event = FailureEvent(
            id=event_id or new_id(),
            codename=codename.strip(),
            repo=repo.strip() if repo else None,
            firing_id=firing_id,
            subtype=subtype.strip(),
            summary=(summary or "").strip(),
            engine=engine.strip() if engine else None,
            severity=severity,
            created_at=created_at or datetime.now(UTC),
        )
        return self.store.insert_failure_event(event)

    def upsert_github_item(
        self,
        *,
        repo: str,
        number: int,
        kind: GitHubItemKind,
        state: GitHubItemState,
        title: str = "",
        url: str = "",
        labels: Iterable[str] | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        closed_at: datetime | None = None,
        merged_at: datetime | None = None,
        head_ref: str | None = None,
        base_ref: str | None = None,
        bundle_slug: str | None = None,
        changed_files: int | None = None,
        additions: int | None = None,
        deletions: int | None = None,
    ) -> GitHubItem:
        """Cache one GitHub issue or PR row.

        The poller is deliberately pull-based and idempotent: every run
        replaces the cached row for ``repo#number`` / ``kind`` with the
        latest shape it saw.
        """
        if not repo or not int(number):
            raise ValueError("upsert_github_item: repo and number are required")
        if kind not in ("issue", "pr"):
            raise ValueError(f"upsert_github_item: unknown kind {kind!r}")
        if state not in ("open", "closed", "merged", "unknown"):
            raise ValueError(f"upsert_github_item: unknown state {state!r}")
        now = datetime.now(UTC)
        clean_labels = sorted(
            {str(label).strip() for label in (labels or []) if str(label).strip()}
        )
        resolved_bundle = (bundle_slug or "").strip() or _bundle_slug_from_labels(clean_labels)
        item = GitHubItem(
            id=f"{repo}#{int(number)}:{kind}",
            repo=repo.strip(),
            number=int(number),
            kind=kind,
            state=state,
            title=(title or "").strip(),
            url=(url or "").strip(),
            labels=clean_labels,
            created_at=created_at,
            updated_at=updated_at or now,
            last_seen_at=last_seen_at or now,
            closed_at=closed_at,
            merged_at=merged_at,
            head_ref=head_ref,
            base_ref=base_ref,
            bundle_slug=resolved_bundle,
            changed_files=max(0, int(changed_files)) if changed_files is not None else None,
            additions=max(0, int(additions)) if additions is not None else None,
            deletions=max(0, int(deletions)) if deletions is not None else None,
        )
        persisted = self.store.upsert_github_item(item)
        if persisted.bundle_slug:
            self.store.upsert_bundle_item(
                BundleItem(
                    id=f"{persisted.bundle_slug}:{persisted.repo}#{persisted.number}:{persisted.kind}",
                    bundle_slug=persisted.bundle_slug,
                    repo=persisted.repo,
                    item_kind=persisted.kind,
                    number=persisted.number,
                    state=persisted.state,
                    title=persisted.title,
                    url=persisted.url,
                    labels=persisted.labels,
                    updated_at=persisted.updated_at,
                    last_seen_at=persisted.last_seen_at,
                )
            )
        return persisted

    def upsert_bundle_item(
        self,
        *,
        bundle_slug: str,
        repo: str,
        item_kind: GitHubItemKind,
        number: int,
        state: GitHubItemState,
        title: str = "",
        url: str = "",
        labels: Iterable[str] | None = None,
        updated_at: datetime | None = None,
        last_seen_at: datetime | None = None,
    ) -> BundleItem:
        """Upsert bundle membership without requiring a full GitHub row."""
        if not bundle_slug or not repo or not int(number):
            raise ValueError("upsert_bundle_item: bundle_slug, repo, and number are required")
        now = datetime.now(UTC)
        item = BundleItem(
            id=f"{bundle_slug}:{repo}#{int(number)}:{item_kind}",
            bundle_slug=bundle_slug.strip(),
            repo=repo.strip(),
            item_kind=item_kind,
            number=int(number),
            state=state,
            title=(title or "").strip(),
            url=(url or "").strip(),
            labels=sorted({str(label).strip() for label in (labels or []) if str(label).strip()}),
            updated_at=updated_at or now,
            last_seen_at=last_seen_at or now,
        )
        return self.store.upsert_bundle_item(item)

    def upsert_worker_heartbeat(
        self,
        *,
        codename: str,
        firing_id: str,
        status: WorkerStatus = "running",
        started_at: datetime | None = None,
        heartbeat_at: datetime | None = None,
        repo: str | None = None,
        pid: int | None = None,
        detail: str = "",
    ) -> WorkerHeartbeat:
        """Record the latest liveness signal for one worker firing."""
        if not codename or not firing_id:
            raise ValueError("upsert_worker_heartbeat: codename and firing_id are required")
        if status not in ("running", "ok", "failed", "stale", "cancelled"):
            raise ValueError(f"upsert_worker_heartbeat: unknown status {status!r}")
        now = datetime.now(UTC)
        heartbeat = WorkerHeartbeat(
            id=f"{codename.strip()}:{firing_id.strip()}",
            codename=codename.strip(),
            firing_id=firing_id.strip(),
            status=status,
            started_at=started_at or now,
            heartbeat_at=heartbeat_at or now,
            repo=repo.strip() if repo else None,
            pid=int(pid) if pid is not None else None,
            detail=(detail or "").strip(),
        )
        return self.store.upsert_worker_heartbeat(heartbeat)

    # ----- read paths ---------------------------------------------------

    def recall(
        self,
        codename: str | None = None,
        repo: str | None = None,
        query: str | None = None,
        *,
        limit: int = _RECALL_DEFAULT,
    ) -> list[Lesson]:
        """Return the most-recent-first lessons matching the filters.

        Calling shape mirrors the prompt-prepend pattern: the runner
        does ``brain.recall(codename, repo)`` and dumps the bodies
        into the firing's system prompt.
        """
        clamped = max(1, min(int(limit), _RECALL_MAX))
        return self.store.recall_lessons(
            codename=codename,
            repo=repo,
            query=query,
            limit=clamped,
        )

    def get_repo_note(self, repo: str) -> RepoNote | None:
        return self.store.get_repo_note(repo)

    def list_lessons(self, limit: int | None = None) -> list[Lesson]:
        return self.store.list_lessons(limit=limit)

    def list_firings(
        self,
        codename: str | None = None,
        status: FiringStatus | None = None,
        limit: int = 50,
    ) -> list[FiringLog]:
        return self.store.list_firing_logs(codename=codename, status=status, limit=limit)

    def list_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> list[FileTouch]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_file_touches(
            repo=repo,
            codename=codename,
            path=path,
            limit=clamped,
        )

    def count_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
        touched_since: datetime | None = None,
    ) -> int:
        """Exact COUNT(*) of file_touches, unbounded by the list 500-row cap.

        ``list_file_touches`` clamps ``limit`` to 500, so callers that need a
        true total (e.g. proof-telemetry's lifetime counts) must use this rather
        than ``len(list_...())``, which silently freezes at 500 on a busy brain.
        """
        return self.store.count_file_touches(
            repo=repo,
            codename=codename,
            path=path,
            touched_since=touched_since,
        )

    def list_memory_candidates(
        self,
        status: MemoryCandidateStatus | None = "candidate",
        repo: str | None = None,
        codename: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryCandidate]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_memory_candidates(
            status=status,
            repo=repo,
            codename=codename,
            limit=clamped,
            offset=max(0, int(offset)),
        )

    def list_failures(
        self,
        repo: str | None = None,
        codename: str | None = None,
        subtype: str | None = None,
        limit: int = 50,
    ) -> list[FailureEvent]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_failure_events(
            repo=repo,
            codename=codename,
            subtype=subtype,
            limit=clamped,
        )

    def list_github_items(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        limit: int = 50,
    ) -> list[GitHubItem]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_github_items(
            repo=repo,
            kind=kind,
            state=state,
            bundle_slug=bundle_slug,
            limit=clamped,
        )

    def count_github_items(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
        created_since: datetime | None = None,
        closed_since: datetime | None = None,
        merged_since: datetime | None = None,
        updated_since: datetime | None = None,
    ) -> int:
        """Exact COUNT(*) of github_items, unbounded by the list 500-row cap.

        ``list_github_items`` clamps ``limit`` to 500, so any caller needing a
        true total (proof-telemetry's lifetime PR counts) must use this. Counting
        by paginating ``list_github_items`` can never exceed 500 because the list
        method re-clamps every request.

        ``authored_only=True`` restricts the count to agent-authored PRs/issues:
        rows carrying the ``agent:authored`` provenance label or pushed from an
        agent branch prefix. The poller stores EVERY PR from ``gh pr list`` (not
        just Alfred's), so proof-telemetry passes this to avoid claiming PRs the
        fleet did not open. The filter is a SQL predicate on already-stored
        columns, so it stays an exact COUNT(*).

        ``agent_labeled_only=True`` restricts the count to rows with any
        ``agent:*`` label. Proof telemetry uses this for issue counts, where the
        public signal is the issue label rather than a branch name.
        """
        return self.store.count_github_items(
            repo=repo,
            kind=kind,
            state=state,
            bundle_slug=bundle_slug,
            authored_only=authored_only,
            agent_labeled_only=agent_labeled_only,
            created_since=created_since,
            closed_since=closed_since,
            merged_since=merged_since,
            updated_since=updated_since,
        )

    def sum_github_changed_lines(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
        created_since: datetime | None = None,
        closed_since: datetime | None = None,
        merged_since: datetime | None = None,
        updated_since: datetime | None = None,
    ) -> int:
        """Sum additions + deletions from cached GitHub PR rows.

        Proof telemetry uses this with ``kind="pr"`` and
        ``authored_only=True`` so the line-count metric is anchored to the same
        Alfred-authored PR subset as the PR counters.
        """
        return self.store.sum_github_changed_lines(
            repo=repo,
            kind=kind,
            state=state,
            bundle_slug=bundle_slug,
            authored_only=authored_only,
            agent_labeled_only=agent_labeled_only,
            created_since=created_since,
            closed_since=closed_since,
            merged_since=merged_since,
            updated_since=updated_since,
        )

    def sum_github_changed_files(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
        created_since: datetime | None = None,
        closed_since: datetime | None = None,
        merged_since: datetime | None = None,
        updated_since: datetime | None = None,
    ) -> int:
        """Sum changed-file counts from cached GitHub PR rows."""
        return self.store.sum_github_changed_files(
            repo=repo,
            kind=kind,
            state=state,
            bundle_slug=bundle_slug,
            authored_only=authored_only,
            agent_labeled_only=agent_labeled_only,
            created_since=created_since,
            closed_since=closed_since,
            merged_since=merged_since,
            updated_since=updated_since,
        )

    def list_bundle_items(
        self,
        bundle_slug: str | None = None,
        state: GitHubItemState | None = None,
        limit: int = 50,
    ) -> list[BundleItem]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_bundle_items(bundle_slug=bundle_slug, state=state, limit=clamped)

    def list_worker_heartbeats(
        self,
        codename: str | None = None,
        status: WorkerStatus | None = None,
        limit: int = 50,
    ) -> list[WorkerHeartbeat]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_worker_heartbeats(
            codename=codename,
            status=status,
            limit=clamped,
        )

    def list_stale_workers(self, *, max_age_minutes: int = 60) -> list[WorkerHeartbeat]:
        """Return running worker heartbeats older than ``max_age_minutes``."""
        cutoff = datetime.now(UTC) - timedelta(minutes=max(1, int(max_age_minutes)))
        return [
            hb
            for hb in self.list_worker_heartbeats(status="running", limit=500)
            if hb.heartbeat_at < cutoff
        ]

    def list_failure_patterns(
        self,
        *,
        repo: str | None = None,
        codename: str | None = None,
        window_days: int = 7,
        min_count: int = 2,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Group repeated failures and attach a suggested operator action.

        This is the "reliability governor" read path. It does not mutate
        fleet state. The goal is to turn repeated Slack-style error noise
        into a small queue of concrete next actions.
        """
        cutoff = datetime.now(UTC) - timedelta(days=max(1, int(window_days)))
        grouped: dict[tuple[str, str, str, str], list[FailureEvent]] = {}
        for failure in self.list_failures(repo=repo, codename=codename, limit=500):
            if failure.created_at < cutoff:
                continue
            key = (
                failure.codename,
                failure.repo or "",
                failure.subtype or "unknown",
                failure.engine or "",
            )
            grouped.setdefault(key, []).append(failure)

        patterns: list[dict[str, Any]] = []
        threshold = max(1, int(min_count))
        for (agent, failure_repo, subtype, engine), rows in grouped.items():
            if len(rows) < threshold:
                continue
            rows.sort(key=lambda item: item.created_at)
            latest = rows[-1]
            if _is_non_actionable_failure_pattern(subtype, latest.summary):
                continue
            classification = _classify_failure_pattern(subtype, latest.summary)
            action = _suggest_failure_action(
                classification=classification,
                codename=agent,
                count=len(rows),
            )
            severity = "blocker" if action in {"pause_agent", "file_setup_issue"} else "warning"
            patterns.append(
                {
                    "key": "|".join([agent, failure_repo or "-", subtype, engine or "-"]),
                    "codename": agent,
                    "repo": failure_repo or None,
                    "subtype": subtype,
                    "engine": engine or None,
                    "count": len(rows),
                    "first_seen": rows[0].created_at.isoformat(),
                    "last_seen": latest.created_at.isoformat(),
                    "latest_summary": latest.summary,
                    "classification": classification,
                    "suggested_action": action,
                    "severity": severity,
                    "evidence_ids": [row.id for row in rows[-5:]],
                }
            )
        patterns.sort(
            key=lambda item: (
                item["severity"] != "blocker",
                -int(item["count"]),
                str(item["last_seen"]),
            )
        )
        return patterns[: max(1, min(int(limit), 100))]

    def suggest_memory_promotions(
        self,
        *,
        min_confidence: float = 0.75,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return reviewable candidates that look safe to promote.

        This is intentionally advisory. Alfred still keeps the human
        promotion step unless an operator explicitly scripts around it.
        """
        rows = self.list_memory_candidates(status="candidate", limit=500)
        suggestions: list[dict[str, Any]] = []
        trusted_bodies = {
            (lesson.repo, _canonical_memory_body(lesson.body)) for lesson in self.list_lessons()
        }
        for candidate in rows:
            canonical = _canonical_memory_body(candidate.body)
            if (candidate.repo, canonical) in trusted_bodies:
                continue
            score = float(candidate.confidence)
            reasons: list[str] = []
            if candidate.confidence >= min_confidence:
                reasons.append(f"confidence {candidate.confidence:.2f}")
            if candidate.evidence:
                score += 0.08
                reasons.append("has evidence")
            if candidate.tags:
                score += 0.03
                reasons.append("tagged")
            if candidate.severity in {"warning", "blocker"}:
                score += 0.04
                reasons.append(f"severity {candidate.severity}")
            if not reasons or score < min_confidence:
                continue
            suggestions.append(
                {
                    "candidate_id": candidate.id,
                    "codename": candidate.codename,
                    "repo": candidate.repo,
                    "body": candidate.body,
                    "score": round(min(score, 1.0), 3),
                    "reasons": reasons,
                }
            )
        suggestions.sort(key=lambda item: float(item["score"]), reverse=True)
        return suggestions[: max(1, min(int(limit), 100))]

    def reliability_report(
        self,
        *,
        window_days: int = 7,
        failure_min_count: int = 2,
        stale_worker_minutes: int = 60,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Return the operator-facing reliability governor report."""
        patterns = self.list_failure_patterns(
            window_days=window_days,
            min_count=failure_min_count,
            limit=limit,
        )
        stale_workers = self.list_stale_workers(max_age_minutes=stale_worker_minutes)
        promotions = self.suggest_memory_promotions(limit=limit)
        actions: list[dict[str, Any]] = []
        for pattern in patterns:
            actions.append(
                {
                    "kind": "failure_pattern",
                    "severity": pattern["severity"],
                    "action": pattern["suggested_action"],
                    "summary": _failure_action_summary(pattern),
                    "target": pattern["codename"],
                    "evidence": pattern["evidence_ids"],
                }
            )
        for worker in stale_workers[:limit]:
            actions.append(
                {
                    "kind": "stale_worker",
                    "severity": "warning",
                    "action": "inspect_worker",
                    "summary": (
                        f"{worker.codename} firing {worker.firing_id} has not "
                        f"sent a heartbeat recently"
                    ),
                    "target": worker.codename,
                    "evidence": [worker.id],
                }
            )
        if promotions:
            actions.append(
                {
                    "kind": "memory_promotion",
                    "severity": "info",
                    "action": "review_memory",
                    "summary": f"{len(promotions)} memory candidate(s) look promotable",
                    "target": None,
                    "evidence": [str(item["candidate_id"]) for item in promotions[:limit]],
                }
            )

        status = "ok"
        if any(item["severity"] == "blocker" for item in actions):
            status = "fail"
        elif actions:
            status = "warn"
        return {
            "status": status,
            "checked_at": datetime.now(UTC).isoformat(),
            "window_days": max(1, int(window_days)),
            "failure_min_count": max(1, int(failure_min_count)),
            "failure_patterns": patterns,
            "stale_workers": [_serialize(asdict(worker)) for worker in stale_workers[:limit]],
            "promotion_suggestions": promotions,
            "actions": actions,
        }

    def stats(self) -> dict[str, int]:
        return self.store.stats()

    def health(self) -> dict[str, Any]:
        """Return a cheap liveness check for local API callers.

        ``doctor`` is the deeper operational report and can legitimately
        return warnings for a fresh install with no GitHub poll data or seed
        memories yet. ``health`` only answers whether the local ledger is
        reachable and schema-backed, which is what the native client's memory
        API needs before listing empty candidates/lessons on first run.
        """
        return {
            "ok": True,
            "status": "ok",
            "checked_at": datetime.now(UTC).isoformat(),
            "stats": self.stats(),
        }

    def doctor(self) -> dict[str, Any]:
        """Return a read-only health report for the memory store."""
        from .schema import SCHEMA_VERSION

        stats = self.stats()
        checks: list[dict[str, str]] = []

        def check(name: str, status: str, detail: str) -> None:
            checks.append({"name": name, "status": status, "detail": detail})

        check("schema", "ok", f"expected schema v{SCHEMA_VERSION}")
        open_candidates = stats.get("memory_candidates_open", 0)
        if open_candidates > 100:
            check("candidate_backlog", "fail", f"{open_candidates} candidates need review")
        elif open_candidates > 20:
            check("candidate_backlog", "warn", f"{open_candidates} candidates need review")
        else:
            check("candidate_backlog", "ok", f"{open_candidates} open candidates")

        recent_failures = self.list_failures(limit=20)
        blocker_failures = [F for F in recent_failures if F.severity == "blocker"]
        if blocker_failures:
            check("recent_failures", "fail", f"{len(blocker_failures)} blocker failure(s)")
        elif recent_failures:
            check("recent_failures", "warn", f"{len(recent_failures)} recorded failure(s)")
        else:
            check("recent_failures", "ok", "no recorded failures")

        stale_workers = self.list_stale_workers(max_age_minutes=60)
        if stale_workers:
            check("stale_workers", "warn", f"{len(stale_workers)} running worker(s) look stale")
        else:
            check("stale_workers", "ok", f"{stats.get('workers_running', 0)} running worker(s)")

        github_items = stats.get("github_items", 0)
        if github_items:
            check("github_poll", "ok", f"{github_items} cached GitHub issue/PR item(s)")
        else:
            check("github_poll", "warn", "no cached GitHub poll data yet")

        bundle_items = stats.get("bundle_items", 0)
        check("bundles", "ok", f"{bundle_items} cached bundle item(s)")

        suggestions = self.suggest_memory_promotions(limit=5)
        if suggestions:
            check("promotion_loop", "warn", f"{len(suggestions)} candidate(s) look promotable")
        else:
            check("promotion_loop", "ok", "no high-confidence candidates waiting")

        patterns = self.list_failure_patterns(limit=5)
        blocker_patterns = [p for p in patterns if p["severity"] == "blocker"]
        if blocker_patterns:
            check(
                "reliability_governor",
                "fail",
                f"{len(blocker_patterns)} repeated blocker failure pattern(s)",
            )
        elif patterns:
            check("reliability_governor", "warn", f"{len(patterns)} repeated pattern(s)")
        else:
            check("reliability_governor", "ok", "no repeated failure patterns")

        if stats.get("lessons", 0) == 0 and open_candidates == 0:
            check("recall_seed", "warn", "no trusted lessons or candidates yet")
        else:
            check("recall_seed", "ok", "memory has seed data")

        status = "ok"
        if any(c["status"] == "fail" for c in checks):
            status = "fail"
        elif any(c["status"] == "warn" for c in checks):
            status = "warn"
        return {
            "status": status,
            "checked_at": datetime.now(UTC).isoformat(),
            "stats": stats,
            "checks": checks,
        }

    # ----- delete paths -------------------------------------------------

    def forget(self, lesson_id: str) -> bool:
        """Delete a single lesson by id. Returns True if it existed."""
        return self.store.delete_lesson(lesson_id)

    def forget_before(self, *, days: int | None = None, before: datetime | None = None) -> int:
        """GC lessons older than ``days`` (or older than ``before``).

        Pass exactly one of ``days`` or ``before``.
        """
        if (days is None) == (before is None):
            raise ValueError("forget_before: pass exactly one of days= or before=")
        cutoff = before
        if cutoff is None:
            assert days is not None  # for mypy
            cutoff = datetime.now(UTC) - timedelta(days=int(days))
        return self.store.delete_lessons_before(cutoff)

    # ----- export -------------------------------------------------------

    def export(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the entire brain.

        Format::

            {
              "schema_version": 3,
              "exported_at": "2026-05-23T...Z",
              "lessons": [{...}, ...],
              "repo_notes": [{...}, ...],
              "firings": [{...}, ...],
              "file_touches": [{...}, ...],
              "memory_candidates": [{...}, ...],
              "failure_events": [{...}, ...]
            }

        ``alfred brain export`` writes this to disk. Restoring is
        currently manual: re-run reflect/firing_log/note_repo on the
        target host.
        """
        from .schema import SCHEMA_VERSION

        return {
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "lessons": [_serialize(asdict(L)) for L in self.list_lessons()],
            "repo_notes": [_serialize(asdict(n)) for n in self._all_repo_notes()],
            "firings": [_serialize(asdict(F)) for F in self.list_firings(limit=10_000)],
            "file_touches": [_serialize(asdict(T)) for T in self.list_file_touches(limit=10_000)],
            "memory_candidates": [
                _serialize(asdict(C))
                for C in self.list_memory_candidates(status=None, limit=10_000)
            ],
            "failure_events": [_serialize(asdict(F)) for F in self.list_failures(limit=10_000)],
            "github_items": [_serialize(asdict(G)) for G in self.list_github_items(limit=10_000)],
            "bundle_items": [_serialize(asdict(B)) for B in self.list_bundle_items(limit=10_000)],
            "worker_heartbeats": [
                _serialize(asdict(H)) for H in self.list_worker_heartbeats(limit=10_000)
            ],
        }

    def _all_repo_notes(self) -> list[RepoNote]:
        """Pull every repo note via a list_lessons-style sweep.

        The store doesn't expose a list method for notes today (the
        operator queries by repo); export needs everything, so we
        derive the repo set from existing lessons + any note we have.
        For now we use the lessons table as the source of repo keys.
        """
        seen: set[str] = set()
        out: list[RepoNote] = []
        for L in self.list_lessons():
            if L.repo in seen:
                continue
            seen.add(L.repo)
            note = self.store.get_repo_note(L.repo)
            if note is not None:
                out.append(note)
        return out


def _serialize(d: dict[str, Any]) -> dict[str, Any]:
    """Best-effort JSON serialization: datetime -> ISO, everything else
    passes through. Used for export only."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(UTC).isoformat()
        else:
            out[k] = v
    return out


def _classify_failure_pattern(subtype: str, summary: str) -> str:
    text = f"{subtype} {summary}".lower()
    if any(token in text for token in ("executable doesn't exist", "playwright", "chromium")):
        return "local_setup"
    if any(token in text for token in ("auth", "token", "sso", "accessdenied", "permission")):
        return "auth"
    if any(token in text for token in ("rate_limit", "quota", "budget", "too many requests")):
        return "provider_limit"
    if any(token in text for token in ("timeout", "timed out", "error_timeout")):
        return "timeout"
    if any(token in text for token in ("no-commit", "no commit", "wip", "salvage")):
        return "agent_quality"
    return "unknown"


def _is_non_actionable_failure_pattern(subtype: str, summary: str) -> bool:
    normalized = str(subtype or "").strip().lower()
    if normalized in _NON_ACTIONABLE_FAILURE_SUBTYPES:
        return True
    text = f"{normalized} {summary or ''}".lower()
    if any(token in text for token in ("error", "fail", "timeout", "blocked", "crash")):
        return False
    return normalized.endswith("-cap")


def _suggest_failure_action(*, classification: str, codename: str, count: int) -> str:
    if classification == "local_setup":
        return "file_setup_issue"
    if classification == "auth":
        return "ask_human"
    if classification == "provider_limit":
        return "retry_later"
    if classification == "agent_quality":
        return "review_prompt_or_checks"
    if classification == "timeout" and count >= 3:
        return "pause_agent"
    if classification == "timeout":
        return "retry_later"
    if count >= 3:
        return "pause_agent"
    return "inspect"


def _failure_action_summary(pattern: dict[str, Any]) -> str:
    repo = f" on {pattern['repo']}" if pattern.get("repo") else ""
    return (
        f"{pattern['codename']} has {pattern['count']} repeated "
        f"{pattern['classification']} failure(s){repo}: "
        f"{pattern['suggested_action']}"
    )


def _canonical_memory_body(body: str) -> str:
    return " ".join((body or "").strip().lower().split())


def _bundle_slug_from_labels(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith("agent:bundle:"):
            return label.removeprefix("agent:bundle:").strip() or None
        if label.startswith("bundle:"):
            return label.removeprefix("bundle:").strip() or None
    return None
