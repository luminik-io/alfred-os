"""Trusted Slack control + query commands for the fleet.

A trusted workspace user can message the bot with a **leading
command verb** to act on the fleet without leaving Slack:

* ``status``               -> fleet health from ``alfred status --json``
* ``pause <codename>``     -> stop scheduled firings for one agent
* ``resume <codename>``    -> reverse a pause
* ``run <codename>``       -> operator-only: trigger one agent now
* ``dry-run <codename|all>`` -> simulate firings with no side effects
* ``runs``                 -> recent firings (last-fired + today counts)
* ``plans``                -> local planning inbox
* ``plan <id>``            -> planning draft or follow-up detail
* ``draft <id>``           -> convert a captured follow-up to a local draft
* ``handled <id>``         -> operator-only: archive a captured follow-up
* ``memory``               -> reviewable memory queue + promotion suggestions
* ``remember ...``         -> stage a reviewable memory candidate from Slack
* ``memory remember ...``  -> same candidate path inside the memory namespace
* ``memory promote <id>``  -> operator-only: promote a memory candidate
* ``memory reject <id>``   -> operator-only: reject a memory candidate
* ``memory redis``         -> check optional Redis Agent Memory Server
* ``assign <issue>``       -> operator-only: route an issue to Batman/Lucius
* ``trusted``              -> list Slack users who can collaborate on plans
* ``trust <@user>``        -> operator-only: add a trusted collaborator
* ``untrust <@user>``      -> operator-only: remove a local collaborator
* ``help``                 -> list these commands

CRITICAL SAFETY MODEL
=====================

* **Explicit leading verb only.** A message is a control command only when
  its first whitespace-delimited token is a known verb. Free-form prose
  ("can you pause everything later?") never triggers an action -- it falls
  through to the normal planning intake. This is the single most important
  guard against a chat message accidentally controlling the fleet.

* **Trust gating happens upstream.** The listener only calls into this
  module after it has already confirmed the message came from a configured
  trusted Slack user. This module additionally refuses if handed an
  untrusted flag (defense in depth).

* **No shell, ever.** Commands that call the scheduler
  (``pause``/``resume``/``run``/``dry-run``) run the ``alfred`` CLI through
  an explicit argv vector via ``subprocess.run`` with ``shell=False``. The
  codename is validated against a strict charset (``[A-Za-z0-9._-]``, no
  leading ``-``) *before* it is placed in the argv, so it can never be read
  as a flag or inject a second command.

* **Queries are read-only.** ``status`` and ``runs`` shell out to
  ``alfred status --json`` only. ``dry-run`` shells out to ``alfred dry-run``
  and relies on the CLI's no-side-effect preview path.

The CLI invocation is injected (``runner``) so tests exercise the full
parse + dispatch path without spawning a real process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runner.metadata import agent_profile
from planning_actions import convert_followup_to_draft, mark_followup_handled
from server.reader import (
    FilesystemReader,
    PlanDraft,
)
from server.reader import (
    default_state_root as default_reader_state_root,
)
from slack_trust import (
    SlackTrustStore,
    env_trusted_user_ids,
    normalize_slack_user_id,
    operator_user_id_from_env,
    trusted_users_snapshot,
)
from slack_trust import (
    default_state_root as default_trust_state_root,
)

# A codename is a short identifier. This is deliberately strict: only word
# characters, dot, underscore and hyphen, never starting with a hyphen (which
# could otherwise be parsed as a CLI flag). ``all`` is allowed because the
# pause/resume/dry-run CLIs accept it as a fleet-wide target. Manual ``run``
# rejects ``all`` at dispatch.
_CODENAME_RE = re.compile(r"^(?!-)[A-Za-z0-9._-]{1,64}$")

# Known leading verbs. A message only becomes a control command when its first
# token (lowercased) is one of these.
_COMMANDS: frozenset[str] = frozenset(
    {
        "status",
        "pause",
        "resume",
        "run",
        "dry-run",
        "dryrun",
        "schedule",
        "runs",
        "plans",
        "plan",
        "draft",
        "handled",
        "memory",
        "memories",
        "remember",
        "assign",
        "queue",
        "hold",
        "trusted",
        "trust",
        "untrust",
        "help",
    }
)

_DEFAULT_RUN_CODENAMES: frozenset[str] = frozenset(
    {
        "agent-cleanup",
        "alfred-nightly",
        "automerge",
        "bane",
        "batman",
        "brand-mention-scanner",
        "cleanup",
        "code-map-refresh",
        "cold-backup",
        "content-drift",
        "damian",
        "drake",
        "fleet-doctor",
        "fleet-recap-evening",
        "fleet-recap-morning",
        "gordon",
        "huntress",
        "lucius",
        "memory-harvest",
        "morning-brief",
        "nightwing",
        "rasalghul",
        "robin",
        "shipped-summary-daily",
        "shipped-summary-weekly",
    }
)

_PLAN_ID_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,180}$")
_MEMORY_ID_RE = re.compile(r"^(?!-)[A-Za-z0-9:_-]{1,96}$")
_REPO_TOKEN_RE = re.compile(r"^(?!-)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FLEET_YAML_AGENT_RE = re.compile(r"^  ([A-Za-z0-9._-]+):\s*$")

CommandRunner = Callable[[list[str]], "RunResult"]


@dataclass(frozen=True)
class RunResult:
    """Result of one injected CLI invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ControlCommand:
    """A parsed, validated control command."""

    verb: str
    arg: str = ""


@dataclass(frozen=True)
class ControlResult:
    """Outcome of handling a control message."""

    handled: bool
    action: str
    text: str = ""
    detail: str = ""


def parse_control_command(text: str) -> ControlCommand | None:
    """Parse a message into a control command, or ``None`` if it is not one.

    A message is a control command only when its first whitespace-delimited
    token (after stripping ``<@mentions>`` and a leading ``/`` or ``!``) is a
    known verb. ``pause``/``resume`` additionally require a single valid
    codename argument; anything else returns ``None`` so the message falls
    through to planning intake rather than being mis-handled.
    """
    cleaned = _strip_mentions(text).strip()
    if not cleaned:
        return None
    tokens = cleaned.split()
    if not tokens:
        return None
    first_token = tokens[0]
    verb = first_token.lstrip("/!").lower()
    if verb == "dryrun":
        verb = "dry-run"
    if verb not in _COMMANDS:
        return None
    args = tokens[1:]
    rest = cleaned[len(first_token) :].strip()

    if verb in {"pause", "resume"}:
        # Exactly one argument, and it must be a valid codename. Extra words
        # mean this is prose ("pause the project for now"), not a command.
        if len(args) != 1 or not is_valid_codename(args[0]):
            return None
        return ControlCommand(verb=verb, arg=args[0])

    if verb in {"run", "dry-run"}:
        # Same exact leading-verb shape as pause/resume. ``run`` rejects
        # ``all`` later with an explicit operator-facing message because a
        # fleet-wide manual kick would stampede the host.
        if len(args) != 1 or not is_valid_codename(args[0]):
            return None
        return ControlCommand(verb=verb, arg=args[0])

    if verb == "schedule":
        if not args:
            return ControlCommand(verb=verb, arg="list")
        subcommand = args[0].lower()
        if subcommand == "list" and len(args) == 1:
            return ControlCommand(verb=verb, arg="list")
        if subcommand == "show" and len(args) == 2 and is_valid_codename(args[1]):
            return ControlCommand(verb=verb, arg=f"show {args[1]}")
        if subcommand == "set" and len(args) == 3 and is_valid_codename(args[1]):
            return ControlCommand(verb=verb, arg=f"set {args[1]} {args[2]}")
        return None

    if verb in {"trust", "untrust"}:
        if len(args) != 1:
            return None
        user_id = normalize_slack_user_id(args[0])
        if user_id is None:
            return None
        return ControlCommand(verb=verb, arg=user_id)

    if verb in {"plan", "draft", "handled"}:
        if len(args) != 1 or not is_valid_plan_id(args[0]):
            return None
        return ControlCommand(verb=verb, arg=args[0])

    if verb in {"memory", "memories"}:
        return ControlCommand(verb="memory", arg=rest)

    if verb == "remember":
        if not rest:
            return None
        return ControlCommand(verb=verb, arg=rest)

    if verb in {"assign", "queue", "hold"}:
        # Arg is an issue reference (GitHub URL or owner/repo#N); keep the rest
        # of the line verbatim and let parse_issue_ref validate it.
        if not rest:
            return None
        return ControlCommand(verb=verb, arg=rest)

    # status / runs / trusted / help are exact commands. Extra words are prose
    # ("help me write the onboarding spec"), not a control command.
    if args:
        return None
    return ControlCommand(verb=verb)


