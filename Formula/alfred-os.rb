class AlfredOs < Formula
  desc "Local agent OS for solo builders"
  homepage "https://luminik-io.github.io/alfred-os"
  url "https://github.com/luminik-io/alfred-os/archive/refs/tags/v0.2.1.tar.gz"
  sha256 "63e2c3d6a9bb49c66fb98b302d367c98325f9ba643f9fd1e0e21210ccdefd585"
  license "MIT"
  head "https://github.com/luminik-io/alfred-os.git", branch: "main"

  depends_on "python@3.11"
  depends_on "git"
  depends_on "gh"
  depends_on "jq"
  depends_on "awscli"
  depends_on "node"
  depends_on "uv"
  depends_on :macos # launchd-only for now; see docs/LINUX.md

  def install
    # Install the framework files into a libexec subdir so we don't pollute
    # bin/ with internal helpers. Then drop a small launcher into bin/.
    libexec.install Dir["*"]
    libexec.install ".alfredrc.example"

    # Operator-facing helpers go onto PATH.
    bin.install_symlink libexec/"bin/alfred" => "alfred"
    bin.install_symlink libexec/"bin/alfred-init.py" => "alfred-init"
    bin.install_symlink libexec/"examples/bin/label_state.py" => "alfred-label-state"

    # Shell scripts compute their repo root from dirname "$0". Use wrappers
    # instead of symlinks so $0 is the real libexec path after exec.
    {
      "alfred-doctor" => libexec/"bin/doctor.sh",
      "alfred-hermes-claude" => libexec/"bin/hermes-claude",
      "alfred-install" => libexec/"install.sh",
      "alfred-deploy" => libexec/"deploy.sh",
    }.each do |name, target|
      (bin/name).write <<~EOS
        #!/bin/bash
        exec "#{target}" "$@"
      EOS
      chmod 0755, bin/name
    end
  end

  def caveats
    <<~EOS
      Alfred-OS installed to:
        #{libexec}

      Available commands:
        alfred                 # minimal runner-gate CLI
        alfred-init            # interactive fleet configuration wizard
        alfred-install         # one-time fresh-machine setup (brew + npm + dirs + rc)
        alfred-deploy          # sync lib/+bin/ into $HERMES_HOME; renders plists when agents.conf exists
        alfred-doctor          # preflight configured agents under HERMES_DOCTOR=1
        alfred-hermes-claude   # swap between Claude Code accounts
        alfred-label-state     # operator CLI for the issue claim state machine

      This formula installs the latest tagged release by default.
      Use --HEAD if you want to track main.

      Next steps:
        1. alfred-install
        2. exec $SHELL                     # pick up ~/.alfredrc
        3. gh auth login                   # GitHub
        4. claude                          # Claude Code first-run auth
        5. alfred-init                     # configure agents, deploy, run doctor

      Framework-only smoke test:
        alfred-deploy && alfred-doctor

      Docs:
        https://luminik-io.github.io/alfred-os
        #{libexec}/INSTALL.md
        #{libexec}/BOOTSTRAP.md
    EOS
  end

  test do
    # Smoke: lib/ is intact and doctor.sh at least executes. It exits clean
    # against an empty fleet.
    assert_predicate libexec/".alfredrc.example", :exist?
    assert_predicate libexec/"lib/agent_runner.py", :exist?
    assert_match(/passed/, shell_output("bash #{libexec}/bin/doctor.sh"))
  end
end
