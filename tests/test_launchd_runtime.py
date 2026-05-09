"""Smoke tests for the launchd runtime wrapper and deploy/doctor parity."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_agent_launch_loads_alfredrc_without_shell_eval(tmp_path):
    home = tmp_path / "home"
    hermes = tmp_path / "hermes"
    bin_dir = hermes / "bin"
    home.mkdir()
    bin_dir.mkdir(parents=True)
    capture = tmp_path / "capture.json"
    (home / ".alfredrc").write_text(
        "\n".join(
            [
                "WORKSPACE_ROOT=$HOME/work space",
                "OPERATOR_NAME=Prasad Subrahmanya",
                "export GH_ORG=acme",
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
        "}))\n"
    )
    target.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "agent-launch"), "probe"],
        env={**os.environ, "HOME": str(home), "HERMES_HOME": str(hermes)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(capture.read_text())
    assert data == {
        "workspace": f"{home}/work space",
        "operator": "Prasad Subrahmanya",
        "gh_org": "acme",
    }


def test_doctor_runs_configured_agent_through_agent_launch(tmp_path):
    home = tmp_path / "home"
    hermes = tmp_path / "hermes"
    bin_dir = hermes / "bin"
    launchd_dir = hermes / "launchd"
    home.mkdir()
    bin_dir.mkdir(parents=True)
    launchd_dir.mkdir(parents=True)
    capture = tmp_path / "doctor-env.json"
    (home / ".alfredrc").write_text("CUSTOM_FROM_RC=loaded\n")
    (launchd_dir / "agents.conf").write_text(
        "alfred.helper\tprobe.py\tinterval:60\tno\talfred.helper\tHelper\n"
    )
    probe = bin_dir / "probe.py"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"open({str(capture)!r}, 'w').write(json.dumps({{\n"
        "  'doctor': os.environ.get('HERMES_DOCTOR'),\n"
        "  'codename': os.environ.get('AGENT_CODENAME'),\n"
        "  'label': os.environ.get('LAUNCHD_LABEL'),\n"
        "  'custom': os.environ.get('CUSTOM_FROM_RC'),\n"
        "}))\n"
        "print('[PROBE-DOCTOR-OK]')\n"
    )
    probe.chmod(0o755)

    res = subprocess.run(
        ["bash", str(REPO / "bin" / "doctor.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "HERMES_HOME": str(hermes),
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


def test_deploy_removes_stale_managed_plists(tmp_path):
    src = tmp_path / "repo"
    home = tmp_path / "home"
    hermes = tmp_path / "hermes"
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
    (src / "launchd" / "agents.conf").write_text(
        "alfred.new\tprobe.py\tinterval:60\tno\talfred.new\tNew helper\n"
    )
    managed = hermes / "launchd" / "managed-labels.txt"
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
            "HERMES_HOME": str(hermes),
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
