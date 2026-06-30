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


def load_serve_module():
    loader = importlib.machinery.SourceFileLoader(
        "alfred_serve_for_test",
        str(ROOT / "bin/alfred-serve.py"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_serve_parser_defaults_to_desktop_port():
    serve = load_serve_module()

    args = serve._build_parser().parse_args(["--no-browser"])

    assert args.host == "127.0.0.1"
    assert args.port == 7010
    assert args.no_browser is True


def test_serve_forwards_supported_server_args(tmp_path, monkeypatch):
    cli = load_cli_module()
    calls = []

    def fake_run(command, check):
        calls.append((command, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
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


def test_serve_uses_managed_alfred_venv_when_present(tmp_path, monkeypatch):
    cli = load_cli_module()
    alfred_home = tmp_path / "alfred"
    venv_python = alfred_home / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    venv_python.chmod(0o755)
    calls = []

    def fake_run(command, check):
        calls.append((command, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("ALFRED_HOME", str(alfred_home))
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.main(["serve", "--no-browser"]) == 0

    assert calls == [
        (
            [
                str(venv_python),
                str(ROOT / "bin/alfred-serve.py"),
                "--no-browser",
            ],
            False,
        )
    ]
