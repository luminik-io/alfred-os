#!/usr/bin/env python3
"""
connector-sync - poll registered input connectors, file ``agent:implement`` issues.

Usage::

    connector-sync                                  # all enabled connectors
    connector-sync --connectors linear              # one connector
    connector-sync --connectors linear,sentry       # explicit list
    connector-sync --config examples/connectors.yaml
    connector-sync --dry-run                        # narrate without filing
    connector-sync --json                           # emit JSON report on stdout

Exit codes
----------
0 - sync ran; ``--json`` will reveal per-connector outcomes.
2 - config file missing or unparseable.
3 - at least one connector raised during ``poll`` or ``gh issue create``.

This script is intentionally thin: it parses args, loads the YAML
config, builds connector instances, and hands them to ``ConnectorRunner``.
All real logic lives in ``lib/connectors/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Make ``lib/`` importable. Resolution order: deployed ``$ALFRED_HOME/lib``
# (when present), else the in-repo ``lib/`` next to this script. The
# fallback lets ``python bin/connector-sync.py`` work in a freshly cloned
# checkout without running ``deploy.sh`` first.
_candidates = []
_env_home = os.environ.get("ALFRED_HOME")
if _env_home:
    _candidates.append(Path(_env_home) / "lib")
_candidates.append(Path(__file__).resolve().parent.parent / "lib")
for _cand in _candidates:
    if (_cand / "connectors" / "__init__.py").exists():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break

from connectors import Connector  # noqa: E402
from connectors.linear import LinearConnector  # noqa: E402
from connectors.runner import ConnectorRunner, SyncReport  # noqa: E402
from connectors.sentry import SentryConnector  # noqa: E402

logger = logging.getLogger("connector-sync")

# Registry: connector name -> factory(config dict) -> Connector. New
# connector authors register one entry here. Keeping the registry in
# the CLI (rather than a global) means library code stays import-only.
CONNECTOR_FACTORIES: dict[str, Any] = {
    "linear": LinearConnector,
    "sentry": SentryConnector,
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = _load_config(Path(args.config))
    except FileNotFoundError:
        print(f"connector-sync: config not found: {args.config}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"connector-sync: config invalid: {e}", file=sys.stderr)
        return 2

    requested = _parse_connector_list(args.connectors)
    connectors = _build_connectors(config, requested=requested)
    if not connectors:
        print("connector-sync: no connectors enabled or matched --connectors", file=sys.stderr)
        return 0

    runner = ConnectorRunner(connectors, dry_run=args.dry_run)
    report = runner.sync()

    if args.json:
        _emit_json_report(report)
    else:
        _emit_text_report(report, dry_run=args.dry_run)

    return 3 if report.failed_count else 0


# ---------------------------------------------------------------------------
# Argv
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="connector-sync",
        description="Drain Alfred input connectors and file agent:implement issues.",
    )
    p.add_argument(
        "--config",
        default=os.environ.get("ALFRED_CONNECTORS_CONFIG", "examples/connectors.yaml"),
        help="Path to connectors.yaml (default: $ALFRED_CONNECTORS_CONFIG or "
        "examples/connectors.yaml).",
    )
    p.add_argument(
        "--connectors",
        default="",
        help="Comma-separated subset of connectors to run (default: all enabled).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Narrate would-be gh issue create calls; do not file. Seen-cache "
        "is still updated so a subsequent live run does not re-fire.",
    )
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON report.")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging.")
    return p.parse_args(argv)


def _parse_connector_list(raw: str) -> set[str] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


# ---------------------------------------------------------------------------
# Config loader - small YAML subset, stdlib-only.
# ---------------------------------------------------------------------------


def _load_config(path: Path) -> dict[str, Any]:
    """Load the connectors config.

    Tries ``PyYAML`` if it's already installed; falls back to a tiny
    handwritten parser so the runtime stays zero-dep. The supported
    shape is intentionally narrow - see ``examples/connectors.yaml``.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text)
    except ImportError:
        data = _minimal_yaml_load(text)
    if not isinstance(data, dict) or "connectors" not in data:
        raise ValueError("config must be a mapping with a top-level `connectors` key")
    if not isinstance(data["connectors"], list):
        raise ValueError("`connectors` must be a list")
    return data


_YAML_KV_RE = re.compile(r"^(\s*)([A-Za-z0-9_\-]+)\s*:\s*(.*?)\s*$")
_YAML_ITEM_RE = re.compile(r"^(\s*)-\s+(.*?)\s*$")


