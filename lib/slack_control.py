"""Trusted Slack control + query commands for the fleet.

A trusted user (the founder/operator) can message the bot with a **leading
command verb** to act on the fleet without leaving Slack:

* ``status``               -> fleet health from ``alfred status --json``
* ``pause <codename>``     -> stop scheduled firings for one agent
* ``resume <codename>``    -> reverse a pause
* ``runs``                 -> recent firings (last-fired + today counts)
* ``plans``                -> local planning inbox
* ``plan <id>``            -> planning draft or follow-up detail
* ``draft <id>``           -> convert a captured follow-up to a local draft
* ``handled <id>``         -> operator-only: archive a captured follow-up
* ``memory``               -> reviewable memory queue + promotion suggestions
* ``remember ...``         -> stage a reviewable memory candidate from Slack
* ``memory promote <id>``  -> operator-only: promote a memory candidate
* ``memory reject <id>``   -> operator-only: reject a memory candidate
* ``memory redis``         -> check optional Redis Agent Memory Server
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

* **No shell, ever.** Commands that mutate state (``pause``/``resume``) run
  the ``alfred`` CLI through an explicit argv vector via ``subprocess.run``
  with ``shell=False``. The codename is validated against a strict charset
  (``[A-Za-z0-9._-]``, no leading ``-``) *before* it is placed in the argv,
  so it can never be read as a flag or inject a second command.

* **Queries are read-only.** ``status`` and ``runs`` shell out to
  ``alfred status --json`` only; they never change fleet state.

The CLI invocation is injected (``runner``) so tests exercise the full
parse + dispatch path without spawning a real process.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
# pause/resume CLI accepts it as a fleet-wide target.
_CODENAME_RE = re.compile(r"^(?!-)[A-Za-z0-9._-]{1,64}$")

# Known leading verbs. A message only becomes a control command when its first
# token (lowercased) is one of these.
_COMMANDS: frozenset[str] = frozenset(
    {
        "status",
        "pause",
        "resume",
        "runs",
        "plans",
        "plan",
        "draft",
        "handled",
        "memory",
        "memories",
        "remember",
        "trusted",
        "trust",
        "untrust",
        "help",
    }
)

_PLAN_ID_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,180}$")
_MEMORY_ID_RE = re.compile(r"^(?!-)[A-Za-z0-9:_-]{1,96}$")
_REPO_TOKEN_RE = re.compile(r"^(?!-)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

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

    # status / runs / trusted / help take no required argument; ignore any
    # trailing words so "status please" still reads as a status request.
    return ControlCommand(verb=verb)


def is_valid_codename(value: str) -> bool:
    """True iff ``value`` is a safe codename token for the CLI argv.

    Strict allowlist: ``[A-Za-z0-9._-]``, 1-64 chars, never leading ``-``.
    This is the injection guard for ``pause``/``resume``.
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
        if command.verb in {"trust", "untrust"}:
            return self._run_trust_mutation(command.verb, command.arg, actor_user_id)
        if command.verb in {"pause", "resume"}:
            return self._run_pause_resume(command.verb, command.arg)
        return ControlResult(False, "not_a_command", detail=f"unhandled verb {command.verb}")

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

    def _memory_candidates(self, *, limit: int) -> tuple[list[dict[str, Any]] | None, str]:
        data, error = self._run_brain_json_first(
            [
                ["candidates", "--status", "candidate", "--limit", str(limit), "--json"],
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

    def _run_pause_resume(self, verb: str, codename: str) -> ControlResult:
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
        verb_past = "paused" if verb == "pause" else "resumed"
        if result.returncode == 0:
            body = (result.stdout or "").strip()
            tail = f"\n```\n{_short(body, 600)}\n```" if body else ""
            return ControlResult(
                True,
                verb,
                text=f"*{verb_past.capitalize()}* `{codename}`.{tail}",
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
    if first in {"pause", "resume"}:
        return (
            f"*Usage:* `{first} <codename>`\n\n"
            f"Give exactly one agent codename (or `all`), e.g. `{first} lucius`."
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
    if first == "remember":
        return render_remember_usage()
    return render_help()


def render_help() -> str:
    return "\n".join(
        [
            "*Alfred control commands*",
            "",
            "Lead a message with one of these verbs (DM me or @-mention me):",
            "- `status`: fleet health (loaded agents, pauses, locks).",
            "- `runs`: recent firings per agent.",
            "- `plans`: local planning inbox.",
            "- `plan <id>`: inspect a draft or captured follow-up.",
            "- `draft <id>`: convert a captured follow-up to a local draft.",
            "- `handled <id>`: operator-only: archive a follow-up without drafting.",
            "- `memory` / `memories`: review memory candidates and promotion suggestions.",
            "- `memory promotions`: show high-confidence memory candidates.",
            "- `remember [repo:] <lesson>`: stage a reviewable memory candidate.",
            "- `memory promote <id>` / `memory reject <id>`: operator-only memory review.",
            "- `memory redis`: check optional Redis Agent Memory Server.",
            "- `memory sync`: preview Redis sync; `memory sync now` writes reviewed lessons.",
            "- `trusted`: list Slack users who can revise plans.",
            "- `trust <@user>`: operator-only: add a planning collaborator.",
            "- `untrust <@user>`: operator-only: remove a local collaborator.",
            "- `pause <codename>`: stop scheduled firings for one agent (or `all`).",
            "- `resume <codename>`: reverse a pause.",
            "- `help`: show this list.",
            "",
            "Anything without a leading command verb is treated as a planning "
            "request, not a control action.",
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
            name = str(agent.get("codename") or agent.get("name") or "?")
            state = _agent_state_label(agent)
            fired = _last_fired_label(agent)
            lines.append(f"- `{name}` — {state}{fired}")
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
        name = str(agent.get("codename") or agent.get("name") or "?")
        last = str(agent.get("last_fired") or "?")
        today = agent.get("today_firings")
        ok = agent.get("today_successes")
        fail = agent.get("today_failures")
        counts = _run_counts_label(today, ok, fail)
        lines.append(f"- `{name}` — last fired {last}{counts}")
    return "\n".join(lines)


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


def render_memory_usage() -> str:
    return "\n".join(
        [
            "*Memory commands*",
            "",
            "- `memory` / `memories`: pending candidates and suggested promotions.",
            "- `memory promotions`: high-confidence candidates with evidence.",
            "- `remember [owner/repo:] <lesson>`: queue a reviewable candidate.",
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
            "- `remember luminik-io/alfred-os: Slack planning replies must stay reviewable.`",
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
        if _is_memory_repo_token(first_clean) and tail.strip():
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
