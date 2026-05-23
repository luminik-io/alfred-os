"""Spec-bundle planner primitives for the ``damian`` codename role.

``damian`` is Alfred's spec-aware, multi-repo bundle planner. It sits one
level above ``drake`` (single-repo issue filer) and feeds ``batman``
(cross-repo bundle coordinator) by emitting ``agent:bundle:<slug>``
siblings across two or more configured repos.

This module is the pure-data layer: spec parsing, multi-repo detection,
bundle-shape construction. The runner shell in ``bin/damian.py`` wires
preflight, the LLM call, and the sentinel-driven reporting around these
primitives. Keeping the logic here means a fleet can dry-run the planner
without touching GitHub or any LLM.

Design notes:

- **Open-Closed**: spec discovery and parsing are pluggable via the
  ``SpecParser`` Protocol. The default ``MarkdownSpecParser`` reads the
  short Alfred-recommended spec shape from ``docs/SPECS_DRIVEN_DEVELOPMENT``;
  a fleet can ship its own parser that reads frontmatter, YAML, or a
  custom format and swap it in via the ``parser=`` argument.
- **Dependency Inversion**: the planner accepts a callable ``gh_client``
  for any GitHub queries it makes so tests can pass a fake without
  monkeypatching ``subprocess``.
- **12-factor**: configuration (the repo scan list, the spec directory)
  is read from the environment via ``PlannerConfig.from_env()``. Nothing
  is hidden in module state.
- **DRY**: bundle-label constants come from ``lib.labels`` when available
  (the label-state port will introduce it); falls back to the existing
  constants in ``lib.batman`` so the module is usable today.

The planner does not call gh, claude, or codex itself. It produces a
``Plan`` — a list of ``SpecBundle`` candidates — that ``bin/damian.py``
formats into the LLM prompt context. The LLM does the final filing via
``gh issue create``; this layer keeps the candidate-list logic pure.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Pull bundle-label constants from the canonical source. ``lib.labels`` is
# the target site once the label-state port lands; until then ``lib.batman``
# already owns the same constants and ships with this repo.
try:  # pragma: no cover - import wiring only
    # labels.py canonicalises this as ``LARGE_FEATURE``; batman.py kept the
    # earlier ``LARGE_FEATURE_LABEL`` name and is the fallback path below.
    # Aliasing keeps the rest of this module readable under either import.
    from labels import BUNDLE_LABEL_PREFIX
    from labels import LARGE_FEATURE as LARGE_FEATURE_LABEL
except ImportError:  # pragma: no cover - fallback path
    from batman import BUNDLE_LABEL_PREFIX, LARGE_FEATURE_LABEL

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerConfig:
    """Operator-supplied configuration for one damian firing.

    All knobs are env-driven so a launchd / systemd unit can configure
    the planner without a config file:

    - ``DAMIAN_SCAN_REPOS``: comma-separated repo slugs the planner is
      allowed to file bundles into. Empty means the planner exits as a
      no-op (no implicit fallback to a hardcoded list).
    - ``DAMIAN_SPEC_DIR``: directory the default markdown parser walks
      to discover specs. Resolved relative to ``WORKSPACE_ROOT`` when
      relative, used as-is when absolute.
    - ``DAMIAN_DAILY_BUNDLE_CAP``: max bundles the planner may emit per
      firing. Read by the runner; surfaced here so dry-runs match the
      runtime ceiling.
    """

    scan_repos: tuple[str, ...]
    spec_dir: Path | None
    daily_bundle_cap: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PlannerConfig:
        environ = env if env is not None else os.environ
        raw_repos = (environ.get("DAMIAN_SCAN_REPOS") or "").strip()
        repos = tuple(token.strip() for token in raw_repos.split(",") if token.strip())

        raw_spec = (environ.get("DAMIAN_SPEC_DIR") or "").strip()
        spec_dir: Path | None = Path(raw_spec).expanduser() if raw_spec else None

        cap_raw = (environ.get("DAMIAN_DAILY_BUNDLE_CAP") or "").strip()
        try:
            cap = int(cap_raw) if cap_raw else 3
        except ValueError:
            cap = 3
        cap = max(1, cap)
        return cls(scan_repos=repos, spec_dir=spec_dir, daily_bundle_cap=cap)


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class BundleChild:
    """One per-repo slice of a multi-repo bundle.

    ``repo`` is the GitHub slug or short name the planner will pass to
    ``gh issue create -R <repo>``. ``criteria`` is the acceptance-criteria
    block lifted from the spec for this repo, kept verbatim so the runner
    can paste it into the issue body without paraphrasing.
    """

    repo: str
    criteria: str
    title_hint: str = ""


@dataclass
class SpecBundle:
    """A candidate multi-repo bundle the planner extracted from one spec.

    ``slug`` is the kebab-case identifier that becomes the
    ``agent:bundle:<slug>`` label shared by every sibling issue. ``children``
    is the per-repo slice list, one per affected repo. ``spec_path`` lets
    the runner cite the source spec in each issue body.
    """

    slug: str
    spec_path: Path
    summary: str
    children: list[BundleChild] = field(default_factory=list)
    severity: str = "p2"

    @property
    def bundle_label(self) -> str:
        return f"{BUNDLE_LABEL_PREFIX}{self.slug}"

    @property
    def affected_repos(self) -> list[str]:
        return [child.repo for child in self.children]

    @property
    def is_multi_repo(self) -> bool:
        return len({child.repo for child in self.children}) >= 2


@dataclass
class Plan:
    """The full planner output for one firing.

    ``bundles`` are the candidates that survived the multi-repo gate and
    the daily cap. ``rejected_single_repo`` is the count of specs that
    parsed cleanly but only touched one in-scope repo — those belong to
    drake, not damian, and the runner logs the count so the operator
    can see whether the planner saw any spec material at all.
    """

    bundles: list[SpecBundle] = field(default_factory=list)
    rejected_single_repo: int = 0
    skipped_unparseable: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.bundles


# ---------------------------------------------------------------------------
# Spec parser Protocol + default markdown implementation
# ---------------------------------------------------------------------------


class SpecParser(Protocol):
    """Pluggable spec discovery and parsing.

    A parser walks a spec directory, returns the list of spec paths it
    can read, and converts each into a ``SpecBundle`` candidate. The
    default ``MarkdownSpecParser`` handles the markdown shape documented
    in ``docs/SPECS_DRIVEN_DEVELOPMENT``; fleets with proprietary spec
    formats ship their own.
    """

    def discover(self, spec_dir: Path) -> list[Path]:
        """Return every spec file the parser is willing to parse."""

    def parse(self, spec_path: Path) -> SpecBundle | None:
        """Return a ``SpecBundle`` candidate, or ``None`` if unparseable.

        ``None`` is the contract for "I saw this file but it does not
        match the spec shape" — the runner counts those without failing
        the firing.
        """


_REPO_LINE_RE = re.compile(r"^\s*Repos?\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SECTION_HEADER_RE = re.compile(
    r"^###\s+(?P<repo>[\w.-]+)\s*$",
    re.MULTILINE,
)
_ACCEPTANCE_BLOCK_RE = re.compile(
    r"^##\s*Acceptance Criteria\s*$(?P<body>.*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _slugify(text: str, *, max_len: int = 40) -> str:
    """Turn a free-text title into a kebab-case slug suitable for the
    ``agent:bundle:<slug>`` label. Capped at ``max_len`` chars so the
    label stays comfortably under GitHub's 50-char limit even after the
    ``agent:bundle:`` prefix."""
    cleaned = _SLUG_RE.sub("-", text.lower()).strip("-")
    if not cleaned:
        return "spec"
    return cleaned[:max_len].rstrip("-") or "spec"


class MarkdownSpecParser:
    """Default spec parser for the markdown shape Alfred recommends.

    Expected spec shape (matches ``docs/SPECS_DRIVEN_DEVELOPMENT``):

        # Feature: <name>

        Repos: api, web, mobile

        ## Acceptance Criteria

        ### api
        - [ ] criterion
        ### web
        - [ ] criterion

    The parser is forgiving: an inline ``Repos:`` line OR the per-repo
    ``### <repo>`` headers under ``## Acceptance Criteria`` is enough to
    extract a bundle. A spec with neither is treated as unparseable and
    the runner moves on.
    """

    file_glob: str = "*.md"

    def discover(self, spec_dir: Path) -> list[Path]:
        if not spec_dir or not spec_dir.exists() or not spec_dir.is_dir():
            return []
        # Sorted for deterministic test output and stable firing-to-firing
        # ordering when two specs would otherwise tie on priority.
        return sorted(p for p in spec_dir.glob(self.file_glob) if p.is_file())

    def parse(self, spec_path: Path) -> SpecBundle | None:
        try:
            text = spec_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("damian: failed to read %s: %s", spec_path, exc)
            return None

        title = self._extract_title(text) or spec_path.stem
        repos_from_inline = self._extract_inline_repos(text)
        repos_from_sections, criteria_by_repo = self._extract_repo_sections(text)

        # Union with order-preservation: inline list first, then any extra
        # repos that only appear in the per-repo H3 headers.
        ordered: list[str] = []
        for repo in (*repos_from_inline, *repos_from_sections):
            if repo not in ordered:
                ordered.append(repo)
        if not ordered:
            return None

        children = [
            BundleChild(
                repo=repo,
                criteria=criteria_by_repo.get(repo, "").strip(),
                title_hint=title,
            )
            for repo in ordered
        ]
        return SpecBundle(
            slug=_slugify(title),
            spec_path=spec_path,
            summary=title,
            children=children,
            severity=self._extract_severity(text),
        )

    @staticmethod
    def _extract_title(text: str) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                payload = stripped[2:].strip()
                # Strip a leading "Feature:" / "Spec:" prefix; it's
                # documentation noise, not part of the slug.
                for prefix in ("Feature:", "Spec:", "feature:", "spec:"):
                    if payload.startswith(prefix):
                        payload = payload[len(prefix) :].strip()
                        break
                return payload or None
        return None

    @staticmethod
    def _extract_inline_repos(text: str) -> list[str]:
        m = _REPO_LINE_RE.search(text)
        if not m:
            return []
        payload = m.group(1)
        return [tok.strip() for tok in re.split(r"[,\s]+", payload) if tok.strip()]

    @staticmethod
    def _extract_repo_sections(text: str) -> tuple[list[str], dict[str, str]]:
        block = _ACCEPTANCE_BLOCK_RE.search(text)
        if not block:
            return [], {}
        body = block.group("body")
        repos: list[str] = []
        criteria: dict[str, str] = {}
        # Walk the H3 sections sequentially so we capture the body between
        # each ### header and the next (or end-of-block).
        matches = list(_SECTION_HEADER_RE.finditer(body))
        for idx, match in enumerate(matches):
            repo = match.group("repo")
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
            section_body = body[start:end].strip()
            if repo not in repos:
                repos.append(repo)
            criteria[repo] = section_body
        return repos, criteria

    @staticmethod
    def _extract_severity(text: str) -> str:
        m = re.search(r"severity\s*:\s*(p[0-3])", text, re.IGNORECASE)
        return m.group(1).lower() if m else "p2"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


GhClient = Callable[[list[str]], list[dict]]
"""A callable that runs a ``gh`` JSON query and returns the decoded rows.

