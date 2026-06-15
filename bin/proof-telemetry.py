#!/usr/bin/env python3
"""Scheduler-facing wrapper for the opt-in proof-telemetry reporter.

This is the script a launchd/cron entry points at. It is a hard NO-OP unless
the operator has opted in by setting ``ALFRED_TELEMETRY_ENABLED=1``. With the
switch off (the default) it prints a one-line sentinel and exits 0 without
generating an install id, reading the brain, or touching the network.

Mirrors the off-by-default posture of ``memory-harvest.py`` and the opt-in
``damian`` runner: the scheduler entry can be present and loaded, yet the job
does nothing until the operator deliberately turns it on.

Exit code is always 0. Telemetry is best-effort; a failure here must never
surface as a scheduler error or break anything else on the host.

Sentinels (printed to stdout, picked up by log scrapers):

    [PROOF-TELEMETRY-DISABLED]   master switch off (the default)
    [PROOF-TELEMETRY-NO-URL]     enabled but ALFRED_TELEMETRY_URL unset
    [PROOF-TELEMETRY-SENT]       payload posted and accepted
    [PROOF-TELEMETRY-FAILED]     enabled and attempted, post did not succeed
    [PROOF-TELEMETRY-ERROR]      unexpected internal error, swallowed
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "lib", Path(os.environ.get("ALFRED_HOME", "")) / "lib"):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


_SENTINELS = {
    "disabled": "[PROOF-TELEMETRY-DISABLED]",
    "no_url": "[PROOF-TELEMETRY-NO-URL]",
    "sent": "[PROOF-TELEMETRY-SENT]",
    "failed": "[PROOF-TELEMETRY-FAILED]",
    "error": "[PROOF-TELEMETRY-ERROR]",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in proof-telemetry reporter.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the payload and print it, but never POST. Useful to see "
        "exactly what would be sent before opting in.",
    )
    args = parser.parse_args(argv)

    # Import lazily so even an import error degrades to a clean no-op exit.
    try:
        import proof_telemetry
    except Exception as exc:  # never let an import error break the scheduler job
        print(f"{_SENTINELS['error']} import failed: {exc}")
        return 0

    if args.dry_run:
        # Dry-run still respects the master switch for the network call, but
        # always shows the operator what the payload would look like. It only
        # generates/reads an install id when already enabled.
        if not proof_telemetry.is_enabled():
            print(f"{_SENTINELS['disabled']} (dry-run: nothing generated, switch is off)")
            return 0
        try:
            from fleet_brain import FleetBrain

            brain = FleetBrain.from_env()
            install_id = proof_telemetry.load_or_create_install_id()
            counts = proof_telemetry.derive_counts(brain)
            payload = proof_telemetry.build_payload(
                install_id, counts, proof_telemetry.current_period()
            )
            print(
                f"{_SENTINELS['no_url'] if not proof_telemetry.telemetry_url() else '[PROOF-TELEMETRY-DRY-RUN]'}"
            )
            # install_id is the operator's own token; safe to show locally.
            print(payload)
        except Exception as exc:  # dry-run is best-effort; never raise
            print(f"{_SENTINELS['error']} dry-run failed: {exc}")
        return 0

    result = proof_telemetry.report_once()
    status = result.get("status", "error")
    sentinel = _SENTINELS.get(status, _SENTINELS["error"])
    if status == "sent":
        counts = result.get("counts", {})
        print(f"{sentinel} period={result.get('period', '?')} counts={counts}")
    else:
        print(sentinel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
