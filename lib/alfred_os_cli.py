"""Small console entry point for installed alfred-os wheels."""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path

import agent_runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alfred-os")
    parser.add_argument(
        "--paths",
        action="store_true",
        help="print the installed agent_runner module path",
    )
    args = parser.parse_args(argv)

    if args.paths:
        print(Path(agent_runner.__file__).resolve())
        return 0

    version = importlib.metadata.version("alfred-os")
    print(f"alfred-os {version}")
    print("agent_runner:", Path(agent_runner.__file__).resolve())
    return 0
