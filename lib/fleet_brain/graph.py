"""Fleet-authored graph densification over the FleetBrain ledger.

The fleet already records, per firing, which repo files an agent
touched (``file_touches``). That is the implicit ``firing -[touched]->
file`` relation. This module densifies the same ledger with three
explicit, fleet-authored edge kinds so a later firing can ask graph
questions without re-deriving them from raw rows every time:

* ``PR -[changed]-> file``: a pull request changed a file.
* ``file -[owned_by]-> owner``: a file's CODEOWNERS owner(s).
* ``file -[in]-> repo``: a file belongs to a repo.

Edges are materialized into the ``graph_edges`` table and keyed by
``(kind, src, dst)`` so re-projecting the same touch is idempotent and
only bumps ``last_seen`` and ``weight``. This is a thin, dependency-free
materialization; it deliberately does NOT parse code ASTs (that is the
code-memory layer's job).

Config: projection is controlled by ``ALFRED_GRAPH_DENSIFY`` and is ON
by default. Set ``ALFRED_GRAPH_DENSIFY=0`` (or ``false``/``no``/``off``)
to skip edge writes without disabling any other memory feature.

Node identity is a stable string of the form ``<type>:<value>``:

* ``file:<repo>/<path>``  e.g. ``file:your-org/api/src/app.py``
* ``repo:<repo>``         e.g. ``repo:your-org/api``
* ``pr:<url-or-ref>``     e.g. ``pr:https://github.com/your-org/api/pull/7``
* ``owner:<handle>``      e.g. ``owner:@your-org/api-team``
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from dataclasses import dataclass

GRAPH_DENSIFY_ENV: str = "ALFRED_GRAPH_DENSIFY"

EdgeKind = str  # "changed" | "owned_by" | "in"


@dataclass(frozen=True)
class GraphEdge:
    """One densified relationship between two ledger nodes."""

    kind: EdgeKind
    src_type: str
    src: str
    dst_type: str
    dst: str
    repo: str | None = None
    weight: int = 1


@dataclass(frozen=True)
class CodeOwnerRule:
    """One CODEOWNERS rule: a path glob mapped to a single owner.

    A CODEOWNERS line can name several owners; each becomes its own
    rule sharing the same ``pattern`` and ``rank`` (file order), so
    ``owned_by`` stays a clean one-edge-per-owner relation.
    """

    repo: str
    pattern: str
    owner: str
    rank: int


def densify_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True unless ``ALFRED_GRAPH_DENSIFY`` is explicitly turned off.

    Default-on: an unset or empty value means densify. Only the explicit
    falsey tokens ``0``/``false``/``no``/``off`` disable projection.
    """
    src = env if env is not None else os.environ
    raw = str(src.get(GRAPH_DENSIFY_ENV, "")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def file_node(repo: str, path: str) -> str:
    """Stable node id for a repo file."""
    return f"file:{_repo(repo)}/{_path(path)}"


def repo_node(repo: str) -> str:
    """Stable node id for a repo."""
    return f"repo:{_repo(repo)}"


def pr_node(pr_url: str) -> str:
    """Stable node id for a pull request (url or ref)."""
    return f"pr:{pr_url.strip()}"


def owner_node(owner: str) -> str:
    """Stable node id for a CODEOWNERS owner handle."""
    return f"owner:{_owner(owner)}"


def edges_for_file_touch(
    *,
    repo: str,
    path: str,
    pr_url: str | None = None,
    owners: list[str] | None = None,
) -> list[GraphEdge]:
    """Build the fleet-authored edges implied by one file touch.

    Always yields ``file -[in]-> repo``. Adds ``PR -[changed]-> file``
    when the touch carries a ``pr_url``, and one ``file -[owned_by]->
    owner`` per resolved CODEOWNERS owner. Returns an empty list for an
    empty repo/path, so callers can project unconditionally.
    """
    repo = _repo(repo)
    path = _path(path)
    if not repo or not path:
        return []
    fnode = file_node(repo, path)
    edges: list[GraphEdge] = [
        GraphEdge(
            kind="in",
            src_type="file",
            src=fnode,
            dst_type="repo",
            dst=repo_node(repo),
            repo=repo,
        )
    ]
    if pr_url and pr_url.strip():
        edges.append(
            GraphEdge(
                kind="changed",
                src_type="pr",
                src=pr_node(pr_url),
                dst_type="file",
                dst=fnode,
                repo=repo,
            )
        )
    for owner in _dedupe(owners or []):
        edges.append(
            GraphEdge(
                kind="owned_by",
                src_type="file",
                src=fnode,
                dst_type="owner",
                dst=owner_node(owner),
                repo=repo,
            )
        )
    return edges


def parse_codeowners(repo: str, text: str) -> list[CodeOwnerRule]:
    """Parse a GitHub-style CODEOWNERS file into ``(pattern, owner)`` rules.

    Honors comments (``#``), blank lines, and multiple owners per line.
    ``rank`` is the 0-based line order among non-empty rules; later rules
    win in CODEOWNERS, so a higher rank is more specific. Owners that do
    not look like a handle (``@user``, ``@org/team``, or an email) are
    skipped defensively.
    """
    repo = _repo(repo)
    rules: list[CodeOwnerRule] = []
    rank = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owners = [o for o in parts[1:] if _looks_like_owner(o)]
        if not owners:
            continue
        for owner in owners:
            rules.append(CodeOwnerRule(repo=repo, pattern=pattern, owner=_owner(owner), rank=rank))
        rank += 1
    return rules


def owners_for_path(path: str, rules: list[CodeOwnerRule]) -> list[str]:
    """Resolve the CODEOWNERS owner(s) for ``path``.

    CODEOWNERS uses "last matching pattern wins", so the highest-``rank``
    matching pattern decides ownership. All owners sharing that winning
    pattern are returned (a line can name several). Returns an empty list
    when nothing matches.
    """
    path = _path(path)
    if not path or not rules:
        return []
    best_rank = -1
    winners: list[str] = []
    by_rank: dict[int, list[str]] = {}
    for rule in rules:
        by_rank.setdefault(rule.rank, []).append(rule.owner)
    for rank in sorted(by_rank):
        # Re-find the pattern for this rank from the first rule that has it.
        pattern = _pattern_for_rank(rules, rank)
        if pattern is None:
            continue
        if _codeowners_match(pattern, path) and rank >= best_rank:
            best_rank = rank
            winners = by_rank[rank]
    return _dedupe(winners)


# ----- internals --------------------------------------------------------


def _pattern_for_rank(rules: list[CodeOwnerRule], rank: int) -> str | None:
    for rule in rules:
        if rule.rank == rank:
            return rule.pattern
    return None


def _codeowners_match(pattern: str, path: str) -> bool:
    """Approximate GitHub CODEOWNERS glob matching with ``fnmatch``.

    Rules:
    * ``*`` matches everything.
    * A trailing ``/`` (directory rule) matches any path under it.
    * A leading ``/`` anchors to the repo root; otherwise the pattern may
      match at any directory depth.
    * Otherwise fall back to ``fnmatch`` on the full path, and also try a
      basename match for bare ``*.ext`` style rules.
    """
    pat = pattern.strip()
    if not pat or pat == "*":
        return True
    anchored = pat.startswith("/")
    pat = pat.lstrip("/")
    if pat.endswith("/"):
        prefix = pat.rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if fnmatch.fnmatch(path, pat):
        return True
    if not anchored:
        # Unanchored pattern may match a suffix of the path or the basename.
        if fnmatch.fnmatch(path, f"*/{pat}"):
            return True
        if "/" not in pat and fnmatch.fnmatch(path.rsplit("/", 1)[-1], pat):
            return True
    return False


def _looks_like_owner(token: str) -> bool:
    token = token.strip()
    if token.startswith("@") and len(token) > 1:
        return True
    return "@" in token and "." in token.split("@", 1)[-1]


def _repo(repo: str) -> str:
    return (repo or "").strip().strip("/")


def _path(path: str) -> str:
    # ``lstrip("./")`` treats its argument as a character set and would strip
    # every leading ``.`` or ``/``, mangling dotfiles (".gitignore" -> "gitignore")
    # so their CODEOWNERS rules never match. Drop a single literal "./" prefix and
    # only trim slashes, leaving a leading dot intact.
    p = (path or "").strip().lstrip("/")
    if p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def _owner(owner: str) -> str:
    return (owner or "").strip()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        norm = item.strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out