def is_valid_codename(value: str) -> bool:
    """True iff ``value`` is a safe codename token for the CLI argv.

    Strict allowlist: ``[A-Za-z0-9._-]``, 1-64 chars, never leading ``-``.
    This is the injection guard for ``pause``/``resume``/``run``/``dry-run``.
    """
    return bool(_CODENAME_RE.match(value or ""))


def is_valid_plan_id(value: str) -> bool:
    """True iff ``value`` is a safe local planning inbox id."""
    return bool(_PLAN_ID_RE.match(value or ""))


def is_valid_memory_id(value: str) -> bool:
    """True iff ``value`` is a safe memory candidate id for CLI argv."""
    return bool(_MEMORY_ID_RE.match(value or ""))


class SlackControlHandler:
    """Dispatch trusted leading-verb control commands to the ``alfred`` CLI.

    The handler holds no Slack state. The listener decides a message came
    from a trusted user, then calls :meth:`handle`. Mutating commands run the
    real ``alfred`` binary via an injected ``runner`` (default: ``subprocess``
    with an explicit argv, ``shell=False``).
    """

    def __init__(
        self,
        *,
        alfred_bin: Path | str | None = None,
        runner: CommandRunner | None = None,
        trust_store: SlackTrustStore | None = None,
        operator_user_id: str | None = None,
        state_root: Path | str | None = None,
        plan_reader: Any | None = None,
        memory_provider: Any | None = None,
        known_run_codenames: Iterable[str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.alfred_bin = str(alfred_bin) if alfred_bin else _default_alfred_bin()
        self._runner = runner or self._default_runner
        self.trust_store = trust_store
        self.operator_user_id = (
            normalize_slack_user_id(operator_user_id) or operator_user_id_from_env()
        )
        self.state_root = (
            Path(state_root) if state_root is not None else default_reader_state_root()
        )
        self.plan_reader = plan_reader
        self.memory_provider = memory_provider
        self._known_run_codenames = (
            frozenset(c for c in known_run_codenames if is_valid_codename(c))
            if known_run_codenames is not None
            else None
        )
        self.timeout = timeout

    def handle(
        self,
        text: str,
        *,
        trusted: bool,
        actor_user_id: str | None = None,
    ) -> ControlResult:
        """Handle a candidate control message.

        Returns ``ControlResult(handled=False, ...)`` when the message is not
        a control command so the caller can fall through to planning intake.
        """
        if not trusted:
            # Defense in depth: the listener already gates trust, but never
            # act on an untrusted control attempt even if called directly.
            return ControlResult(False, "ignored_untrusted", detail="control from untrusted user")
        command = parse_control_command(text)
        if command is None:
            # The message may still LEAD with a known verb but have bad args
            # (e.g. a bare "pause" or "pause two words"). In that case answer
            # with usage rather than letting it fall through to planning intake.
            usage = _usage_for_malformed(text)
            if usage is not None:
                return ControlResult(True, "usage", text=usage, detail="malformed control command")
            return ControlResult(False, "not_a_command", detail="no leading control verb")

        if command.verb == "help":
            return ControlResult(True, "help", text=render_help())
        if command.verb == "status":
            return self._run_status()
        if command.verb == "runs":
            return self._run_runs()
        if command.verb == "plans":
            return self._run_plans()
        if command.verb == "plan":
            return self._run_plan_detail(command.arg)
        if command.verb == "draft":
            return self._run_followup_draft(command.arg)
        if command.verb == "handled":
            return self._run_followup_handled(command.arg, actor_user_id)
        if command.verb == "memory":
            return self._run_memory(command.arg, actor_user_id)
        if command.verb == "remember":
            return self._run_remember(command.arg, actor_user_id)
        if command.verb == "trusted":
            return self._run_trusted()
        if command.verb == "schedule":
            return self._run_schedule(command.arg, actor_user_id)
        if command.verb == "assign":
            return self._run_assign(command.arg, actor_user_id)
        if command.verb in {"trust", "untrust"}:
            return self._run_trust_mutation(command.verb, command.arg, actor_user_id)
        if command.verb in {"queue", "hold"}:
            return self._run_queue(command.verb, command.arg, actor_user_id)
        if command.verb in {"pause", "resume"}:
            return self._run_agent_cli(command.verb, command.arg)
        if command.verb == "run":
            return self._run_manual_agent(command.arg, actor_user_id)
        if command.verb == "dry-run":
            return self._run_agent_cli(command.verb, command.arg)
        return ControlResult(False, "not_a_command", detail=f"unhandled verb {command.verb}")

    def _run_assign(
        self,
        arg: str,
        actor_user_id: str | None,
    ) -> ControlResult:
        """`assign <issue>` decides Batman vs Lucius and labels the issue.

        Assignment can make an issue eligible for autonomous pickup, so it is
        operator-only, matching ``queue``.
        """
        from issue_assignment import assign_issue
        from issue_queue import parse_issue_ref

        actor = normalize_slack_user_id(actor_user_id)
        if not self.operator_user_id or actor != self.operator_user_id:
            return ControlResult(
                True,
                "assign_rejected",
                text=(
                    "*Only the operator can assign work to Alfred.*\n\n"
                    "Assignment can make an issue eligible for autonomous pickup. Nothing changed."
                ),
                detail="actor is not the operator",
            )

        ref = parse_issue_ref(arg)
        if ref is None:
            return ControlResult(
                True,
                "assign_rejected",
                text=(
                    f"*Couldn't read an issue from* `{_short(arg)}`.\n\n"
                    "Send a GitHub issue link or `owner/repo#123`."
                ),
                detail="unparseable issue ref",
            )
        repo, number = ref
        result = assign_issue(repo, number)
        if not result.ok:
            reason = result.error or result.detail
            return ControlResult(
                True,
                "assign_failed",
                text=f"*Assignment did not run.*\n\n{reason}",
                detail=reason,
            )
        return ControlResult(
            True,
            "assign",
            text=_assignment_ack_text(repo, number, result.detail),
            detail=result.detail,
        )

    def _run_queue(
        self,
        verb: str,
        arg: str,
        actor_user_id: str | None,
    ) -> ControlResult:
        """`queue <issue>` makes an issue eligible for pickup; `hold <issue>`
        takes it out of Alfred's reach. Accepts a GitHub issue URL or
        owner/repo#N.

        Arming an issue (``queue`` -> ``agent:implement``) makes it eligible for
        autonomous bypassPermissions pickup, so it is operator-only. De-arming
        (``hold``) is safe and stays open to all trusted collaborators.
        """
        from issue_queue import parse_issue_ref, set_issue_pickup

        if verb == "queue":
            actor = normalize_slack_user_id(actor_user_id)
            if not self.operator_user_id or actor != self.operator_user_id:
                return ControlResult(
                    True,
                    "queue_rejected",
                    text=(
                        "*Only the operator can queue work for Alfred.*\n\n"
                        "Arming an issue makes it eligible for autonomous pickup. "
                        "Use `hold` to take an issue out of Alfred's reach. Nothing changed."
                    ),
                    detail="actor is not the operator",
                )

        ref = parse_issue_ref(arg)
        if ref is None:
            return ControlResult(
                True,
                f"{verb}_rejected",
                text=(
                    f"*Couldn't read an issue from* `{_short(arg)}`.\n\n"
                    "Send a GitHub issue link or `owner/repo#123`."
                ),
                detail="unparseable issue ref",
            )
        repo, number = ref
        ok, detail = set_issue_pickup(repo, number, hold=(verb == "hold"))
        if not ok:
            # ``detail`` may carry raw gh stderr. Keep it in the structured
            # ``detail`` for server-side JSON only; show a generic message on
            # the Slack/HTTP surface so gh internals never leak to chat.
            return ControlResult(
                True,
                f"{verb}_failed",
                text="*Queue update failed (gh error).*",
                detail=detail,
            )
        emoji = ":raised_hand:" if verb == "hold" else ":inbox_tray:"
        return ControlResult(True, verb, text=f"{emoji} {detail}.")

    # -- query commands (read-only) --------------------------------------

    def _status_snapshot(self) -> dict[str, Any] | None:
        result = self._runner([self.alfred_bin, "status", "--json"])
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _run_status(self) -> ControlResult:
        data = self._status_snapshot()
        if data is None:
            return ControlResult(
                True,
                "status_unavailable",
                text="*Fleet status unavailable*\n\nCould not read `alfred status --json`.",
            )
        return ControlResult(True, "status", text=render_fleet_status(data))

    def _run_runs(self) -> ControlResult:
        data = self._status_snapshot()
        if data is None:
            return ControlResult(
                True,
                "runs_unavailable",
                text="*Recent runs unavailable*\n\nCould not read `alfred status --json`.",
            )
        return ControlResult(True, "runs", text=render_recent_runs(data))

    def _reader(self) -> Any:
        return self.plan_reader or FilesystemReader(self.state_root)

    def _run_plans(self) -> ControlResult:
        try:
            rows = self._reader().list_plans(limit=10)
        except Exception as exc:
            return ControlResult(
                True,
                "plans_unavailable",
                text="*Planning inbox unavailable*\n\nCould not read local planning state.",
                detail=str(exc),
            )
        return ControlResult(True, "plans", text=render_plans(rows))

    def _run_plan_detail(self, plan_id: str) -> ControlResult:
        plan = self._get_plan(plan_id)
        if plan is None:
            return ControlResult(True, "plan_not_found", text=f"*Plan not found:* `{plan_id}`.")
        return ControlResult(True, "plan", text=render_plan_detail(plan))

    def _run_followup_draft(self, plan_id: str) -> ControlResult:
        plan = self._get_plan(plan_id)
        if plan is None:
            return ControlResult(
                True, "draft_not_found", text=f"*Follow-up not found:* `{plan_id}`."
            )
        if plan.source != "followup":
            return ControlResult(
                True,
                "draft_rejected",
                text=f"*Cannot draft `{plan_id}`:* only captured follow-ups can be converted.",
            )
        try:
            conversion = convert_followup_to_draft(
                plan,
                state_root=self.state_root,
                memory_provider=self.memory_provider,
            )
        except Exception as exc:
            return ControlResult(
                True,
                "draft_failed",
                text=f"*Could not create a planning draft from* `{plan_id}`.\n\nNothing ran.",
                detail=str(exc),
            )
        return ControlResult(
            True,
            "draft",
            text=(
                "*Planning draft created from follow-up*\n\n"
                f"- Draft: `{conversion.draft_id}`\n"
                f"- Source archived: `{conversion.archived_path.name}`\n\n"
                f"Use `plan {conversion.draft_id}` to inspect it. It stays local until an explicit approval."
            ),
        )

    def _run_followup_handled(
        self,
        plan_id: str,
        actor_user_id: str | None,
    ) -> ControlResult:
        actor = normalize_slack_user_id(actor_user_id)
        if not self.operator_user_id or actor != self.operator_user_id:
            return ControlResult(
                True,
                "handled_rejected",
                text="*Only the operator can mark follow-ups handled.*\n\nNothing changed.",
                detail="actor is not the operator",
            )
        plan = self._get_plan(plan_id)
        if plan is None:
            return ControlResult(
                True, "handled_not_found", text=f"*Follow-up not found:* `{plan_id}`."
            )
        if plan.source != "followup":
            return ControlResult(
                True,
                "handled_rejected",
                text=f"*Cannot mark `{plan_id}` handled:* only captured follow-ups can be archived.",
            )
        try:
            archived = mark_followup_handled(plan)
        except Exception as exc:
            return ControlResult(
                True,
                "handled_failed",
                text=f"*Could not mark follow-up handled:* `{plan_id}`.",
                detail=str(exc),
            )
        return ControlResult(
            True,
            "handled",
            text=f"*Marked follow-up handled:* `{plan_id}`.\n\nArchived as `{archived.name}`.",
        )

    def _run_memory(self, raw_args: str, actor_user_id: str | None) -> ControlResult:
        args = raw_args.split()
        subcommand = args[0].lower() if args else "review"
        rest = args[1:]

        if subcommand in {"review", "queue", "candidates", "candidate"}:
            candidates, error = self._memory_candidates(limit=5)
            if candidates is None:
                return _memory_unavailable(error)
            promotions, _promotion_error = self._memory_promotions(limit=5)
            return ControlResult(
                True,
                "memory",
                text=render_memory_review(candidates, promotions or []),
            )

        if subcommand in {"promotions", "promotable"}:
            promotions, error = self._memory_promotions(limit=10)
            if promotions is None:
                return _memory_unavailable(error)
            return ControlResult(
                True,
                "memory_promotions",
                text=render_memory_promotions(promotions),
            )

        if subcommand == "remember":
            return self._run_remember(" ".join(rest), actor_user_id)

        if subcommand == "harvest":
            return self._run_memory_harvest(rest, actor_user_id)

        if subcommand == "redis":
            if rest and rest[0].lower() == "sync":
                return self._run_memory_sync(rest[1:], actor_user_id)
            health, error = self._memory_redis_status()
            if health is None:
                return _memory_unavailable(error)
            return ControlResult(True, "memory_redis", text=render_redis_status(health))

        if subcommand == "sync":
            return self._run_memory_sync(rest, actor_user_id)

        if subcommand in {"promote", "approve"}:
            if len(rest) != 1 or not is_valid_memory_id(rest[0]):
                return ControlResult(True, "usage", text=render_memory_usage())
            return self._run_memory_review_action("promote", rest[0], "", actor_user_id)

        if subcommand == "reject":
            if not rest or not is_valid_memory_id(rest[0]):
                return ControlResult(True, "usage", text=render_memory_usage())
            note = " ".join(rest[1:]).strip()
            return self._run_memory_review_action("reject", rest[0], note, actor_user_id)

        return ControlResult(True, "usage", text=render_memory_usage())

    def _run_remember(self, raw_args: str, actor_user_id: str | None) -> ControlResult:
        parsed = _parse_remember_payload(raw_args)
        if parsed is None:
            return ControlResult(True, "usage", text=render_remember_usage())
        repo, body = parsed
        attempts = [
            [
                "propose",
                "--tag",
                "slack",
                "--source",
                "slack",
                "--evidence",
                f"Slack memory note from <@{normalize_slack_user_id(actor_user_id) or 'trusted-user'}>",
                "--confidence",
                "0.70",
                "--json",
                "operator",
                repo,
                "--",
                body,
            ],
            [
                "propose",
                "--agent",
                "operator",
                "--repo",
                repo,
                "--topic",
                "slack",
                "--body",
                body,
                "--evidence",
                json.dumps(
                    {
                        "source": "slack",
                        "actor": normalize_slack_user_id(actor_user_id) or "trusted-user",
                    }
                ),
                "--json",
            ],
        ]
        result = self._run_brain_first_success(attempts)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            return ControlResult(
                True,
                "remember_failed",
                text=f"*Could not queue memory candidate.*\n\n```{_short(err, 700)}```",
                detail=err,
            )
        candidate_id = _extract_candidate_id(result.stdout)
        id_line = f"\n- Candidate: `{candidate_id}`" if candidate_id else ""
        return ControlResult(
            True,
            "remember",
            text=(
                "*Memory candidate queued for review*\n\n"
                f"- Repo: `{repo}`{id_line}\n"
                f"- Note: {_short(body, 240)}\n\n"
                "It is not prompt context yet. The operator can promote it with "
                f"`memory promote {candidate_id or '<id>'}` after review."
            ),
        )

    def _run_memory_review_action(
        self,
        action: str,
        candidate_id: str,
        note: str,
        actor_user_id: str | None,
    ) -> ControlResult:
        actor = normalize_slack_user_id(actor_user_id)
        if not self.operator_user_id or actor != self.operator_user_id:
            return ControlResult(
                True,
                f"memory_{action}_rejected",
                text="*Only the operator can promote or reject memory candidates.*\n\nNothing changed.",
                detail="actor is not the operator",
            )
        argv = [action, candidate_id, "--reviewer", actor or "operator", "--json"]
        if note:
            argv.append(f"--note={note}")
        result = self._run_brain(argv)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            return ControlResult(
                True,
                f"memory_{action}_failed",
                text=f"*Could not {action} memory candidate* `{candidate_id}`.\n\n```{_short(err, 700)}```",
                detail=err,
            )
        past = "promoted" if action == "promote" else "rejected"
        return ControlResult(
            True,
            f"memory_{action}",
            text=f"*Memory candidate {past}:* `{candidate_id}`.",
        )

    def _run_memory_sync(
        self,
        args: list[str],
        actor_user_id: str | None,
    ) -> ControlResult:
        actual = bool(args and args[0].lower() in {"now", "run", "write"})
        actor = normalize_slack_user_id(actor_user_id)
        if actual and (not self.operator_user_id or actor != self.operator_user_id):
            return ControlResult(
                True,
                "memory_sync_rejected",
                text="*Only the operator can sync memory to Redis AMS.*\n\nNothing changed.",
                detail="actor is not the operator",
            )
        argv = ["redis-sync", "--json"]
        if not actual:
            argv.append("--dry-run")
        result = self._run_brain(argv)
        payload = _json_or_none(result.stdout)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            return ControlResult(
                True,
                "memory_sync_failed",
                text=f"*Redis memory sync failed.*\n\n```{_short(err, 700)}```",
                detail=err,
            )
        if not isinstance(payload, dict):
            return ControlResult(True, "memory_sync", text=_short(result.stdout, 900))
        return ControlResult(True, "memory_sync", text=render_redis_sync(payload))

    def _run_memory_harvest(
        self,
        args: list[str],
        actor_user_id: str | None,
    ) -> ControlResult:
        actual = bool(args and args[0].lower() == "now")
        actor = normalize_slack_user_id(actor_user_id)
        if actual and (not self.operator_user_id or actor != self.operator_user_id):
            return ControlResult(
                True,
                "memory_harvest_rejected",
                text="*Only the operator can queue harvested memory candidates.*\n\nNothing changed.",
                detail="actor is not the operator",
            )
        argv = ["harvest", "--json"]
        if actual:
            argv.append("--apply")
        result = self._run_brain(argv)
        payload = _json_or_none(result.stdout)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            return ControlResult(
                True,
                "memory_harvest_failed",
                text=f"*Memory harvest failed.*\n\n```{_short(err, 700)}```",
                detail=err,
            )
        if not isinstance(payload, dict):
            return ControlResult(True, "memory_harvest", text=_short(result.stdout, 900))
        return ControlResult(True, "memory_harvest", text=render_memory_harvest(payload))

    def _memory_candidates(self, *, limit: int) -> tuple[list[dict[str, Any]] | None, str]:
        data, error = self._run_brain_json_first(
            [
                [
                    "candidates",
                    "--status",
                    "candidate",
                    "--limit",
                    str(limit),
                    "--json",
                ],
                ["candidates", "--status", "pending", "--limit", str(limit), "--json"],
            ]
        )
        if data is None:
            return None, error
        return _ensure_dict_list(data), ""

    def _memory_promotions(self, *, limit: int) -> tuple[list[dict[str, Any]] | None, str]:
        data, error = self._run_brain_json_first([["promotions", "--limit", str(limit), "--json"]])
        if data is None:
            return None, error
        return _ensure_dict_list(data), ""

    def _memory_redis_status(self) -> tuple[dict[str, Any] | None, str]:
        result = self._run_brain(["redis-status", "--json"])
        payload = _json_or_none(result.stdout)
        if isinstance(payload, dict):
            return payload, ""
        return None, (result.stderr or result.stdout or "Redis status unavailable").strip()

    def _run_brain_json_first(
        self,
        attempts: list[list[str]],
    ) -> tuple[Any | None, str]:
        errors: list[str] = []
        for argv in attempts:
            result = self._run_brain(argv)
            payload = _json_or_none(result.stdout)
            if result.returncode == 0 and payload is not None:
                return payload, ""
            errors.append((result.stderr or result.stdout or "").strip())
        return None, "\n".join(e for e in errors if e) or "alfred brain returned no JSON"

    def _run_brain_first_success(self, attempts: list[list[str]]) -> RunResult:
        last = RunResult(returncode=1, stderr="no attempts")
        for argv in attempts:
            last = self._run_brain(argv)
            if last.returncode == 0:
                return last
        return last

    def _run_brain(self, argv: list[str]) -> RunResult:
        return self._runner([self.alfred_bin, "brain", *argv])

    def _get_plan(self, plan_id: str) -> PlanDraft | None:
        try:
            return self._reader().get_plan(plan_id)
        except Exception:
            return None

    def _run_trusted(self) -> ControlResult:
        snapshot = (
            self.trust_store.snapshot(
                operator_user_id=self.operator_user_id,
                env_trusted_user_ids=env_trusted_user_ids(),
            )
            if self.trust_store is not None
            else trusted_users_snapshot(operator_user_id=self.operator_user_id)
        )
        lines = ["*Trusted Slack collaborators*", ""]
        if not snapshot.users:
            lines.append("_No trusted Slack users configured._")
        else:
            for user in snapshot.users:
                source = ", ".join(user.sources)
                removable = " · local" if user.can_remove else ""
                lines.append(f"- `<@{user.user_id}>` — {source}{removable}")
        lines.extend(
            [
                "",
                "The operator can add someone with `trust @person` and remove local entries with `untrust @person`.",
            ]
        )
        return ControlResult(True, "trusted", text="\n".join(lines))

    def _run_schedule(self, raw_args: str, actor_user_id: str | None) -> ControlResult:
        args = raw_args.split() if raw_args.strip() else ["list"]
        subcommand = args[0].lower() if args else "list"
        if subcommand == "set":
            actor = normalize_slack_user_id(actor_user_id)
            if not self.operator_user_id or actor != self.operator_user_id:
                return ControlResult(
                    True,
                    "schedule_rejected",
                    text="*Only the operator can change agent schedules.*\n\nNothing changed.",
                    detail="actor is not the operator",
                )
        result = self._runner([self.alfred_bin, "schedule", *args])
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "schedule command failed").strip()
            return ControlResult(
                True,
                "schedule_failed",
                text=f"*Schedule update failed.*\n\n```{_short(err, 700)}```",
                detail=err,
            )
        body = (result.stdout or "").strip()
        return ControlResult(
            True,
            "schedule",
            text=_short(body, 1200) if body else "*Schedule command completed.*",
        )

    # -- mutating commands (validated, shell-free) ------------------------

    def _run_trust_mutation(
        self,
        verb: str,
        target_user_id: str,
        actor_user_id: str | None,
    ) -> ControlResult:
        actor = normalize_slack_user_id(actor_user_id)
        if not self.operator_user_id:
            return ControlResult(
                True,
                f"{verb}_rejected",
                text=(
                    "*Trusted collaborator changes need an operator*\n\n"
                    "Set `ALFRED_OPERATOR_SLACK_USER_ID` first. Nothing changed."
                ),
                detail="operator user id is not configured",
            )
        if actor != self.operator_user_id:
            return ControlResult(
                True,
                f"{verb}_rejected",
                text="*Only the operator can change trusted Slack collaborators.*\n\nNothing changed.",
                detail="actor is not the operator",
            )
        store = self.trust_store or SlackTrustStore.from_state_root(default_trust_state_root())
        try:
            if verb == "trust":
                added, _user = store.add(target_user_id, added_by=actor or self.operator_user_id)
                action = "added" if added else "already trusted"
                return ControlResult(
                    True,
                    "trust",
                    text=(
                        f"*Trusted collaborator {action}:* `<@{target_user_id}>`.\n\n"
                        "They can now revise planning threads and send planning requests. "
                        "Only the operator can approve execution."
                    ),
                )
            removed = store.remove(target_user_id)
        except ValueError as exc:
            return ControlResult(
                True,
                f"{verb}_rejected",
                text=f"*Rejected:* `{_short(target_user_id)}` is not a Slack user id.",
                detail=str(exc),
            )
        if removed:
            return ControlResult(
                True,
                "untrust",
                text=f"*Removed local trusted collaborator:* `<@{target_user_id}>`.",
            )
        return ControlResult(
            True,
            "untrust",
            text=(
                f"*No local collaborator entry for* `<@{target_user_id}>`.\n\n"
                "If they are still trusted through environment config, remove them from "
                "`ALFRED_TRUSTED_SLACK_USER_IDS` and restart the listener."
            ),
        )

    def _run_manual_agent(self, codename: str, actor_user_id: str | None) -> ControlResult:
        if codename == "all":
            return ControlResult(
                True,
                "run_rejected",
                text=(
                    "*Rejected:* `run all` is intentionally unavailable.\n\n"
                    "Trigger one agent at a time, e.g. `run batman`."
                ),
                detail="run all rejected",
            )
        if not self._is_known_run_codename(codename):
            return ControlResult(
                False,
                "not_a_command",
                detail=f"unknown run codename {codename}",
            )
        actor = normalize_slack_user_id(actor_user_id)
        if not self.operator_user_id or actor != self.operator_user_id:
            return ControlResult(
                True,
                "run_rejected",
                text=(
                    "*Only the operator can manually run an agent.*\n\n"
                    "A manual run can kill an in-flight firing before kickstarting "
                    "the scheduler unit. Nothing changed."
                ),
                detail="actor is not the operator",
            )
        return self._run_agent_cli("run", codename)

    def _is_known_run_codename(self, codename: str) -> bool:
        known = self._known_run_codenames
        if known is None:
            known = _discover_run_codenames(self.alfred_bin)
            self._known_run_codenames = known
        return codename in known

    def _run_agent_cli(self, verb: str, codename: str) -> ControlResult:
        # Re-validate at the boundary even though the parser already did:
        # nothing reaches the argv without passing this check.
        if not is_valid_codename(codename):
            return ControlResult(
                True,
                f"{verb}_rejected",
                text=f"*Rejected:* `{_short(codename)}` is not a valid codename.",
                detail="invalid codename",
            )
        result = self._runner([self.alfred_bin, verb, codename])
        if result.returncode == 0:
            body = (result.stdout or "").strip()
            tail = f"\n```\n{_short(body, 600)}\n```" if body else ""
            return ControlResult(
                True,
                verb,
                text=f"*{_agent_cli_success_title(verb)}* `{codename}`.{tail}",
            )
        err = (result.stderr or result.stdout or "").strip()
        return ControlResult(
            True,
            f"{verb}_failed",
            text=(f"*Could not {verb}* `{codename}`.\n```\n{_short(err, 600) or 'no output'}\n```"),
            detail=err,
        )

    # -- default subprocess runner ---------------------------------------

    def _default_runner(self, argv: list[str]) -> RunResult:
        """Run ``argv`` with no shell. Never raises; surfaces failures."""
        try:
            # Explicit argv, no shell; the codename is charset-validated before
            # it ever reaches this vector, so it cannot inject a flag or command.
            cp = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return RunResult(returncode=1, stdout="", stderr=f"{type(exc).__name__}: {exc}")
        return RunResult(returncode=cp.returncode, stdout=cp.stdout or "", stderr=cp.stderr or "")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _usage_for_malformed(text: str) -> str | None:
    """Usage hint when a message leads with a known verb but cannot parse.

    Returns ``None`` when the message does not lead with a control verb at all
    (so the caller falls through to planning intake).
    """
    cleaned = _strip_mentions(text).strip()
    if not cleaned:
        return None
    first = cleaned.split()[0].lstrip("/!").lower()
    if first not in _COMMANDS:
        return None
    if first == "dryrun":
        first = "dry-run"
    if first in {"pause", "resume", "run", "dry-run"}:
        if first in {"run", "dry-run"} and len(cleaned.split()) > 2:
            return None
        return (
            f"*Usage:* `{first} <codename>`\n\n"
            f"Give exactly one agent codename"
            f"{' (or `all`)' if first in {'pause', 'resume', 'dry-run'} else ''}, "
            f"e.g. `{first} lucius`."
        )
    if first in {"trust", "untrust"}:
        return (
            f"*Usage:* `{first} <@user>`\n\n"
            "Only the configured operator can change trusted collaborators."
        )
    if first in {"plan", "draft", "handled"}:
        # Natural-language intake often starts with "plan ..." or "draft ...".
        # Only short malformed control attempts get usage; multi-word prose
        # falls back to the planning listener.
        if len(cleaned.split()) > 2:
            return None
        return f"*Usage:* `{first} <plan-id>`\n\nUse `plans` to find the exact local inbox id."
    if first == "schedule":
        parts = cleaned.split()
        if len(parts) > 1 and parts[1].lower() not in {"list", "show", "set"}:
            return None
        return (
            "*Usage:* `schedule list`, `schedule show <agent>`, or "
            "`schedule set <agent> <cadence>`\n\n"
            "Cadence examples: `10m`, `2h`, `daily@09:00`, `weekly@mon:09:00`."
        )
    if first == "assign":
        return (
            "*Usage:* `assign owner/repo#123`\n\n"
            "Only the operator can route an issue to Batman or Lucius."
        )
    if first == "remember":
        return render_remember_usage()
    if first in {"status", "runs", "plans", "trusted", "help"}:
        return None
    return render_help()


def render_help() -> str:
    return "\n".join(
        [
            "*Talk to Alfred naturally*",
            "",
            "DM me or @-mention me with normal requests:",
            '- "How is the fleet doing?"',
            '- "What shipped today?"',
            '- "What is blocked in the planning inbox?"',
            '- "Run Batman now."',
            '- "Dry-run Lucius before I change the schedule."',
            '- "Pause Nightwing for a bit."',
            '- "Change Lucius to every 20 minutes."',
            '- "Assign example-org/alfred#123 to the right agent."',
            '- "Queue example-org/alfred#123 for the fleet."',
            '- "Put that issue on hold."',
            '- "Remember for Alfred: Slack confirmations must stay explicit."',
            "",
            "For actions that change GitHub or schedules, I will ask for a "
            "confirmation reaction before anything changes. Dry-runs and status "
            "questions answer directly.",
            "",
            "Power-user shorthand still exists for exact local operations like "
            "`status`, `runs`, `plans`, `memory`, and trust management.",
        ]
    )


def render_fleet_status(data: dict[str, Any]) -> str:
    agents = data.get("agents")
    agents = agents if isinstance(agents, list) else []
    raw_globals = data.get("global")
    globals_: dict[str, Any] = raw_globals if isinstance(raw_globals, dict) else {}

    lines = ["*Fleet status*", ""]
    if not agents:
        lines.append("_No agents configured._")
    else:
        loaded = sum(1 for a in agents if _truthy(a.get("loaded")))
        paused = sum(1 for a in agents if _agent_paused(a))
        lines.append(f"*Agents:* {len(agents)} configured, {loaded} loaded, {paused} paused")
        for agent in agents[:20]:
            name = _agent_display_label(agent)
            state = _agent_state_label(agent)
            fired = _last_fired_label(agent)
            lines.append(f"- {name}: {state}{fired}")
        if len(agents) > 20:
            lines.append(f"- ...and {len(agents) - 20} more.")

    locks = globals_.get("locks") if isinstance(globals_.get("locks"), list) else []
    if locks:
        lines.extend(["", f"*Active locks:* {len(locks)}"])
    paused_repos = globals_.get("paused_repos")
    if isinstance(paused_repos, list) and paused_repos:
        lines.extend(["", "*Paused repos:* " + ", ".join(f"`{r}`" for r in paused_repos[:10])])
    return "\n".join(lines)


def render_recent_runs(data: dict[str, Any]) -> str:
    agents = data.get("agents")
    agents = agents if isinstance(agents, list) else []
    fired = [a for a in agents if a.get("last_fired")]
    fired.sort(key=lambda a: str(a.get("last_fired") or ""), reverse=True)

    lines = ["*Recent firings*", ""]
    if not fired:
        lines.append("_No agent has fired recently._")
        return "\n".join(lines)
    for agent in fired[:15]:
        name = _agent_display_label(agent)
        last = str(agent.get("last_fired") or "?")
        today = agent.get("today_firings")
        ok = agent.get("today_successes")
        fail = agent.get("today_failures")
        counts = _run_counts_label(today, ok, fail)
        lines.append(f"- {name}: last fired {last}{counts}")
    return "\n".join(lines)


def _agent_identifier(agent: dict[str, Any]) -> str:
    raw = agent.get("codename") or agent.get("name") or agent.get("agent")
    return str(raw or "").strip() or "?"


def _agent_display_label(agent: dict[str, Any]) -> str:
    codename = _agent_identifier(agent)
    if codename == "?":
        return "`?`"
    profile = agent_profile(codename)
    explicit_role = str(agent.get("role_title") or "").strip()
    role_title = explicit_role or profile.role_title
    if role_title:
        display_name = str(agent.get("display_name") or profile.display_name).strip()
        display_name = display_name or codename
        return f"*{display_name} · {role_title}* (`{codename}`)"
    return f"`{codename}`"


def _agent_cli_success_title(verb: str) -> str:
    if verb == "pause":
        return "Paused"
    if verb == "resume":
        return "Resumed"
    if verb == "run":
        return "Triggered"
    if verb == "dry-run":
        return "Dry-run"
    return verb.capitalize()


def _discover_run_codenames(alfred_bin: str) -> frozenset[str]:
    codenames = set(_DEFAULT_RUN_CODENAMES)
    for raw in os.environ.get("ALFRED_SLACK_RUN_CODENAMES", "").split(","):
        candidate = raw.strip()
        if is_valid_codename(candidate):
            codenames.add(candidate)
    for path in _agents_conf_candidates(alfred_bin):
        codenames.update(_codenames_from_agents_conf(path))
    for path in _fleet_yaml_candidates(alfred_bin):
        codenames.update(_codenames_from_fleet_yaml(path))
    return frozenset(codenames)


def _agents_conf_candidates(alfred_bin: str) -> list[Path]:
    repo_root = _repo_root_from_bin(alfred_bin)
    candidates: list[Path] = []
    for env_name in ("ALFRED_REPO", "ALFRED_HOME", "HERMES_HOME"):
        raw = os.environ.get(env_name)
        if raw:
            root = Path(raw).expanduser()
            candidates.extend(
                [
                    root / "infra" / "agents" / "launchd" / "agents.conf",
                    root / "launchd" / "agents.conf",
                ]
            )
    candidates.extend(
        [
            repo_root / "infra" / "agents" / "launchd" / "agents.conf",
            repo_root / "launchd" / "agents.conf",
            repo_root / "launchd" / "agents.conf.example",
        ]
    )
    return _existing_unique_paths(candidates)


def _fleet_yaml_candidates(alfred_bin: str) -> list[Path]:
    repo_root = _repo_root_from_bin(alfred_bin)
    candidates: list[Path] = []
    for env_name in ("ALFRED_REPO", "ALFRED_HOME", "HERMES_HOME"):
        raw = os.environ.get(env_name)
        if raw:
            root = Path(raw).expanduser()
            candidates.extend(
                [
                    root / "infra" / "agents" / "fleet.yaml",
                    root / "fleet.yaml",
                ]
            )
    candidates.extend(
        [
            repo_root / "infra" / "agents" / "fleet.yaml",
            repo_root / "fleet.yaml",
        ]
    )
    return _existing_unique_paths(candidates)


def _repo_root_from_bin(alfred_bin: str) -> Path:
    path = Path(alfred_bin).expanduser()
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    for parent in (path.parent, *path.parents):
        if (parent / "infra" / "agents").exists() or (parent / "launchd").exists():
            return parent
    return path.parent.parent if path.parent != path.parent.parent else path.parent


def _existing_unique_paths(candidates: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _codenames_from_agents_conf(path: Path) -> set[str]:
    codenames: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return codenames
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
            if "\t" not in line:
                continue
        if "\t" not in line:
            continue
        label = line.split("\t", 1)[0].strip()
        codename = label.rsplit(".", 1)[-1]
        if is_valid_codename(codename):
            codenames.add(codename)
    return codenames


def _codenames_from_fleet_yaml(path: Path) -> set[str]:
    codenames: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return codenames
    in_agents = False
    for line in lines:
        if line == "agents:":
            in_agents = True
            continue
        if in_agents and line and not line.startswith(" "):
            break
        if not in_agents:
            continue
        match = _FLEET_YAML_AGENT_RE.match(line)
        if match and is_valid_codename(match.group(1)):
            codenames.add(match.group(1))
    return codenames


def render_plans(rows: list[PlanDraft]) -> str:
    lines = ["*Planning inbox*", ""]
    if not rows:
        lines.append("_No saved plans, drafts, or follow-ups._")
        lines.extend(
            [
                "",
                "Send Alfred a planning request in DM or mention Alfred in a channel to start one.",
            ]
        )
        return "\n".join(lines)

    for plan in rows[:10]:
        title = _short(plan.title or plan.plan_id, 90)
        meta = [
            _source_label(plan),
            plan.status or "unknown",
            _plan_updated_label(plan),
        ]
        readiness = _readiness_label(plan)
        if readiness:
            meta.append(readiness)
        lines.append(f"- `{plan.plan_id}`: *{title}* ({', '.join(meta)})")

    lines.extend(
        [
            "",
            "Use `plan <id>` to inspect one. Captured follow-ups can become drafts with `draft <id>`.",
        ]
    )
    return "\n".join(lines)


def render_plan_detail(plan: PlanDraft) -> str:
    lines = [
        f"*{_short(plan.title or plan.plan_id, 120)}*",
        "",
        f"- Id: `{plan.plan_id}`",
        f"- Source: {_source_label(plan)}",
        f"- Status: `{plan.status or 'unknown'}`",
    ]
    updated = _plan_updated_label(plan)
    if updated:
        lines.append(f"- Updated: {updated}")
    if plan.parent:
        lines.append(f"- Parent: {plan.parent}")
    if plan.affected_repos:
        lines.append(f"- Repos: {plan.affected_repos}")
    readiness = _readiness_label(plan)
    if readiness:
        lines.append(f"- Readiness: {readiness}")
    if plan.revision_count:
        lines.append(f"- Revisions: {plan.revision_count}")

    preview = _short(plan.preview or plan.content, 700)
    if preview:
        lines.extend(["", "*Preview*", preview])

    lines.extend(["", "*Next actions*"])
    if plan.source == "followup":
        lines.append(
            f"- `draft {plan.plan_id}` to turn this follow-up into a local planning draft."
        )
        lines.append(f"- `handled {plan.plan_id}` to archive it without drafting.")
    else:
        lines.append(
            "- Reply in the Slack planning thread to revise scope, or continue from the local client."
        )
        lines.append("- Approval still uses Alfred's existing human gate.")
    return "\n".join(lines)


def render_memory_review(
    candidates: list[dict[str, Any]],
    promotions: list[dict[str, Any]],
) -> str:
    lines = ["*Memory review*", ""]
    if not candidates and not promotions:
        lines.append("_No pending memory candidates or promotion suggestions._")
    if candidates:
        lines.append("*Pending candidates*")
        for item in candidates[:5]:
            lines.append(_render_memory_row(item))
    if promotions:
        if candidates:
            lines.append("")
        lines.append("*Suggested promotions*")
        for item in promotions[:5]:
            lines.append(_render_memory_row(item))
    lines.extend(
        [
            "",
            "Use `remember owner/repo: lesson` to queue a candidate.",
            "The operator can run `memory promote <id>` or `memory reject <id>`. Redis is checked with `memory redis`.",
        ]
    )
    return "\n".join(lines)


def render_memory_promotions(items: list[dict[str, Any]]) -> str:
    lines = ["*Memory promotion suggestions*", ""]
    if not items:
        lines.append("_No high-confidence promotion suggestions right now._")
    else:
        for item in items[:10]:
            lines.append(_render_memory_row(item))
    lines.extend(["", "Use `memory promote <id>` after reviewing the evidence."])
    return "\n".join(lines)


def render_redis_status(health: dict[str, Any]) -> str:
    ok = bool(health.get("ok"))
    lines = [
        "*Redis Agent Memory Server*",
        "",
        f"- Status: {'ok' if ok else 'unavailable'}",
        f"- URL: `{health.get('base_url') or 'unknown'}`",
        f"- Namespace: `{health.get('namespace') or 'alfred'}`",
    ]
    response = health.get("response")
    if isinstance(response, dict):
        detail = response.get("status") or response.get("version") or response.get("service")
        if detail:
            lines.append(f"- Server: `{detail}`")
    if not ok and health.get("error"):
        lines.append(f"- Error: `{_short(str(health['error']), 180)}`")
    lines.extend(
        [
            "",
            "Redis AMS is optional. Alfred still uses local fleet-brain unless you explicitly configure and sync Redis memory.",
        ]
    )
    return "\n".join(lines)


def render_redis_sync(payload: dict[str, Any]) -> str:
    dry_run = bool(payload.get("dry_run"))
    matched = payload.get("matched", 0)
    synced = payload.get("synced", 0)
    failed = payload.get("failed") if isinstance(payload.get("failed"), list) else []
    title = "Redis memory sync preview" if dry_run else "Redis memory sync complete"
    lines = [
        f"*{title}*",
        "",
        f"- Reviewed lessons matched: `{matched}`",
        f"- {'Would sync' if dry_run else 'Synced'}: `{synced}`",
    ]
    if failed:
        lines.append(f"- Failed: {', '.join(f'`{_short(str(x), 40)}`' for x in failed[:10])}")
    if dry_run:
        lines.append("")
        lines.append("Run `memory sync now` to mirror reviewed lessons to Redis AMS.")
    return "\n".join(lines)


def render_memory_harvest(payload: dict[str, Any]) -> str:
    applied = bool(payload.get("applied"))
    queued = int(payload.get("queued") or 0)
    proposals = _ensure_dict_list(payload.get("proposals"))
    if applied:
        title = "Memory harvest queued" if queued > 0 else "Memory harvest finished"
    else:
        title = "Memory harvest preview"
    lines = [f"*{title}*", ""]
    if not proposals:
        lines.append("_No repeated failure patterns are ready to harvest._")
    else:
        for item in proposals[:8]:
            status = str(item.get("status") or "preview")
            candidate = item.get("candidate_id")
            candidate_text = f" `{candidate}`" if candidate else ""
            lines.append(
                f"- `{status}`{candidate_text} — `{item.get('codename') or item.get('agent')}/{item.get('repo')}`: "
                f"{_short(str(item.get('body') or ''), 180)}"
            )
        if len(proposals) > 8:
            lines.append(f"- _{len(proposals) - 8} more candidate(s) not shown._")
    lines.append("")
    if not proposals:
        lines.append("Nothing was queued.")
    elif applied and queued > 0:
        lines.append("Review queued candidates with `memory`, then promote only the useful ones.")
    elif applied:
        lines.append("No new candidates were queued.")
    else:
        lines.append("Run `memory harvest now` to queue these as reviewable candidates.")
    return "\n".join(lines)


def render_memory_usage() -> str:
    return "\n".join(
        [
            "*Memory commands*",
            "",
            "- `memory` / `memories`: pending candidates and suggested promotions.",
            "- `memory promotions`: high-confidence candidates with evidence.",
            "- `memory harvest`: preview failure-pattern candidates; `memory harvest now` queues them.",
            "- `remember [owner/repo:] <lesson>` or `memory remember ...`: queue a reviewable candidate.",
            "- `memory promote <id>`: operator-only: trust a candidate for future recall.",
            "- `memory reject <id> [note]`: operator-only: reject a noisy candidate.",
            "- `memory redis`: check optional Redis AMS.",
            "- `memory sync`: preview Redis sync; `memory sync now` writes reviewed lessons.",
        ]
    )


def render_remember_usage() -> str:
    return "\n".join(
        [
            "*Usage:* `remember [owner/repo:] <lesson>`",
            "",
            "Examples:",
            "- `remember example-org/alfred: Slack planning replies must stay reviewable.`",
            "- `memory remember example-org/alfred: keep the namespace discoverable.`",
            "- `remember Keep memory candidates out of prompt context until promoted.`",
        ]
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_memory_row(item: dict[str, Any]) -> str:
    candidate_id = _memory_row_id(item)
    actor = item.get("codename") or item.get("agent") or item.get("source") or "operator"
    repo = item.get("repo") or "global"
    confidence = item.get("confidence")
    score = item.get("score")
    metric = ""
    if isinstance(confidence, (int, float)):
        metric = f" confidence={confidence:.2f}"
    elif isinstance(score, (int, float)):
        metric = f" score={score:.2f}"
    body = _short(str(item.get("body") or item.get("summary") or item.get("topic") or ""), 180)
    return f"- `{candidate_id}` — `{actor}/{repo}`{metric}: {body}"


def _memory_row_id(item: dict[str, Any]) -> str:
    return str(item.get("candidate_id") or item.get("id") or "?")


def _memory_unavailable(error: str) -> ControlResult:
    return ControlResult(
        True,
        "memory_unavailable",
        text=f"*Memory unavailable*\n\n```{_short(error or 'No output', 700)}```",
        detail=error,
    )


def _parse_remember_payload(raw: str) -> tuple[str, str] | None:
    text = raw.strip()
    if not text:
        return None
    repo = "global"
    body = text
    lower = text.lower()
    if lower.startswith("repo="):
        first, _, tail = text.partition(" ")
        repo = first.split("=", 1)[1].rstrip(":").strip()
        body = tail.strip()
    else:
        first, _, tail = text.partition(" ")
        first_clean = first.rstrip(":")
        explicit_scope = first.endswith(":") or "/" in first_clean
        if explicit_scope and _is_memory_repo_token(first_clean) and tail.strip():
            repo = first_clean
            body = tail.strip()
        else:
            before, sep, after = text.partition(":")
            if sep and (before.strip() == "global" or "/" in before):
                repo_candidate = before.strip()
                if not _is_memory_repo_token(repo_candidate):
                    return None
                repo = repo_candidate
                body = after.strip()
    if not _is_memory_repo_token(repo) or not body:
        return None
    return repo, body


def _is_memory_repo_token(value: str) -> bool:
    if value == "global":
        return True
    if not _REPO_TOKEN_RE.match(value):
        return False
    owner, repo = value.split("/", 1)
    if "." in owner:
        return False
    return _is_safe_repo_segment(owner) and _is_safe_repo_segment(repo)


def _is_safe_repo_segment(value: str) -> bool:
    return not (
        not value
        or value in {".", ".."}
        or value.startswith(".")
        or value.endswith(".")
        or ".." in value
    )


def _json_or_none(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _ensure_dict_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _extract_candidate_id(raw: str) -> str | None:
    payload = _json_or_none(raw)
    if isinstance(payload, dict):
        candidate_id = payload.get("id") or payload.get("candidate_id")
        return str(candidate_id) if candidate_id else None
    match = re.search(r"(?:memory_candidate id=|proposed candidate\s+)([A-Za-z0-9:_-]+)", raw)
    return match.group(1) if match else None


def _agent_state_label(agent: dict[str, Any]) -> str:
    if _agent_paused(agent):
        return "paused"
    if _truthy(agent.get("loaded")):
        return "loaded"
    return "not loaded"


def _agent_paused(agent: dict[str, Any]) -> bool:
    if _truthy(agent.get("paused")):
        return True
    return str(agent.get("enable_state") or "").lower() == "paused"


def _last_fired_label(agent: dict[str, Any]) -> str:
    last = agent.get("last_fired")
    return f", last fired {last}" if last else ""


def _run_counts_label(today: Any, ok: Any, fail: Any) -> str:
    parts: list[str] = []
    if isinstance(today, int):
        parts.append(f"{today} today")
    if isinstance(ok, int):
        parts.append(f"{ok} ok")
    if isinstance(fail, int) and fail:
        parts.append(f"{fail} fail")
    return f" ({', '.join(parts)})" if parts else ""


def _source_label(plan: PlanDraft) -> str:
    if plan.source == "followup":
        return "captured follow-up"
    if plan.source == "planning":
        return "planning draft"
    return str(plan.source or "plan")


def _plan_updated_label(plan: PlanDraft) -> str:
    return str(plan.updated_at or "unknown time")


def _readiness_label(plan: PlanDraft) -> str:
    if plan.readiness_score is None and plan.readiness_ok is None:
        return ""
    score = "?" if plan.readiness_score is None else str(plan.readiness_score)
    if plan.readiness_ok is True:
        state = "ready"
    elif plan.readiness_ok is False:
        state = "needs scope"
    else:
        state = "not checked"
    return f"{state} ({score}/100)"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "loaded"}
    return bool(value)


def _strip_mentions(text: str) -> str:
    # Remove bot mentions used to address Alfred at the beginning of the
    # message, but keep later mentions because `trust <@user>` needs the target.
    return re.sub(r"^(?:<@[^>]+>\s*)+", "", str(text or "")).strip()


def _short(text: str, n: int = 80) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _issue_link(repo: str, number: int) -> str:
    """Slack mrkdwn link for a GitHub issue."""
    return f"<https://github.com/{repo}/issues/{number}|{repo}#{number}>"


def _assignment_ack_text(repo: str, number: int, detail: str) -> str:
    """Human Slack acknowledgement after Alfred routes an issue."""
    return "\n".join(
        [
            ":label: *Issue routed*",
            f"*Issue:* {_issue_link(repo, number)}",
            f"*Result:* {detail}",
        ]
    )


def _default_alfred_bin() -> str:
    """Best-effort path to the ``alfred`` CLI shipped next to ``lib/``."""
    here = Path(__file__).resolve().parent
    candidate = here.parent / "bin" / "alfred"
    if candidate.exists():
        return str(candidate)
    return "alfred"


def is_control_message(text: str) -> bool:
    """Cheap predicate: does this message lead with a known control verb?

    Note this returns True for a bare ``pause`` with no/invalid codename so
    the listener can answer with usage help rather than silently treating it
    as a planning request. :func:`parse_control_command` is stricter.
    """
    cleaned = _strip_mentions(text).strip()
    if not cleaned:
        return False
    first = cleaned.split()[0].lstrip("/!").lower()
    return first in _COMMANDS


__all__ = [
    "ControlCommand",
    "ControlResult",
    "RunResult",
    "SlackControlHandler",
    "is_control_message",
    "is_valid_codename",
    "is_valid_memory_id",
    "is_valid_plan_id",
    "parse_control_command",
    "render_fleet_status",
    "render_help",
    "render_plan_detail",
    "render_plans",
    "render_recent_runs",
]
