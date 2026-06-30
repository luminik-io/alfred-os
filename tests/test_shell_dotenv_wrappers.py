import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _write_stub(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf 'operator=%s\\n' \"${OPERATOR_NAME:-}\"\n"
        "printf 'args=%s\\n' \"$*\"\n"
    )
    path.chmod(0o755)


def _wrapper_env(runtime: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["ALFRED_HOME"] = str(runtime)
    env.pop("OPERATOR_NAME", None)
    return env


def test_fleet_recap_loads_dotenv_without_executing_shell(tmp_path: Path) -> None:
    runtime = tmp_path / "alfred"
    (runtime / ".env").parent.mkdir(parents=True)
    (runtime / ".env").write_text("OPERATOR_NAME=Jane Doe\n")
    _write_stub(runtime / "bin" / "alfred-status.py")

    result = subprocess.run(
        [str(ROOT / "bin" / "fleet-recap.sh"), "--probe"],
        check=True,
        capture_output=True,
        text=True,
        env=_wrapper_env(runtime),
    )

    assert "operator=Jane Doe" in result.stdout
    assert "args=--slack --probe" in result.stdout


def test_shell_wrapper_process_env_wins_over_dotenv(tmp_path: Path) -> None:
    runtime = tmp_path / "alfred"
    (runtime / ".env").parent.mkdir(parents=True)
    (runtime / ".env").write_text("OPERATOR_NAME=Jane Doe\n")
    _write_stub(runtime / "bin" / "alfred-status.py")
    env = _wrapper_env(runtime)
    env["OPERATOR_NAME"] = "Process Override"

    result = subprocess.run(
        [str(ROOT / "bin" / "fleet-recap.sh")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "operator=Process Override" in result.stdout


def test_shipped_summary_wrappers_load_dotenv_without_sourcing(tmp_path: Path) -> None:
    runtime = tmp_path / "alfred"
    (runtime / ".env").parent.mkdir(parents=True)
    (runtime / ".env").write_text("OPERATOR_NAME=Jane Doe\n")
    _write_stub(runtime / "bin" / "alfred-shipped-summary.py")

    for script, period in (
        ("shipped-summary-daily.sh", "daily"),
        ("shipped-summary-weekly.sh", "weekly"),
    ):
        result = subprocess.run(
            [str(ROOT / "bin" / script), "--probe"],
            check=True,
            capture_output=True,
            text=True,
            env=_wrapper_env(runtime),
        )

        assert "operator=Jane Doe" in result.stdout
        assert f"args=--period {period} --slack --probe" in result.stdout
