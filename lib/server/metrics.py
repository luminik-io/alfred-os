"""Self-benchmark metrics payload for ``alfred serve``.

Thin projection over :mod:`benchmark` so the desktop Metrics view can read
the same four metric families (throughput / quality / reliability /
efficiency) plus the subscription-quota cost framing that
``alfred benchmark report --json`` emits, without shelling out to the CLI.

Every figure is read-only and derived from telemetry the fleet already left
on disk. An empty or missing state tree degrades to an honest
``available: false`` payload rather than fabricating numbers.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def unavailable_metrics_payload(reason: str) -> dict[str, Any]:
    """Honest empty shape when the benchmark report cannot be built."""
    return {
        "available": False,
        "unavailable_reason": reason,
        "label": None,
        "generated_at": None,
        "throughput": None,
        "quality": None,
        "reliability": None,
        "efficiency": None,
        "spend": None,
        "quota_cost": [],
    }


def build_metrics() -> dict[str, Any]:
    """Build the benchmark report payload (report.to_dict() + quota_cost).

    Mirrors ``render_report_json`` in ``bin/alfred-benchmark.py``: the full
    report dict with a ``quota_cost`` array appended, wrapped with an
    ``available`` flag for the client.
    """
    from benchmark import quota_cost_for_report, run_report
    from transcripts import default_state_dir

    state_dir = default_state_dir()
    if not state_dir.exists():
        return unavailable_metrics_payload(
            "No fleet telemetry yet. Run the suite at least once."
        )

    report = run_report(state_dir, label="serve")
    payload: dict[str, Any] = {"available": True}
    payload.update(report.to_dict())
    payload["quota_cost"] = [row.to_dict() for row in quota_cost_for_report(report)]
    # The desktop view renders the four families + quota cost; the full
    # observation list is large and not needed there, so drop it to keep the
    # response small. The CLI JSON keeps it for offline analysis.
    payload.pop("observations", None)
    return payload
