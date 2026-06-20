# frozen_string_literal: true

# Homebrew formula for the Alfred release package.
class AlfredOs < Formula
  desc "Local coding agents for Claude Code and Codex"
  homepage "https://alfred.luminik.io"
  url "https://github.com/luminik-io/alfred-os/archive/refs/tags/v0.5.1.tar.gz"
  sha256 "d891d956e6d6ec6fdf8181a21416535feb9fd72496824b1c63d1c693b5b8371a"
  license "MIT"
  head "https://github.com/luminik-io/alfred-os.git", branch: "main"

  depends_on "awscli"
  depends_on "gh"
  depends_on "git"
  depends_on "jq"
  depends_on :macos # Homebrew formula path. Linux uses install.sh apt lane; see docs/LINUX.md.
  depends_on "node"
  depends_on "python@3.11"
  depends_on "uv"

  def install
    # Install the framework files into a libexec subdir so we don't pollute
    # bin/ with internal helpers. Then drop a small launcher into bin/.
    libexec.install Dir["*"]
    libexec.install ".alfredrc.example"

    # User-facing helpers go onto PATH.
    bin.install_symlink libexec/"bin/alfred" => "alfred"
    bin.install_symlink libexec/"bin/alfred-init.py" => "alfred-init"
    bin.install_symlink libexec/"examples/bin/label_state.py" => "alfred-label-state"

    # Shell scripts compute their repo root from dirname "$0". Use wrappers
    # instead of symlinks so $0 is the real libexec path after exec.
    {
      "alfred-doctor"  => libexec/"bin/doctor.sh",
      "alfred-install" => libexec/"install.sh",
      "alfred-deploy"  => libexec/"deploy.sh",
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
      Alfred installed to:
        #{libexec}

      Available commands:
        alfred                 # local CLI, including `alfred claude`
        alfred-init            # interactive fleet configuration wizard
        alfred-install         # one-time fresh-machine setup (brew + npm + dirs + rc)
        alfred-deploy          # sync lib/+bin/ into $ALFRED_HOME; renders scheduler units when agents.conf exists
        alfred-doctor          # preflight configured agents without running real work
        alfred-label-state     # CLI for the issue claim state machine

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
        https://alfred.luminik.io
        #{libexec}/INSTALL.md
        #{libexec}/BOOTSTRAP.md
    EOS
  end

  test do
    # Smoke: lib/ is intact and doctor.sh at least executes. It exits clean
    # against an empty fleet.
    assert_path_exists libexec/".alfredrc.example"
    assert_path_exists libexec/"lib/agent_runner/__init__.py"
    assert_match(/passed/, shell_output("bash #{libexec}/bin/doctor.sh"))
  end
end
