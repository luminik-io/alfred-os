"""Dependency parsing helpers for Alfred issue pickup."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

DEPENDENCY_LINE = re.compile(
    r"^\s*(?:depends\s+on|blocked\s+by|requires)\s*:\s*(?P<refs>.+)$",
    re.IGNORECASE | re.MULTILINE,
)
URL_REF = re.compile(
    r"https://github\.com/(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+)/(?:issues|pull)/(?P<number>\d+)",
    re.IGNORECASE,
)
QUALIFIED_REF = re.compile(
    r"(?<![\w.-])(?:(?P<owner>[\w.-]+)/)?(?P<repo>[\w.-]+)#(?P<number>\d+)\b"
)
LOCAL_REF = re.compile(r"(?<![\w/.-])#(?P<number>\d+)\b")
AMBIGUOUS_BARE_REPO_REFS = frozenset(
    {
        "close",
        "closed",
        "closes",
        "fix",
        "fixed",
        "fixes",
        "issue",
        "issues",
        "pr",
        "pull",
        "resolve",
        "resolved",
        "resolves",
    }
)


@dataclass(frozen=True, order=True)
class IssueRef:
    repo: str
    number: int


def repo_from_issue_url(url: str) -> str:
    """Return the repo slug from a GitHub issue or PR URL."""
    match = re.search(
        r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:issues|pull)/\d+",
        url,
    )
    return _repo_slug(match.group("repo"), owner=match.group("owner")) if match else ""


def parse_dependency_refs(body: str, *, default_repo: str = "") -> tuple[IssueRef, ...]:
    """Parse dependency refs from ``Depends on:`` style lines."""
    refs: set[IssueRef] = set()
    for line in DEPENDENCY_LINE.finditer(body or ""):
        text = line.group("refs")
        consumed: list[tuple[int, int]] = []
        for match in URL_REF.finditer(text):
            refs.add(
                IssueRef(
                    _repo_slug(match.group("repo"), owner=match.group("owner")),
                    int(match.group("number")),
                )
            )
            consumed.append(match.span())
        for match in QUALIFIED_REF.finditer(text):
            if _inside_any(match.span(), consumed):
                continue
            if _is_ambiguous_bare_repo_ref(match):
                continue
            refs.add(
                IssueRef(
                    _repo_slug(
                        match.group("repo"),
                        owner=match.group("owner"),
                        default_repo=default_repo,
                    ),
                    int(match.group("number")),
                )
            )
            consumed.append(match.span())
        if default_repo:
            for match in LOCAL_REF.finditer(text):
                if _inside_any(match.span(), consumed):
                    continue
                refs.add(IssueRef(default_repo, int(match.group("number"))))
    return tuple(sorted(refs))


def issue_ref(issue: dict) -> IssueRef | None:
    """Return an ``IssueRef`` for a GitHub issue payload."""
    repo = repo_from_issue_url(issue.get("url", ""))
    try:
        number = int(issue["number"])
    except (KeyError, TypeError, ValueError):
        return None
    return IssueRef(repo, number) if repo else None


def issue_dependencies(issue: dict, *, default_repo: str = "") -> tuple[IssueRef, ...]:
    """Return dependencies declared by an issue payload."""
    repo = repo_from_issue_url(issue.get("url", "")) or default_repo
    own = issue_ref(issue)
    deps = parse_dependency_refs(issue.get("body", ""), default_repo=repo)
    if own is None:
        return deps
    return tuple(dep for dep in deps if dep != own)


def sort_issues_by_dependencies(issues: Iterable[dict]) -> list[dict]:
    """Topologically sort issues when dependency refs point inside the set.

    Unknown external dependencies are ignored for ordering. Cycles fall back to
    the original order so operators do not lose visibility.
    """
    original = list(issues)
    keys = [issue_ref(issue) for issue in original]
    key_to_issue = {
        key: issue for key, issue in zip(keys, original, strict=False) if key is not None
    }
    if len(key_to_issue) < 2:
        return original
    original_index = {key: index for index, key in enumerate(keys) if key is not None}
    remaining = set(key_to_issue)
    emitted: list[IssueRef] = []
    deps_by_key = {
        key: {dep for dep in issue_dependencies(issue) if dep in key_to_issue and dep != key}
        for key, issue in key_to_issue.items()
    }
    while remaining:
        emitted_set = set(emitted)
        ready = sorted(
            (key for key in remaining if deps_by_key[key].issubset(emitted_set)),
            key=lambda key: original_index[key],
        )
        if not ready:
            return original
        for key in ready:
            remaining.remove(key)
            emitted.append(key)
    sorted_issues = [key_to_issue[key] for key in emitted]
    missing_key_issues = [issue for key, issue in zip(keys, original, strict=False) if key is None]
    return sorted_issues + missing_key_issues


def _inside_any(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start >= used_start and end <= used_end for used_start, used_end in spans)


def _is_ambiguous_bare_repo_ref(match: re.Match[str]) -> bool:
    return not match.group("owner") and match.group("repo").lower() in AMBIGUOUS_BARE_REPO_REFS


def _repo_slug(repo: str, *, owner: str | None = None, default_repo: str = "") -> str:
    """Return a bare repo or ``owner/repo`` slug without losing explicit owners."""
    clean_repo = (repo or "").strip()
    clean_owner = (owner or "").strip()
    if clean_owner:
        return f"{clean_owner}/{clean_repo}"
    if "/" in default_repo:
        default_owner = default_repo.split("/", 1)[0].strip()
        if default_owner:
            return f"{default_owner}/{clean_repo}"
    return clean_repo
