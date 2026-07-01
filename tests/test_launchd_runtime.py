"""Smoke tests for the launchd runtime wrapper and deploy/doctor parity."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {**os.environ, **overrides}
    env.pop("WORKSPACE_ROOT", None)
    return env


def test_agent_launch_loads_runtime_env_without_shell_eval(tmp_path):
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    bin_dir = alfred / "bin"
    home.mkdir()
    bin_dir.mkdir(parents=True)
    capture = tmp_path / "capture.json"
    marker = tmp_path / "command-substitution-ran"
    telemetry_token = f"tok$(touch {marker})&still"
    (alfred / ".env").write_text(
        "\n".join(
            [
                "WORKSPACE_ROOT=$HOME/work space",
                "OPERATOR_NAME=Example Operator",
                "export GH_ORG=acme",
                f"ALFRED_TELEMETRY_TOKEN='{telemetry_token}'",
                "QUOTE_TOKEN='can'\"'\"'quote'",
                "HOME_LITERAL='abc$HOMEdef-${HOME}'",
                "",
            ]
        )
    )
    target = bin_dir / "probe"
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"open({str(capture)!r}, 'w').write(json.dumps({{\n"
        "  'workspace': os.environ.get('WORKSPACE_ROOT'),\n"
        "  'operator': os.environ.get('OPERATOR_NAME'),\n"
        "  'gh_org': os.environ.get('GH_ORG'),\n"
        "  'telemetry_token': os.environ.get('ALFRED_TELEMETRY_TOKEN'),\n"
        "  'quote_token': os.environ.get('QUOTE_TOKEN'),\n"
        "  'home_literal': os.environ.get('HOME_LITERAL'),\n"
        "  'alfredrc': os.environ.get('ALFREDRC'),\n"
        "}))\n"
    )
    target.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "agent-launch"), "probe"],
        env=_clean_env(
            HOME=str(home),
            ALFRED_HOME=str(alfred),
            ALFREDRC=str(home / ".alfredrc"),
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(capture.read_text())
    assert data == {
        "workspace": f"{home}/work space",
        "operator": "Example Operator",
        "gh_org": "acme",
        "telemetry_token": telemetry_token,
        "quote_token": "can'quote",
        "home_literal": "abc$HOMEdef-${HOME}",
        "alfredrc": None,
    }
    assert not marker.exists()


def test_agent_launch_expands_double_quoted_home_path_values_from_runtime_env(tmp_path):
    home = tmp_path / "home"
    alfred = home / ".alfred"
    bin_dir = alfred / "bin"
    home.mkdir()
    bin_dir.mkdir(parents=True)
    capture = tmp_path / "capture.json"
    (alfred / ".env").write_text(
        "\n".join(
            [
                'WORKSPACE_ROOT="$HOME/code space"',
                "",
            ]
        )
    )
    target = bin_dir / "probe"
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"open({str(capture)!r}, 'w').write(json.dumps({{\n"
        "  'alfred_home': os.environ.get('ALFRED_HOME'),\n"
        "  'workspace': os.environ.get('WORKSPACE_ROOT'),\n"
        "}))\n"
    )
    target.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "agent-launch"), "probe"],
        env=_clean_env(HOME=str(home)),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(capture.read_text())
    assert data == {
        "alfred_home": str(alfred),
        "workspace": f"{home}/code space",
    }


def test_doctor_runs_configured_agent_through_agent_launch(tmp_path):
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    bin_dir = alfred / "bin"
    launchd_dir = alfred / "launchd"
    home.mkdir()
    bin_dir.mkdir(parents=True)
    launchd_dir.mkdir(parents=True)
    capture = tmp_path / "doctor-env.json"
    (alfred / ".env").write_text("CUSTOM_FROM_ENV=loaded\n")
    (launchd_dir / "agents.conf").write_text(
        "alfred.helper\tprobe.py\tinterval:60\tno\talfred.helper\tHelper\n"
    )
    probe = bin_dir / "probe.py"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"open({str(capture)!r}, 'w').write(json.dumps({{\n"
        "  'doctor': os.environ.get('ALFRED_DOCTOR'),\n"
        "  'codename': os.environ.get('AGENT_CODENAME'),\n"
        "  'label': os.environ.get('LAUNCHD_LABEL'),\n"
        "  'custom': os.environ.get('CUSTOM_FROM_ENV'),\n"
        "}))\n"
        "print('[PROBE-DOCTOR-OK]')\n"
    )
    probe.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "doctor.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "probe" in res.stdout
    data = json.loads(capture.read_text())
    assert data == {
        "doctor": "1",
        "codename": "helper",
        "label": "alfred.helper",
        "custom": "loaded",
    }


def test_doctor_accepts_no_url_telemetry_sentinel(tmp_path):
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    bin_dir = alfred / "bin"
    launchd_dir = alfred / "launchd"
    home.mkdir()
    bin_dir.mkdir(parents=True)
    launchd_dir.mkdir(parents=True)
    (launchd_dir / "agents.conf").write_text(
        "alfred.proof-telemetry\tproof-telemetry.py\tinterval:3600\tno\t"
        "alfred.proof-telemetry\tAnonymous usage totals\n"
    )
    (bin_dir / "agent-launch").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\npython3 "$ALFRED_HOME/bin/$1"\n'
    )
    (bin_dir / "agent-launch").chmod(0o755)
    probe = bin_dir / "proof-telemetry.py"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "assert os.environ.get('ALFRED_DOCTOR') == '1'\n"
        "print('[PROOF-TELEMETRY-NO-URL] (doctor: enabled but no collector URL available)')\n"
    )
    probe.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "ALFRED_HOME": str(alfred),
        "WORKSPACE_ROOT": str(tmp_path / "code"),
    }
    env.pop("ALFRED_TELEMETRY_URL", None)
    env.pop("ALFRED_TELEMETRY_ENABLED", None)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "doctor.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "proof-telemetry" in res.stdout
    assert "no URL" in res.stdout
    assert "unexpected output" not in res.stdout


def test_doctor_includes_custom_agents_when_runtime_conf_is_empty(tmp_path):
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    workspace = tmp_path / "workspace"
    fakebin = tmp_path / "fakebin"
    (alfred / "launchd").mkdir(parents=True)
    (alfred / "state" / "custom-agents").mkdir(parents=True)
    workspace.mkdir()
    fakebin.mkdir()
    (alfred / "launchd" / "agents.conf").write_text("", encoding="utf-8")
    (alfred / "state" / "custom-agents" / "custom-agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers.",
                        "engine": "codex",
                        "schedule": "interval:1800",
                        "repos": [],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (fakebin / "codex").write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    (fakebin / "git").write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    (fakebin / "codex").chmod(0o755)
    (fakebin / "git").chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "doctor.sh"), "--dev"],
        env=_clean_env(
            HOME=str(home),
            ALFRED_HOME=str(alfred),
            WORKSPACE_ROOT=str(workspace),
            PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert "custom-agent" in res.stdout
    assert "doctor: 1 passed, 0 failed" in res.stdout


def test_deploy_removes_stale_managed_plists(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "launchd" / "render.sh", src / "launchd" / "render.sh")
    shutil.copy(REPO / "launchd" / "_template.plist", src / "launchd" / "_template.plist")
    shutil.copy(REPO / "bin" / "agent-launch", src / "bin" / "agent-launch")
    (src / "bin" / "probe.py").write_text("#!/usr/bin/env python3\nprint('[PROBE-OK]')\n")
    (src / "bin" / "probe.py").chmod(0o755)
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    # deploy.sh v0.4.x asserts these five subpackages land after copy, so the
    # fixture must ship them. Empty __init__.py satisfies the existence check.
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")
    (src / "launchd" / "agents.conf").write_text(
        "alfred.new\tprobe.py\tinterval:60\tno\talfred.new\tNew helper\n"
    )
    managed = alfred / "launchd" / "managed-labels.txt"
    managed.parent.mkdir(parents=True)
    managed.write_text("alfred.old\n")
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "alfred.old.plist").write_text("<plist><dict></dict></plist>\n")
    (launch_agents / "alfred.personal.plist").write_text("<plist><dict></dict></plist>\n")
    (launch_agents / "unrelated.keep.plist").write_text("<plist><dict></dict></plist>\n")
    launchctl_log = tmp_path / "launchctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Darwin\n")
    (fakebin / "launchctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(launchctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "launchctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (launch_agents / "alfred.old.plist").exists()
    assert (launch_agents / "alfred.personal.plist").exists()
    assert (launch_agents / "unrelated.keep.plist").exists()
    assert (launch_agents / "alfred.new.plist").exists()
    assert managed.read_text().strip() == "alfred.new"
    log = launchctl_log.read_text()
    assert "alfred.old.plist" in log
    assert "alfred.personal.plist" not in log
    assert "alfred.new.plist" in log


def test_desktop_deploy_prefers_seeded_runtime_roster(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "launchd" / "render.sh", src / "launchd" / "render.sh")
    shutil.copy(REPO / "launchd" / "_template.plist", src / "launchd" / "_template.plist")
    shutil.copy(REPO / "bin" / "agent-launch", src / "bin" / "agent-launch")
    (src / "bin" / "probe.py").write_text("#!/usr/bin/env python3\nprint('[PROBE-OK]')\n")
    (src / "bin" / "probe.py").chmod(0o755)
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")
    (src / "launchd" / "agents.conf").write_text(
        "alfred.source\tprobe.py\tinterval:60\tno\talfred.source\tStale source\n"
    )
    runtime_conf = alfred / "launchd" / "agents.conf"
    runtime_conf.parent.mkdir(parents=True)
    runtime_conf.write_text(
        "alfred.seeded\tprobe.py\tinterval:60\tno\talfred.seeded\tSeeded runtime\n"
    )

    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    launchctl_log = tmp_path / "launchctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Darwin\n")
    (fakebin / "launchctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(launchctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "launchctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "ALFRED_DESKTOP_INSTALL": "1",
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert "using seeded runtime launchd/agents.conf" in res.stdout
    assert runtime_conf.read_text().startswith("alfred.seeded\t")
    assert (launch_agents / "alfred.seeded.plist").exists()
    assert not (launch_agents / "alfred.source.plist").exists()


def test_deploy_launchd_reaps_previous_custom_only_when_last_agent_removed(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "launchd" / "render.sh", src / "launchd" / "render.sh")
    shutil.copy(REPO / "launchd" / "_template.plist", src / "launchd" / "_template.plist")
    shutil.copy(REPO / "bin" / "agent-launch", src / "bin" / "agent-launch")
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")

    managed = alfred / "launchd" / "managed-labels.txt"
    managed.parent.mkdir(parents=True)
    managed.write_text("alfred.release-captain\n", encoding="utf-8")
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "alfred.release-captain.plist").write_text(
        "<plist><dict></dict></plist>\n",
        encoding="utf-8",
    )
    (launch_agents / "unrelated.keep.plist").write_text(
        "<plist><dict></dict></plist>\n",
        encoding="utf-8",
    )
    launchctl_log = tmp_path / "launchctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Darwin\n")
    (fakebin / "launchctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(launchctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "launchctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert "framework-only deploy complete" not in res.stdout
    assert not (launch_agents / "alfred.release-captain.plist").exists()
    assert (launch_agents / "unrelated.keep.plist").exists()
    assert managed.read_text(encoding="utf-8") == ""
    log = launchctl_log.read_text(encoding="utf-8")
    assert "alfred.release-captain.plist" in log
    assert "unrelated.keep.plist" not in log


def test_deploy_linux_stays_framework_only_without_conf_or_custom_agents(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    systemd_user = tmp_path / "systemd-user"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    systemd_user.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    (src / "systemd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "systemd" / "render.sh", src / "systemd" / "render.sh")
    shutil.copy(REPO / "systemd" / "_template.service", src / "systemd" / "_template.service")
    shutil.copy(REPO / "systemd" / "_template.timer", src / "systemd" / "_template.timer")
    (src / "bin" / "probe.py").write_text("#!/usr/bin/env python3\nprint('[PROBE-OK]')\n")
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")

    (systemd_user / "alfred.old.timer").write_text("[Timer]\n", encoding="utf-8")
    (systemd_user / "alfred.old.service").write_text("[Service]\n", encoding="utf-8")
    (systemd_user / "backup.timer").write_text("[Timer]\n", encoding="utf-8")
    (systemd_user / "backup.service").write_text("[Service]\n", encoding="utf-8")

    systemctl_log = tmp_path / "systemctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Linux\n")
    (fakebin / "systemctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(systemctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "systemctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "ALFRED_SYSTEMD_USER_DIR": str(systemd_user),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert "framework-only deploy complete" in res.stdout
    assert (systemd_user / "alfred.old.timer").exists()
    assert (systemd_user / "alfred.old.service").exists()
    assert (systemd_user / "backup.timer").exists()
    assert (systemd_user / "backup.service").exists()
    assert not (alfred / "systemd" / "managed-labels.txt").exists()
    log = systemctl_log.read_text(encoding="utf-8")
    assert "alfred.old.timer" not in log
    assert "backup.timer" not in log


def test_deploy_linux_fails_when_custom_agent_manifest_is_malformed(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    systemd_user = tmp_path / "systemd-user"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    systemd_user.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    (src / "systemd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "systemd" / "render.sh", src / "systemd" / "render.sh")
    shutil.copy(REPO / "systemd" / "_template.service", src / "systemd" / "_template.service")
    shutil.copy(REPO / "systemd" / "_template.timer", src / "systemd" / "_template.timer")
    shutil.copy(REPO / "lib" / "custom_agents.py", src / "lib" / "custom_agents.py")
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")

    store = alfred / "state" / "custom-agents"
    store.mkdir(parents=True)
    (store / "custom-agents.json").write_text('{"version": 1, "agents": [', encoding="utf-8")

    systemctl_log = tmp_path / "systemctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Linux\n")
    (fakebin / "systemctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(systemctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "systemctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "ALFRED_SYSTEMD_USER_DIR": str(systemd_user),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 2
    assert "custom agent manifest invalid" in res.stderr
    assert "not valid JSON" in res.stderr
    assert "framework-only deploy complete" not in res.stdout
    if systemctl_log.exists():
        log = systemctl_log.read_text(encoding="utf-8")
        assert "alfred.release-captain" not in log


def test_deploy_linux_reaps_previous_custom_only_when_last_agent_removed(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    systemd_user = tmp_path / "systemd-user"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    systemd_user.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    (src / "systemd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "systemd" / "render.sh", src / "systemd" / "render.sh")
    shutil.copy(REPO / "systemd" / "_template.service", src / "systemd" / "_template.service")
    shutil.copy(REPO / "systemd" / "_template.timer", src / "systemd" / "_template.timer")
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")

    managed = alfred / "systemd" / "managed-labels.txt"
    managed.parent.mkdir(parents=True)
    managed.write_text("alfred.release-captain\n", encoding="utf-8")
    (systemd_user / "alfred.release-captain.timer").write_text("[Timer]\n", encoding="utf-8")
    (systemd_user / "alfred.release-captain.service").write_text(
        "[Service]\n",
        encoding="utf-8",
    )
    (systemd_user / "backup.timer").write_text("[Timer]\n", encoding="utf-8")
    (systemd_user / "backup.service").write_text("[Service]\n", encoding="utf-8")

    systemctl_log = tmp_path / "systemctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Linux\n")
    (fakebin / "systemctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(systemctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "systemctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "ALFRED_SYSTEMD_USER_DIR": str(systemd_user),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert "framework-only deploy complete" not in res.stdout
    assert not (systemd_user / "alfred.release-captain.timer").exists()
    assert not (systemd_user / "alfred.release-captain.service").exists()
    assert (systemd_user / "backup.timer").exists()
    assert (systemd_user / "backup.service").exists()
    assert managed.read_text(encoding="utf-8") == ""
    log = systemctl_log.read_text(encoding="utf-8")
    assert "alfred.release-captain.timer" in log
    assert "backup.timer" not in log


def test_deploy_linux_custom_only_reaps_only_previous_managed_units(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    systemd_user = tmp_path / "systemd-user"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    systemd_user.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    (src / "systemd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "systemd" / "render.sh", src / "systemd" / "render.sh")
    shutil.copy(REPO / "systemd" / "_template.service", src / "systemd" / "_template.service")
    shutil.copy(REPO / "systemd" / "_template.timer", src / "systemd" / "_template.timer")
    shutil.copy(REPO / "lib" / "custom_agents.py", src / "lib" / "custom_agents.py")
    shutil.copy(REPO / "bin" / "agent-launch", src / "bin" / "agent-launch")
    shutil.copy(REPO / "bin" / "custom-agent.py", src / "bin" / "custom-agent.py")
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")

    store = alfred / "state" / "custom-agents"
    store.mkdir(parents=True)
    (store / "custom-agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers.",
                        "engine": "codex",
                        "schedule": "interval:1800",
                        "repos": [],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    managed = alfred / "systemd" / "managed-labels.txt"
    managed.parent.mkdir(parents=True)
    managed.write_text("alfred.old\n", encoding="utf-8")
    (systemd_user / "alfred.old.timer").write_text("[Timer]\n", encoding="utf-8")
    (systemd_user / "alfred.old.service").write_text("[Service]\n", encoding="utf-8")
    (systemd_user / "backup.timer").write_text("[Timer]\n", encoding="utf-8")
    (systemd_user / "backup.service").write_text("[Service]\n", encoding="utf-8")

    systemctl_log = tmp_path / "systemctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Linux\n")
    (fakebin / "systemctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(systemctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "systemctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "ALFRED_SYSTEMD_USER_DIR": str(systemd_user),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert not (systemd_user / "alfred.old.timer").exists()
    assert not (systemd_user / "alfred.old.service").exists()
    assert (systemd_user / "backup.timer").exists()
    assert (systemd_user / "backup.service").exists()
    assert (systemd_user / "alfred.release-captain.timer").exists()
    assert (systemd_user / "alfred.release-captain.service").exists()
    assert managed.read_text(encoding="utf-8").strip() == "alfred.release-captain"
    log = systemctl_log.read_text(encoding="utf-8")
    assert "alfred.old.timer" in log
    assert "alfred.release-captain.timer" in log
    assert "backup.timer" not in log


def test_deploy_linux_migrates_existing_alfred_units_without_ledger(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    systemd_user = tmp_path / "systemd-user"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    systemd_user.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    (src / "systemd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "systemd" / "render.sh", src / "systemd" / "render.sh")
    shutil.copy(REPO / "systemd" / "_template.service", src / "systemd" / "_template.service")
    shutil.copy(REPO / "systemd" / "_template.timer", src / "systemd" / "_template.timer")
    shutil.copy(REPO / "lib" / "custom_agents.py", src / "lib" / "custom_agents.py")
    shutil.copy(REPO / "bin" / "agent-launch", src / "bin" / "agent-launch")
    shutil.copy(REPO / "bin" / "custom-agent.py", src / "bin" / "custom-agent.py")
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")

    store = alfred / "state" / "custom-agents"
    store.mkdir(parents=True)
    (store / "custom-agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers.",
                        "engine": "codex",
                        "schedule": "interval:1800",
                        "repos": [],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (systemd_user / "alfred.old.timer").write_text("[Timer]\n", encoding="utf-8")
    (systemd_user / "alfred.old.service").write_text(
        "[Unit]\nDescription=alfred-os alfred.old\n[Service]\n",
        encoding="utf-8",
    )
    (systemd_user / "backup.timer").write_text("[Timer]\n", encoding="utf-8")
    (systemd_user / "backup.service").write_text(
        "[Unit]\nDescription=nightly backup\n[Service]\nEnvironment=ALFRED_HOME=/tmp/alfred\n",
        encoding="utf-8",
    )

    systemctl_log = tmp_path / "systemctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Linux\n")
    (fakebin / "systemctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(systemctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "systemctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "ALFRED_SYSTEMD_USER_DIR": str(systemd_user),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert "framework-only deploy complete" not in res.stdout
    assert not (systemd_user / "alfred.old.timer").exists()
    assert not (systemd_user / "alfred.old.service").exists()
    assert (systemd_user / "backup.timer").exists()
    assert (systemd_user / "backup.service").exists()
    assert (systemd_user / "alfred.release-captain.timer").exists()
    assert (systemd_user / "alfred.release-captain.service").exists()
    managed = alfred / "systemd" / "managed-labels.txt"
    assert managed.read_text(encoding="utf-8").strip() == "alfred.release-captain"
    log = systemctl_log.read_text(encoding="utf-8")
    assert "alfred.old.timer" in log
    assert "alfred.release-captain.timer" in log
    assert "backup.timer" not in log


def test_deploy_defers_reload_for_running_jobs(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (src / "launchd").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    shutil.copy(REPO / "launchd" / "render.sh", src / "launchd" / "render.sh")
    shutil.copy(REPO / "launchd" / "_template.plist", src / "launchd" / "_template.plist")
    shutil.copy(REPO / "bin" / "agent-launch", src / "bin" / "agent-launch")
    (src / "bin" / "probe.py").write_text("#!/usr/bin/env python3\nprint('[PROBE-OK]')\n")
    (src / "bin" / "probe.py").chmod(0o755)
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    # deploy.sh v0.4.x asserts these five subpackages land after copy, so the
    # fixture must ship them. Empty __init__.py satisfies the existence check.
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")
    (src / "launchd" / "agents.conf").write_text(
        "alfred.new\tprobe.py\tinterval:60\tno\talfred.new\tNew helper\n"
    )
    launchctl_log = tmp_path / "launchctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Darwin\n")
    (fakebin / "launchctl").write_text(
        "#!/usr/bin/env sh\n"
        f"printf '%s\\n' \"$*\" >> {str(launchctl_log)!r}\n"
        'case "$1" in\n'
        "  list) printf '123\\t0\\talfred.new\\n' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "launchctl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(tmp_path / "code"),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert "alfred.new running pid 123; installed but reload deferred" in res.stdout
    alfred_new_calls = [
        line for line in launchctl_log.read_text().splitlines() if "alfred.new.plist" in line
    ]
    assert alfred_new_calls == []


def test_deploy_ignores_explicit_alfredrc_and_uses_alfred_home(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    stale_alfred = tmp_path / "stale-alfred"
    alfred = tmp_path / "alfred"
    workspace = tmp_path / "workspace"
    fakebin = tmp_path / "fakebin"
    legacy_rc = tmp_path / "legacy.alfredrc"
    src.mkdir()
    home.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    (src / "bin" / "probe.py").write_text("#!/usr/bin/env python3\nprint('[PROBE-OK]')\n")
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")
    legacy_rc.write_text(
        f"ALFRED_HOME={stale_alfred}\nWORKSPACE_ROOT={tmp_path / 'stale-workspace'}\n",
        encoding="utf-8",
    )
    launchctl_log = tmp_path / "launchctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Darwin\n")
    (fakebin / "launchctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(launchctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "launchctl").chmod(0o755)
    env = os.environ.copy()
    env.pop("ALFRED_HOME", None)
    env.pop("WORKSPACE_ROOT", None)
    env.update(
        {
            "HOME": str(home),
            "ALFREDRC": str(legacy_rc),
            "ALFRED_HOME": str(alfred),
            "WORKSPACE_ROOT": str(workspace),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        }
    )

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert (alfred / "launchd" / "source-repo.txt").read_text().strip() == str(src)
    assert not stale_alfred.exists()
    assert not (alfred / "launchd" / "alfredrc.path").exists()


def test_deploy_removes_persisted_alfredrc_pointer_and_uses_runtime_env(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    alfred = home / ".alfred"
    workspace = tmp_path / "workspace"
    fakebin = tmp_path / "fakebin"
    src.mkdir()
    home.mkdir()
    fakebin.mkdir()
    (src / "bin").mkdir()
    (src / "lib").mkdir()
    (home / ".alfred" / "launchd").mkdir(parents=True)
    shutil.copy(REPO / "deploy.sh", src / "deploy.sh")
    (src / "bin" / "probe.py").write_text("#!/usr/bin/env python3\nprint('[PROBE-OK]')\n")
    (src / "lib" / "dummy.py").write_text("# dummy\n")
    for pkg in ("agent_runner", "connectors", "fleet_brain", "memory", "server"):
        (src / "lib" / pkg).mkdir()
        (src / "lib" / pkg / "__init__.py").write_text("")
    (home / ".alfred" / "launchd" / "alfredrc.path").write_text(
        "%h/custom.alfredrc\n",
        encoding="utf-8",
    )
    (home / ".alfred" / ".env").write_text(
        f"WORKSPACE_ROOT={workspace}\n",
        encoding="utf-8",
    )
    launchctl_log = tmp_path / "launchctl.log"
    (fakebin / "uname").write_text("#!/usr/bin/env sh\necho Darwin\n")
    (fakebin / "launchctl").write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> {str(launchctl_log)!r}\nexit 0\n"
    )
    (fakebin / "uname").chmod(0o755)
    (fakebin / "launchctl").chmod(0o755)
    env = os.environ.copy()
    env.pop("ALFRED_HOME", None)
    env.pop("ALFREDRC", None)
    env.pop("WORKSPACE_ROOT", None)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        }
    )

    res = subprocess.run(
        ["bash", str(src / "deploy.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    assert (alfred / "launchd" / "source-repo.txt").read_text().strip() == str(src)
    assert not (alfred / "launchd" / "alfredrc.path").exists()


def test_agent_launch_prefers_alfred_home_venv_python(tmp_path):
    """When ${ALFRED_HOME}/venv/bin/python exists, agent-launch must
    invoke the target through it instead of the shebang. This is what
    makes slack-sdk + boto3 (v0.4.0 base deps) importable from launchd
    / systemd-spawned agents without per-host pip-into-system tricks."""
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    venv_bin = alfred / "venv" / "bin"
    home.mkdir()
    (alfred / "bin").mkdir(parents=True)
    venv_bin.mkdir(parents=True)

    # Fake "venv python" that writes a marker so we can prove agent-launch
    # picked it. Has to be a real executable; tests run on macOS + Linux.
    fake_python = venv_bin / "python"
    fake_python.write_text(
        '#!/usr/bin/env bash\necho VENV-PYTHON-USED\nexec /usr/bin/env python3 "$@"\n'
    )
    fake_python.chmod(0o755)

    target = alfred / "bin" / "probe.py"
    target.write_text("#!/usr/bin/env python3\nprint('PROBE-OK')\n")
    target.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "agent-launch"), "probe.py"],
        env={**os.environ, "HOME": str(home), "ALFRED_HOME": str(alfred)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "VENV-PYTHON-USED" in res.stdout, (
        f"agent-launch did not route through $ALFRED_HOME/venv/bin/python; "
        f"got stdout: {res.stdout!r}"
    )
    assert "PROBE-OK" in res.stdout


def test_agent_launch_falls_back_to_shebang_when_venv_absent(tmp_path):
    """When no venv exists, agent-launch must keep the historical
    shebang-resolution path so source-checkout / dev installs are
    unaffected by the v0.4.x venv addition."""
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    home.mkdir()
    (alfred / "bin").mkdir(parents=True)
    # No venv directory at all.

    target = alfred / "bin" / "probe.py"
    target.write_text(
        "#!/usr/bin/env python3\nimport sys; print(f'shebang-python={sys.executable}')\n"
    )
    target.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "agent-launch"), "probe.py"],
        env={**os.environ, "HOME": str(home), "ALFRED_HOME": str(alfred)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "shebang-python=" in res.stdout


def test_agent_launch_honours_explicit_alfred_python_override(tmp_path):
    """``ALFRED_PYTHON`` env override takes precedence over the
    auto-detected venv so operators with a different interpreter
    (system python with a different libs set, alternate venv) can opt
    in without uninstalling the venv."""
    home = tmp_path / "home"
    alfred = tmp_path / "alfred"
    venv_bin = alfred / "venv" / "bin"
    home.mkdir()
    (alfred / "bin").mkdir(parents=True)
    venv_bin.mkdir(parents=True)

    # Stub venv python that would be picked by the default path.
    (venv_bin / "python").write_text("#!/usr/bin/env bash\necho VENV\nexit 0\n")
    (venv_bin / "python").chmod(0o755)

    # Operator-supplied override. Should take precedence.
    override = tmp_path / "operator-python"
    override.write_text("#!/usr/bin/env bash\necho OPERATOR-OVERRIDE\nexit 0\n")
    override.chmod(0o755)

    target = alfred / "bin" / "probe.py"
    target.write_text("#!/usr/bin/env python3\nprint('PROBE-OK')\n")
    target.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "agent-launch"), "probe.py"],
        env={
            **os.environ,
            "HOME": str(home),
            "ALFRED_HOME": str(alfred),
            "ALFRED_PYTHON": str(override),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "OPERATOR-OVERRIDE" in res.stdout
    assert "VENV" not in res.stdout
