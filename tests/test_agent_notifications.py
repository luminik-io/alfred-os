"""Headless fleet agents suppress Claude Code notifications by default.

``claude_invoke`` adds ``--settings '{"agentPushNotifEnabled":false,
"preferredNotifChannel":"none"}'`` to every non-interactive firing so the
launchd-driven fleet does not spray macOS notification banners. The
``--settings`` flag ADDS a settings source — it does not replace auth
(auth comes from the config-dir credentials), so this is purely a
notification toggle. The operator opts back in with
``ALFRED_AGENT_NOTIFICATIONS=1``.

These tests pin the argv contract without invoking Claude: we monkeypatch
``run`` to capture the constructed command and short-circuit.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_LIB))

import agent_runner  # noqa: E402

_OK_STDOUT = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"stop_reason":"end_turn","num_turns":1,"total_cost_usd":0,"result":""}'
)


def _capture_claude_argv(monkeypatch) -> list[str]:
    """Invoke claude_invoke with a stubbed ``run`` and return the argv."""
    captured: dict = {}

    def fake_run(cmd, *, cwd=None, timeout=60, capture=True, env=None, **kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout=_OK_STDOUT, stderr="")

    with mock.patch.object(agent_runner, "run", fake_run):
        agent_runner.claude_invoke(
            prompt="hi",
            workdir=Path("/tmp"),
            allowed_tools="Read",
            max_turns=None,
            timeout=10,
        )
    return captured["cmd"]


def test_notif_suppression_flag_present_by_default(monkeypatch) -> None:
    """With the env var unset, the suppression --settings source is added."""
    monkeypatch.delenv("ALFRED_AGENT_NOTIFICATIONS", raising=False)
    cmd = _capture_claude_argv(monkeypatch)

    assert "--settings" in cmd, f"--settings must be present by default; got {cmd}"
    idx = cmd.index("--settings")
    settings_json = cmd[idx + 1]
    assert '"agentPushNotifEnabled":false' in settings_json
    assert '"preferredNotifChannel":"none"' in settings_json


def test_notif_flag_absent_when_opted_in(monkeypatch) -> None:
    """ALFRED_AGENT_NOTIFICATIONS=1 keeps notifications on (no --settings)."""
    monkeypatch.setenv("ALFRED_AGENT_NOTIFICATIONS", "1")
    cmd = _capture_claude_argv(monkeypatch)

    assert "--settings" not in cmd, (
        f"--settings must be omitted when ALFRED_AGENT_NOTIFICATIONS=1; got {cmd}"
    )


def test_notif_flag_does_not_replace_auth_or_other_flags(monkeypatch) -> None:
    """The suppression source is additive: it sits alongside the existing
    transport flags and never displaces auth/permission/output flags."""
    monkeypatch.delenv("ALFRED_AGENT_NOTIFICATIONS", raising=False)
    cmd = _capture_claude_argv(monkeypatch)

    # Core flags still present and unchanged.
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    # No credential/api-key flag is injected by the notification toggle.
    assert "--api-key" not in cmd
    assert "apiKeyHelper" not in " ".join(cmd)


def test_notif_opt_in_accepts_other_truthy_values(monkeypatch) -> None:
    """Truthy spellings (true/yes/on) also opt back in."""
    for val in ("true", "yes", "on", "TRUE"):
        monkeypatch.setenv("ALFRED_AGENT_NOTIFICATIONS", val)
        cmd = _capture_claude_argv(monkeypatch)
        assert "--settings" not in cmd, f"{val!r} should opt in; got {cmd}"
