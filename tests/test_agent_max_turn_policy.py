"""Guard the no-cap-by-default policy at the agent runner layer.

Background — agents in ``bin/`` previously hardcoded per-firing ``max_turns``
ceilings (e.g. ``max_turns=40``) that produced no-output runs on cross-file
work. The runners now pass ``optional_env_int(...)`` which returns None
unless the operator explicitly opts into a debug knob via the
``ALFRED_<AGENT>_MAX_TURNS`` env var. This test prevents a future patch
from silently re-introducing a literal cap that could recreate the loop.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_agent_scripts_do_not_hardcode_claude_max_turns() -> None:
    """Engineering agents should rely on wall-clock timeouts by default.

    Emergency ``ALFRED_<AGENT>_MAX_TURNS`` env knobs are allowed (they
    flow through ``optional_env_int``); a literal ``max_turns=40`` in
    an agent script recreates the no-output-cap loop where work
    repeatedly stops before reaching a useful artifact.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "bin").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"max_turns\s*=\s*\d+", text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)}")

    assert offenders == [], (
        "bin/<agent>.py runners must not hardcode a numeric max_turns. "
        "Use optional_env_int('ALFRED_<AGENT>_MAX_TURNS', minimum=...) "
        "instead so the wall-clock timeout is the only ceiling. "
        f"Offenders: {offenders}"
    )
