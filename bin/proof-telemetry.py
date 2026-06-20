#!/usr/bin/env python3
"""Scheduler-facing wrapper for the anonymous proof-telemetry reporter.

This is the script a launchd/cron entry points at. The reporter is enabled by
default once an ingest endpoint is configured. Users can opt out by setting
``ALFRED_TELEMETRY_ENABLED=0`` or by running ``alfred telemetry off``. With the
switch off it prints a one-line sentinel and exits 0 without generating an
install id, reading the brain, or touching the network.

The scheduler entry can be present and loaded. If no custom ingest URL is
configured, the job uses Alfred's hosted collector unless the hosted default is
explicitly disabled.

Exit code is always 0. Telemetry is best-effort; a failure here must never
surface as a scheduler error or break anything else on the host.

Under ``ALFRED_DOCTOR=1`` the script takes a doctor fast path: it does a
lightweight config check and exits 0 WITHOUT building a payload, reading the
brain, or touching the network. This mirrors the other ``bin/*.py`` agents so
``bin/doctor.sh`` (which runs every configured agent under ``ALFRED_DOCTOR=1``)
sees a recognized sentinel instead of an accidental ``[PROOF-TELEMETRY-SENT]``
/ ``[PROOF-TELEMETRY-FAILED]`` from a real report during a health check.

Sentinels (printed to stdout, picked up by log scrapers):

    [PROOF-TELEMETRY-DISABLED]      master switch explicitly off
    [PROOF-TELEMETRY-DOCTOR-OK]     doctor fast path, enabled and config present
    [PROOF-TELEMETRY-NO-URL]        enabled but no collector URL is available
    [PROOF-TELEMETRY-NO-INSTALL-ID] enabled but the install id could not be
                                    persisted; report skipped so an ephemeral id
                                    does not inflate the install count
    [PROOF-TELEMETRY-STALE-COUNTS]  local brain read was incomplete; previous
                                    accepted totals stay in place
    [PROOF-TELEMETRY-SENT]          payload posted and accepted
    [PROOF-TELEMETRY-FAILED]        enabled and attempted, post did not succeed
    [PROOF-TELEMETRY-ERROR]         unexpected internal error, swallowed
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Add fallback install libs first, then the library beside this script, so a
# worktree run always exercises the matching checkout instead of a stale
# ~/.alfred/lib copy.
for candidate in (Path(os.environ.get("ALFRED_HOME", "")) / "lib", HERE.parent / "lib"):
    if candidate.exists():
        path = str(candidate.resolve())
        sys.path[:] = [entry for entry in sys.path if entry != path]
        sys.path.insert(0, path)


_SENTINELS = {
    "disabled": "[PROOF-TELEMETRY-DISABLED]",
    "doctor_ok": "[PROOF-TELEMETRY-DOCTOR-OK]",
    "no_url": "[PROOF-TELEMETRY-NO-URL]",
    "no_install_id": "[PROOF-TELEMETRY-NO-INSTALL-ID]",
    "stale_counts": "[PROOF-TELEMETRY-STALE-COUNTS]",
    "sent": "[PROOF-TELEMETRY-SENT]",
    "failed": "[PROOF-TELEMETRY-FAILED]",
    "error": "[PROOF-TELEMETRY-ERROR]",
}


def _valid_env_key(key: str) -> bool:
    return bool(key) and not key[0].isdigit() and all(ch.isalnum() or ch == "_" for ch in key)


def _load_alfredrc_env() -> None:
    """Load ``~/.alfredrc`` for direct CLI invocations.

    Scheduled runs go through ``agent-launch``, which already exports this file.
    Direct commands such as ``proof-telemetry.py --dry-run`` need the same view
    of the opt-out switch without forcing users to invoke the launcher.
    """
    path = Path.home() / ".alfredrc"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _valid_env_key(key) or key in os.environ:
            continue
        quoted_single = value.startswith("'") and value.endswith("'")
        if quoted_single or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        if not quoted_single:
            value = value.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
        os.environ[key] = value


def _doctor_fast_path() -> int:
    """Lightweight health check for ``bin/doctor.sh`` (ALFRED_DOCTOR=1).

    doctor.sh invokes every configured agent with ``ALFRED_DOCTOR=1`` and
    expects a quick sentinel, NOT real work. Without this short-circuit an
    enabled install would run the full report path during a health check and
    emit ``[PROOF-TELEMETRY-SENT]`` / ``[PROOF-TELEMETRY-FAILED]``, which
    doctor.sh treats as "unexpected output" (a hard failure).

    The fast path does no payload build, no brain read, and no network POST:

      * switch explicitly off      -> ``[PROOF-TELEMETRY-DISABLED]`` (disabled)
      * enabled but URL unavailable -> ``[PROOF-TELEMETRY-NO-URL]`` (usually
        because ALFRED_DEFAULT_TELEMETRY_URL was set empty)
      * enabled and URL present    -> ``[PROOF-TELEMETRY-DOCTOR-OK]`` (✅ ok)
    """
    try:
        import proof_telemetry
    except Exception as exc:  # an import error must not break the doctor sweep
        print(f"{_SENTINELS['error']} import failed: {exc}")
        return 0

    if not proof_telemetry.is_enabled():
        print(f"{_SENTINELS['disabled']} (doctor: switch is off)")
        return 0
    if not proof_telemetry.telemetry_url():
        print(f"{_SENTINELS['no_url']} (doctor: enabled but no collector URL available)")
        return 0
    print(f"{_SENTINELS['doctor_ok']} (doctor: enabled, config present, no report sent)")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_alfredrc_env()

    # Doctor fast path first: bin/doctor.sh runs every agent under
    # ALFRED_DOCTOR=1 and must never trigger a real telemetry POST.
    if os.environ.get("ALFRED_DOCTOR") == "1":
        return _doctor_fast_path()

    parser = argparse.ArgumentParser(description="Anonymous proof-telemetry reporter.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the payload and print it, but never POST. Useful to see "
        "exactly what would be sent.",
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
        # always shows the user what the payload would look like. It only
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
            # install_id is the local install token; safe to show locally.
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