def _minimal_yaml_load(text: str) -> dict[str, Any]:
    """Parse the narrow YAML subset our config uses.

    Supports:
      * mappings (``key: value``)
      * lists of mappings (``- key: value``)
      * scalars: strings (quoted or bare), ints, booleans, null
      * indentation-based nesting (2 spaces)

    Anything more exotic should rely on PyYAML being available; the
    operator's first ``pip install pyyaml`` opt-in unlocks full YAML.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    pending_key: list[tuple[int, str, dict]] = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        # List item?
        m_item = _YAML_ITEM_RE.match(line)
        if m_item:
            indent = len(m_item.group(1))
            _pop_to_indent(stack, indent)
            parent = stack[-1][1]
            if not isinstance(parent, list):
                raise ValueError(f"unexpected list item under non-list: {line!r}")
            item: dict[str, Any] = {}
            parent.append(item)
            rest = m_item.group(2)
            stack.append((indent + 2, item))
            if rest:
                _consume_kv(rest, indent + 2, stack)
            continue

        # Key/value?
        m_kv = _YAML_KV_RE.match(line)
        if not m_kv:
            raise ValueError(f"could not parse line: {raw_line!r}")
        indent = len(m_kv.group(1))
        key = m_kv.group(2)
        value = m_kv.group(3)
        _pop_to_indent(stack, indent)
        parent = stack[-1][1]
        if isinstance(parent, list):
            raise ValueError(f"mapping under a list without dash: {line!r}")
        if value == "":
            # Block scalar follows on next indent - could be mapping or list.
            container: Any = {}
            parent[key] = container
            stack.append((indent + 2, container))
            pending_key.append((indent, key, parent))
        elif value.startswith("["):
            parent[key] = _parse_flow_list(value)
        else:
            parent[key] = _parse_scalar(value)
    # Promote any pending-key containers that turned out to be lists
    # (detected when first child line was a dash; handled inline above).
    return root


def _consume_kv(rest: str, indent: int, stack: list[tuple[int, Any]]) -> None:
    m_kv = _YAML_KV_RE.match(" " * indent + rest)
    if not m_kv:
        return
    key = m_kv.group(2)
    value = m_kv.group(3)
    parent = stack[-1][1]
    if value == "":
        container: Any = {}
        parent[key] = container
        stack.append((indent + 2, container))
    elif value.startswith("["):
        parent[key] = _parse_flow_list(value)
    else:
        parent[key] = _parse_scalar(value)


def _pop_to_indent(stack: list[tuple[int, Any]], indent: int) -> None:
    while stack and stack[-1][0] >= indent:
        stack.pop()
    if not stack:
        raise ValueError("indentation underflow")


def _parse_scalar(value: str) -> Any:
    v = value.strip()
    if v == "" or v.lower() == "null" or v == "~":
        return None
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _parse_flow_list(value: str) -> list[Any]:
    v = value.strip()
    if not (v.startswith("[") and v.endswith("]")):
        raise ValueError(f"bad flow list: {value!r}")
    inner = v[1:-1].strip()
    if not inner:
        return []
    return [_parse_scalar(tok) for tok in _split_top_level(inner)]


def _split_top_level(s: str) -> list[str]:
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


# ---------------------------------------------------------------------------
# Connector construction
# ---------------------------------------------------------------------------


def _build_connectors(
    config: dict[str, Any],
    *,
    requested: set[str] | None,
) -> list[Connector]:
    out: list[Connector] = []
    for entry in config.get("connectors") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("type")
        if not name:
            logger.warning("skipping connector entry with no name/type: %r", entry)
            continue
        if not entry.get("enabled", True):
            logger.info("connector %s disabled in config; skipping", name)
            continue
        if requested is not None and name not in requested:
            continue
        factory = CONNECTOR_FACTORIES.get(entry.get("type") or name)
        if not factory:
            logger.warning("unknown connector type %r; skipping", name)
            continue
        kwargs = _connector_kwargs(entry)
        try:
            out.append(factory(**kwargs))
        except TypeError as e:
            logger.warning("connector %s: bad config: %s", name, e)
    return out


def _connector_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
    """Strip config-only keys; pass everything else as ctor kwargs."""
    kwargs = {k: v for k, v in entry.items() if k not in {"type", "enabled"}}
    # Re-key common config aliases onto Connector ctor names.
    if "repo" in kwargs and "default_repo" not in kwargs:
        kwargs["default_repo"] = kwargs.pop("repo")
    if "labels" in kwargs and "default_labels" not in kwargs:
        kwargs["default_labels"] = kwargs.pop("labels")
    return kwargs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _emit_text_report(report: SyncReport, *, dry_run: bool) -> None:
    header = "[DRY RUN] " if dry_run else ""
    print(
        f"{header}connector-sync: filed={report.filed_count} "
        f"skipped={report.skipped_count} failed={report.failed_count}"
    )
    for row in report.filed:
        marker = "(would-file)" if dry_run else "filed"
        print(f"  [{row.source}] {marker} {row.source_id} -> {row.issue_url} ({row.title!r})")
    for row in report.failed:
        print(f"  [{row.source}] FAILED {row.source_id}: {row.error}", file=sys.stderr)


def _emit_json_report(report: SyncReport) -> None:
    payload = {
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat() if report.finished_at else None,
        "filed": [asdict(r) for r in report.filed],
        "skipped": [asdict(r) for r in report.skipped],
        "failed": [asdict(r) for r in report.failed],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
