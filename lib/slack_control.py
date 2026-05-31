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
        "trusted",
        "trust",
        "untrust",
        "help",
    }
)

_PLAN_ID_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,180}$")

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
    verb = tokens[0].lstrip("/!").lower()
    if verb not in _COMMANDS:
        return None
    args = tokens[1:]

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
    return render_help()


def render_help() -> str:
    return "\n".join(
        [
            "*Alfred control commands*",
            "",
            "Lead a message with one of these verbs (DM me or @-mention me):",
            "- `status` — fleet health (loaded agents, pauses, locks).",
            "- `runs` — recent firings per agent.",
            "- `plans` — local planning inbox.",
            "- `plan <id>` — inspect a draft or captured follow-up.",
            "- `draft <id>` — convert a captured follow-up to a local draft.",
            "- `handled <id>` — operator-only: archive a follow-up without drafting.",
            "- `trusted` — list Slack users who can revise plans.",
            "- `trust <@user>` — operator-only: add a planning collaborator.",
            "- `untrust <@user>` — operator-only: remove a local collaborator.",
            "- `pause <codename>` — stop scheduled firings for one agent (or `all`).",
            "- `resume <codename>` — reverse a pause.",
            "- `help` — show this list.",
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


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


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
    state = "ready" if plan.readiness_ok else "needs scope"
    return f"{state} ({score}/10)"


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
    "is_valid_plan_id",
    "parse_control_command",
    "render_fleet_status",
    "render_help",
    "render_plan_detail",
    "render_plans",
    "render_recent_runs",
]