The default implementation in ``bin/damian.py`` is the existing
``agent_runner.gh_json`` helper; tests pass a fake.
"""


def _default_gh_client(_cmd: list[str]) -> list[dict]:
    # The pure-data planner never reaches this in unit tests because they
    # inject a fake. Production wiring lives in ``bin/damian.py``.
    return []


class SpecBundlePlanner:
    """Compose a ``Plan`` from one spec directory + open-bundle context.

    Construction surfaces the dependencies (parser, gh client, scan
    repos) so unit tests can wire them directly without environment
    fiddling. ``build_plan`` is idempotent and deterministic given the
    same inputs.
    """

    def __init__(
        self,
        *,
        scan_repos: Iterable[str],
        parser: SpecParser | None = None,
        gh_client: GhClient | None = None,
        gh_org: str = "",
        daily_bundle_cap: int = 3,
    ) -> None:
        self.scan_repos = tuple(scan_repos)
        self.parser: SpecParser = parser or MarkdownSpecParser()
        self.gh_client: GhClient = gh_client or _default_gh_client
        self.gh_org = gh_org.strip()
        self.daily_bundle_cap = max(1, daily_bundle_cap)

    @classmethod
    def from_config(
        cls,
        config: PlannerConfig,
        *,
        parser: SpecParser | None = None,
        gh_client: GhClient | None = None,
        gh_org: str = "",
    ) -> SpecBundlePlanner:
        return cls(
            scan_repos=config.scan_repos,
            parser=parser,
            gh_client=gh_client,
            gh_org=gh_org,
            daily_bundle_cap=config.daily_bundle_cap,
        )

    # ----- candidate discovery -----------------------------------------

    def build_plan(self, spec_dir: Path | None) -> Plan:
        """Walk ``spec_dir``, build candidate bundles, apply gates.

        Order of operations matches the runner's prompt contract:

        1. Discover specs via the parser.
        2. Parse each; count unparseable, count single-repo (drake's
           lane), keep multi-repo candidates.
        3. Restrict children to the configured ``scan_repos``; drop
           candidates left with fewer than two affected repos after the
           filter.
        4. Dedup against any already-open ``agent:bundle:<slug>`` labels.
        5. Cap at ``daily_bundle_cap``.
        """
        plan = Plan()
        if spec_dir is None:
            log.info("damian: no spec dir configured; emitting empty plan")
            return plan
        if not self.scan_repos:
            log.info("damian: DAMIAN_SCAN_REPOS empty; emitting empty plan")
            return plan

        open_slugs = self._fetch_open_bundle_slugs()
        scan_set = {r.lower() for r in self.scan_repos}

        candidates: list[SpecBundle] = []
        for spec_path in self.parser.discover(spec_dir):
            bundle = self.parser.parse(spec_path)
            if bundle is None:
                plan.skipped_unparseable += 1
                continue
            in_scope = [child for child in bundle.children if child.repo.lower() in scan_set]
            if len(in_scope) < 2:
                plan.rejected_single_repo += 1
                continue
            bundle.children = in_scope
            if bundle.slug in open_slugs:
                # Already filed; the runner-level dedup gate keeps damian
                # idempotent across firings.
                continue
            candidates.append(bundle)

        plan.bundles = candidates[: self.daily_bundle_cap]
        return plan

    # ----- gh integration ----------------------------------------------

    def _fetch_open_bundle_slugs(self) -> set[str]:
        """Return the set of slugs already in flight as ``agent:bundle:*``.

        Uses the injected ``gh_client``. Returns an empty set on any
        error path so a transient gh failure does not block the planner
        from proposing fresh candidates — the runner has a separate
        retry / escalation gate for gh outages.
        """
        if not self.gh_org or not self.scan_repos:
            return set()
        slugs: set[str] = set()
        for repo in self.scan_repos:
            full = repo if "/" in repo else f"{self.gh_org}/{repo}"
            try:
                rows = self.gh_client(
                    [
                        "gh",
                        "issue",
                        "list",
                        "-R",
                        full,
                        "--label",
                        LARGE_FEATURE_LABEL,
                        "--state",
                        "open",
                        "--json",
                        "labels",
                        "--limit",
                        "60",
                    ],
                )
            except Exception as exc:
                log.warning("damian: gh open-bundle scan failed for %s: %s", full, exc)
                continue
            for row in rows or []:
                for label in row.get("labels") or []:
                    name = label.get("name", "") if isinstance(label, dict) else ""
                    if name.startswith(BUNDLE_LABEL_PREFIX):
                        slugs.add(name[len(BUNDLE_LABEL_PREFIX) :])
        return slugs


# ---------------------------------------------------------------------------
# Prompt-context rendering
# ---------------------------------------------------------------------------


def render_plan_for_prompt(plan: Plan) -> str:
    """Render a ``Plan`` into prompt-ready text.

    The LLM is the one that actually files the issues, but the runner
    pre-computes candidates so the LLM does not have to re-walk the spec
    directory. The rendered block goes under
    ``## Candidate bundles (pre-computed)`` in the prompt.
    """
    if plan.is_empty:
        return "## Candidate bundles (pre-computed)\n\n(none — empty plan)\n"
    lines = ["## Candidate bundles (pre-computed)", ""]
    for bundle in plan.bundles:
        lines.append(f"### {bundle.slug} ({bundle.severity})")
        lines.append(f"- spec: `{bundle.spec_path}`")
        lines.append(f"- summary: {bundle.summary}")
        lines.append(f"- affected repos: {', '.join(bundle.affected_repos)}")
        for child in bundle.children:
            criteria = child.criteria or "(no criteria block found in spec)"
            lines.append(f"  - `{child.repo}`: {criteria.splitlines()[0][:160]}")
        lines.append("")
    if plan.rejected_single_repo:
        lines.append(f"_Single-repo specs deferred to drake: {plan.rejected_single_repo}_")
    if plan.skipped_unparseable:
        lines.append(f"_Unparseable specs skipped: {plan.skipped_unparseable}_")
    return "\n".join(lines) + "\n"


__all__ = [
    "BUNDLE_LABEL_PREFIX",
    "LARGE_FEATURE_LABEL",
    "BundleChild",
    "MarkdownSpecParser",
    "Plan",
    "PlannerConfig",
    "SpecBundle",
    "SpecBundlePlanner",
    "SpecParser",
    "render_plan_for_prompt",
]
