from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load_cli_module():
    loader = importlib.machinery.SourceFileLoader("alfred_cli_for_test", str(ROOT / "bin/alfred"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_serve_forwards_supported_server_args(monkeypatch):
    cli = load_cli_module()
    calls = []

    def fake_run(command, check):
        calls.append((command, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(
            [
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "7010",
                "--no-browser",
                "--log-level",
                "debug",
            ]
        )
        == 0
    )

    assert calls == [
        (
            [
                sys.executable,
                str(ROOT / "bin/alfred-serve.py"),
                "--host",
                "127.0.0.1",
                "--port",
                "7010",
                "--no-browser",
                "--log-level",
                "debug",
            ],
            False,
        )
    ]
