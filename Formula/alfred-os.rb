class AlfredOs < Formula
  desc "Launchd-managed Claude Code agent fleet for solo founders"
  homepage "https://luminik-io.github.io/alfred-os"
  # HEAD-only until the first public tag is cut. The release workflow prints
  # the source tarball sha256; add url/sha256 here before publishing a tap.
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

    # Operator-facing helpers go onto PATH.
    bin.install_symlink libexec/"bin/alfred" => "alfred"
    bin.install_symlink libexec/"bin/alfred-init.py" => "alfred-init"
    bin.install_symlink libexec/"bin/doctor.sh" => "alfred-doctor"
    bin.install_symlink libexec/"bin/hermes-claude" => "alfred-hermes-claude"
    bin.install_symlink libexec/"install.sh" => "alfred-install"
    bin.install_symlink libexec/"deploy.sh" => "alfred-deploy"

    # The example label-state CLI ships as a runnable binary too.
    bin.install_symlink libexec/"examples/bin/label_state.py" => "alfred-label-state"
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

      This formula is HEAD-only until the first public release tarball exists.

      Next steps:
        1. alfred-install
        2. exec $SHELL                     # pick up ~/.alfredrc
        3. gh auth login                   # GitHub
        4. claude                          # Claude Code first-run auth
        5. alfred-deploy && alfred-doctor

      Docs:
        https://luminik-io.github.io/alfred-os
        #{libexec}/INSTALL.md
        #{libexec}/BOOTSTRAP.md
    EOS
  end

  test do
    # Smoke: lib/ is intact and doctor.sh at least executes. It exits clean
    # against an empty fleet.
    assert_predicate libexec/"lib/agent_runner.py", :exist?
    assert_match(/passed/, shell_output("bash #{libexec}/bin/doctor.sh"))
  end
end
