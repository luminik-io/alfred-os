#!/usr/bin/env python3
"""Deterministic guardrails for the autonomous fleet, as Claude Code hooks.

Every fleet firing runs ``claude -p --permission-mode bypassPermissions`` —
full trust, no interactive approval. The model is therefore the *only* thing
standing between a drifting prompt and a destructive action. This module adds a
deterministic backstop that survives prompt drift: a ``PreToolUse`` hook that
inspects each tool call and blocks the handful of actions the repo's locked
guardrails already forbid.

Blocked (deny + exit 2 so Claude Code refuses the call):
  - ``git push`` to a protected branch, any force-push to one, and
    ``--no-verify`` / ``--no-gpg-sign`` (the locked "never push to main / never
    skip hooks" rules).
  - ``rm -rf`` (and variants) targeting ``/``, ``~``, ``$HOME`` or any absolute
    path outside the firing's worktree — relative cleanups (node_modules, dist)
    stay allowed.
  - Reading credential material (``.env`` / ``.pem`` / ``id_rsa`` /
    ``~/.aws/credentials`` / ...) via Bash readers or the Read tool, so secrets
    never land in a transcript.
  - ``curl|bash`` / ``wget|sh`` download-and-run pipelines (supply-chain).
  - Writing a banned personal name into any file (the OSS scrub rule).

Design rules:
  - Stdlib only, so the hook runs under any ``python3`` without the venv.
  - Fail OPEN: any parse error or unexpected shape allows the call. A buggy
    guard must never wedge the whole fleet; exit 2 is reserved for a *definite*
    deny. (Claude Code treats a non-2 non-zero exit as a non-blocking error.)
  - Conservative matchers: the normal agent flow (``git push -u origin
    feat/...``, ``rm -rf node_modules``, reading ``.env.example``) is allowed.

Wired in via ``agent_runner._agent_settings_args()`` which emits a ``--settings``
payload pointing PreToolUse at ``python3 <lib>/alfred_hooks.py pretooluse``.
Disable for a manual debugging run with ``ALFRED_AGENT_HOOKS=0``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

# Branches the fleet must never push to directly (locked guardrail).
PROTECTED_BRANCHES = {"main", "master", "production", "release", "prod"}

# Personal names to scrub from writes are loaded from operator-supplied config
# ONLY (env var or a gitignored file) and are NEVER hardcoded in tracked source.
# See _load_scrub_names(). When unconfigured, the banned-name check is a no-op.

# Tools whose calls we inspect. Anything else is allowed untouched.
INSPECTED_TOOLS = {"Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}

# Shell readers that would dump a file's contents into the transcript.
_READERS = (
    "cat",
    "bat",
    "less",
    "more",
    "head",
    "tail",
    "xxd",
    "od",
    "strings",
    "base64",
    "cp",
    "scp",
    "rsync",
    "openssl",
)

ALLOW: tuple[str, str] = ("allow", "")


def _is_secret_path(path: str) -> bool:
    """True when ``path`` looks like credential material (not an example)."""
    if not path:
        return False
    p = path.strip().strip("'\"").lower()
    # Allow committed example/template env files.
    if re.search(r"\.env(\.(example|sample|template|dist|defaults?))\b", p):
        return False
    needles = (
        r"\.env(\b|$|[./])",  # .env, .env.local, .env.production
        r"\.pem\b",
        r"\.p8\b",
        r"\.pfx\b",
        r"\.keystore\b",
        r"\bid_rsa\b",
        r"\bid_ed25519\b",
        r"\bid_dsa\b",
        r"\bid_ecdsa\b",
        r"(^|/)\.ssh/",
        r"(^|/)\.aws/credentials\b",
        r"(^|/)\.aws/config\b",
        r"(^|/)\.npmrc\b",
        r"(^|/)\.pypirc\b",
        r"(^|/)\.netrc\b",
        r"\bcredentials\.json\b",
        r"\bservice[-_]account",
        r"\.p12\b",
        r"secrets?\.(ya?ml|json|env)\b",
    )
    return any(re.search(n, p) for n in needles)


def _load_scrub_names() -> tuple[str, ...]:
    """Names to scrub from writes, from operator config only (never hardcoded).

    Merged, lowercased and de-duped from:
      - ``ALFRED_SCRUB_NAMES`` (comma-separated), and
      - ``ALFRED_SCRUB_NAMES_FILE`` or ``$ALFRED_HOME/scrub-names.txt``
        (one name per line; ``#`` comments allowed).
    Returns ``()`` when nothing is configured, so the check simply does nothing.
    Keeping the actual names out of tracked source is the whole point.
    """
    names: list[str] = []
    raw = os.environ.get("ALFRED_SCRUB_NAMES", "")
    names += [n.strip() for n in raw.split(",") if n.strip()]
    path = os.environ.get("ALFRED_SCRUB_NAMES_FILE")
    if not path:
        home = os.environ.get("ALFRED_HOME")
        if home:
            path = os.path.join(home, "scrub-names.txt")
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                names += [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError:
            pass
    return tuple(dict.fromkeys(n.lower() for n in names))


def _banned_name_in(text: str | None, scrub_names: tuple[str, ...] | None = None) -> str | None:
    if not text:
        return None
    # evaluate_pretooluse loads the scrub list once and passes it in, so a single
    # hook event re-uses one read instead of re-opening the file per content
    # field. When called standalone (tests), fall back to loading it here.
    names = _load_scrub_names() if scrub_names is None else scrub_names
    if not names:
        return None
    low = text.lower()
    for name in names:
        # Whole-word-ish match so a name can't fire inside an unrelated token.
        if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", low):
            return name
    return None


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        # Unbalanced quotes etc. — fall back to a cheap split so we still scan.
        return command.split()


def _check_bash(command: str, cwd: str | None = None) -> tuple[str, str]:
    if not command or not command.strip():
        return ALLOW
    low = command.lower()
    toks = _tokens(command)
    tokset = set(toks)

    # --- download-and-run pipeline ---
    if re.search(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(bash|sh|zsh|python3?)\b", low):
        return (
            "deny",
            "Blocked: piping a remote download straight into a shell "
            "(supply-chain risk). Download, inspect, then run.",
        )

    is_git = "git" in toks
    if is_git:
        # Skipping hooks / signing is forbidden for the fleet.
        if "--no-verify" in tokset or "--no-gpg-sign" in tokset:
            return (
                "deny",
                "Blocked: --no-verify / --no-gpg-sign skips the commit "
                "hooks the fleet must always run.",
            )
        if "push" in toks:
            # The one locked rule: never push to a protected branch (forced or
            # not). Force-pushing a *feature* branch is legitimate (rebased PRs),
            # so it stays allowed; only a protected target is blocked.
            protected_join = "/".join(sorted(PROTECTED_BRANCHES))
            # 1. Explicit protected refspec, e.g. `git push origin main`, `+main`.
            #    Scan only the refspec args, NOT the remote name: a remote called
            #    `prod`/`release`/`master` must not make `git push prod feat/x`
            #    look like a push to a protected branch (the first positional
            #    after `push` is the remote and is skipped).
            if any(_targets_protected(t) for t in _push_refspecs(toks)):
                return (
                    "deny",
                    f"Blocked: pushing to a protected branch "
                    f"({protected_join}). Open a PR from a feature "
                    "branch instead.",
                )
            # 2. --all / --mirror push every local branch (incl. protected ones)
            #    without naming them.
            if tokset & {"--all", "--mirror"}:
                return (
                    "deny",
                    "Blocked: 'git push --all/--mirror' pushes every "
                    "branch, including protected ones. Push a single "
                    "feature branch explicitly.",
                )
            # 3. Implicit push (no refspec, e.g. `git push` / `git push origin`)
            #    pushes the CURRENT branch. Resolve it from cwd and block if it is
            #    protected, since the argv carries no branch token to match.
            if not _push_has_explicit_refspec(toks) and _current_branch_protected(cwd):
                return (
                    "deny",
                    f"Blocked: the checkout is on a protected branch "
                    f"({protected_join}); an implicit 'git push' would "
                    "push it. Switch to a feature branch first.",
                )

    # --- destructive rm ---
    if re.search(r"\brm\b", low) and _is_recursive_force(toks):
        # A relative `rm -rf build` is only safe if the shell's working dir is
        # still inside the worktree. `cd /tmp && rm -rf build` moves out first,
        # so a relative target escapes the path check below.
        if _cd_escapes_worktree(toks, cwd):
            return (
                "deny",
                "Blocked: 'rm -rf' after a 'cd' out of the worktree "
                "would delete outside the checkout. Run rm from the "
                "firing's working directory.",
            )
        for t in _rm_targets(toks):
            if _is_dangerous_rm_target(t, cwd):
                return (
                    "deny",
                    f"Blocked: 'rm -rf {t}' targets a path outside the "
                    "worktree (or $HOME / root). Delete only paths "
                    "inside the firing's checkout.",
                )

    # --- secret reads via shell ---
    if toks and toks[0] in _READERS:
        for t in toks[1:]:
            if _is_secret_path(t):
                return (
                    "deny",
                    f"Blocked: reading credential file '{t}' would copy "
                    "secrets into the transcript. Reference the SSM/env "
                    "name instead of the value.",
                )
    return ALLOW


def _targets_protected(token: str) -> bool:
    # Match the FULL destination branch name, not its last path segment, so a
    # feature branch like `feat/main` or `bane/release` is NOT mistaken for
    # `main`/`release`. Handles `main`, `HEAD:main`, `+main`, `src:dst`, and
    # `refs/heads/main`.
    dst = token.lstrip("+").split(":")[-1]
    if dst.startswith("refs/heads/"):
        dst = dst[len("refs/heads/") :]
    return dst in PROTECTED_BRANCHES


def _push_positional_args(toks: list[str]) -> list[str]:
    """Non-flag args after ``push``, in order. The first is the remote
    (``origin``), any further ones are refspecs (``main``, ``HEAD:main``)."""
    args: list[str] = []
    seen_push = False
    for t in toks:
        if t == "push":
            seen_push = True
            continue
        if not seen_push or t.startswith("-"):
            continue
        args.append(t)
    return args


def _push_refspecs(toks: list[str]) -> list[str]:
    """Just the refspec args of a ``git push``: positional args minus the
    leading remote name, so a remote named like a protected branch
    (``prod``/``release``/``master``) is never mistaken for a push target."""
    return _push_positional_args(toks)[1:]


def _push_has_explicit_refspec(toks: list[str]) -> bool:
    """True when `git push` names a refspec (so step 1's token scan covers it).

    The first non-flag arg after ``push`` is the remote (``origin``); a second is
    a refspec. 0-1 args means an implicit current-branch push, which carries no
    branch token and so needs the cwd-based current-branch check.
    """
    return len(_push_positional_args(toks)) >= 2


def _current_branch_protected(cwd: str | None) -> bool:
    """True when the checkout at ``cwd`` is on a protected branch.

    Fail-open (returns False) when cwd is missing or git can't answer, matching
    the module's philosophy: the explicit-refspec scan already covers the common
    case; this only closes the implicit-push gap when cwd is known.
    """
    if not cwd:
        return False
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return (out.stdout or "").strip() in PROTECTED_BRANCHES


def _is_recursive_force(toks: list[str]) -> bool:
    has_r = has_f = False
    for t in toks:
        if t.startswith("-") and not t.startswith("--"):
            # `rm` treats -r and -R identically (both recursive); match either
            # case so `rm -Rf` / `rm -fR` are caught, not just `-rf`.
            flags = t[1:].lower()
            has_r = has_r or "r" in flags
            has_f = has_f or "f" in flags
        elif t == "--recursive":
            has_r = True
        elif t == "--force":
            has_f = True
    return has_r and has_f


def _rm_targets(toks: list[str]) -> list[str]:
    out = []
    seen_rm = False
    for t in toks:
        if t == "rm":
            seen_rm = True
            continue
        if not seen_rm:
            continue
        if t.startswith("-"):
            continue
        out.append(t)
    return out


def _abs_inside_cwd(path: str, cwd: str | None) -> bool:
    """True if absolute ``path`` resolves to somewhere inside ``cwd``."""
    if not cwd:
        return False
    try:
        tgt = os.path.realpath(path)
        base = os.path.realpath(cwd)
        return tgt == base or tgt.startswith(base + os.sep)
    except Exception:
        return False


def _is_dangerous_rm_target(target: str, cwd: str | None = None) -> bool:
    t = target.strip().strip("'\"")
    if not t:
        return False
    # Whole-tree / home / cwd-root / parent sentinels.
    if t in {"/", "~", "*", ".", "./", "..", "../", "/*", "$PWD", "${PWD}"}:
        return True
    if t.startswith(("~", "$HOME", "${HOME}", "$PWD", "${PWD}")):
        return True
    if t.startswith("../"):
        return True
    if t.startswith("/"):
        # Absolute path. A path STRICTLY inside the firing worktree is a safe
        # cleanup (`rm -rf /workspace/alfred/dist` from cwd=/workspace/alfred);
        # deleting the worktree root itself, an absolute path outside cwd, or an
        # unknown cwd is dangerous.
        if cwd:
            try:
                tgt = os.path.realpath(t)
                base = os.path.realpath(cwd)
                if tgt == base:
                    return True
                if tgt.startswith(base + os.sep):
                    return False
            except Exception:
                pass
        return True
    return False


def _cd_escapes_worktree(toks: list[str], cwd: str | None = None) -> bool:
    """True if the command ``cd``s to a directory not provably inside ``cwd``.

    Guards the compound case `cd /tmp && rm -rf build`: the relative `build`
    would otherwise pass the per-target check while actually deleting outside
    the checkout. We flag any ``cd`` whose target is absolute-outside-cwd, ``~``,
    ``$HOME``, or ``..``-relative.
    """
    expect_target = False
    for t in toks:
        if t == "cd":
            expect_target = True
            continue
        if not expect_target:
            continue
        if t.startswith("-"):  # cd flags like -P / -L
            continue
        target = t.strip().strip("'\"")
        expect_target = False
        if target.startswith(("~", "$HOME", "${HOME}")):
            return True
        if target.startswith("/"):
            if not _abs_inside_cwd(target, cwd):
                return True
        elif target == ".." or target.startswith("../"):
            return True
    return False


def evaluate_pretooluse(
    tool_name: str, tool_input: dict, cwd: str | None = None
) -> tuple[str, str]:
    """Pure decision function. Returns ("allow"|"deny", reason)."""
    if tool_name not in INSPECTED_TOOLS:
        return ALLOW
    ti = tool_input or {}

    if tool_name == "Bash":
        return _check_bash(str(ti.get("command", "")), cwd)

    # File tools: block reads of secrets and writes of banned names.
    path = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
    if tool_name == "Read":
        if _is_secret_path(path):
            return (
                "deny",
                f"Blocked: reading credential file '{path}'. Reference "
                "the SSM ARN / env-var name, never the secret value.",
            )
        return ALLOW

    # Write / Edit / MultiEdit / NotebookEdit — scan the content being written.
    # Load the scrub list once for the whole event (it is read per call below).
    scrub_names = _load_scrub_names()
    if not scrub_names:
        return ALLOW
    content_fields = (
        ti.get("content"),
        ti.get("new_string"),
        ti.get("new_source"),
        ti.get("new_str"),
    )
    for chunk in content_fields:
        hit = _banned_name_in(chunk if isinstance(chunk, str) else None, scrub_names)
        if hit:
            return (
                "deny",
                f"Blocked: writing the banned name '{hit}' into "
                f"'{path or 'a file'}'. Use a generic handle "
                "(the OSS scrub rule forbids personal names).",
            )
    # MultiEdit carries a list of edits.
    for edit in ti.get("edits") or []:
        if isinstance(edit, dict):
            hit = _banned_name_in(str(edit.get("new_string", "")), scrub_names)
            if hit:
                return ("deny", f"Blocked: writing the banned name '{hit}' (OSS scrub rule).")
    return ALLOW


def _emit(decision: str, reason: str) -> int:
    """Print the hook result and return the process exit code.

    Emits both the modern ``hookSpecificOutput.permissionDecision`` JSON (for
    forward-compat) and uses exit code 2 as the stable deny signal that every
    Claude Code version honors for PreToolUse.
    """
    if decision == "deny":
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(out))
        # exit 2 → Claude Code blocks the call and feeds stderr back to the model.
        print(reason, file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    event = argv[0] if argv else "pretooluse"
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail open: never block on a malformed event

    if event != "pretooluse":
        return 0  # only PreToolUse is enforced today

    try:
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {}) or {}
        cwd = payload.get("cwd")
        decision, reason = evaluate_pretooluse(tool_name, tool_input, cwd)
    except Exception:
        return 0  # fail open on any unexpected shape
    return _emit(decision, reason)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
