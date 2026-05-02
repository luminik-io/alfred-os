class Pennyworth < Formula
  desc "Cron-driven Claude Code agent fleet for solo founders"
  homepage "https://luminik-io.github.io/pennyworth"
  # NOTE: bump `version`, `url`, and `sha256` on every release. The release
  # workflow at .github/workflows/release.yml prints the tarball sha256 to the
  # job log when a tag is pushed; copy it here.
  version "0.1.0"
  url "https://github.com/luminik-io/pennyworth/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_ME_AFTER_FIRST_RELEASE"
  license "MIT"
  head "https://github.com/luminik-io/pennyworth.git", branch: "main"

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
    bin.install_symlink libexec/"bin/doctor.sh" => "pennyworth-doctor"
    bin.install_symlink libexec/"bin/hermes-claude" => "pennyworth-hermes-claude"
    bin.install_symlink libexec/"install.sh" => "pennyworth-install"
    bin.install_symlink libexec/"deploy.sh" => "pennyworth-deploy"

    # The example label-state CLI ships as a runnable binary too.
    bin.install_symlink libexec/"examples/bin/label_state.py" => "pennyworth-label-state"
  end

  def caveats
    <<~EOS
      Pennyworth installed to:
        #{libexec}

      Available commands:
        pennyworth-install         # one-time fresh-machine setup (brew + npm + dirs + rc)
        pennyworth-deploy          # sync lib/+bin/ into $HERMES_HOME, render plists, reload launchd
        pennyworth-doctor          # preflight every agent under HERMES_DOCTOR=1
        pennyworth-hermes-claude   # swap between Claude Code accounts
        pennyworth-label-state     # operator CLI for the issue claim state machine

      Next steps:
        1. pennyworth-install
        2. exec $SHELL                     # pick up ~/.pennyworthrc
        3. gh auth login                   # GitHub
        4. claude                          # Claude Code first-run auth
        5. pennyworth-deploy && pennyworth-doctor

      Docs:
        https://luminik-io.github.io/pennyworth
        #{libexec}/INSTALL.md
        #{libexec}/BOOTSTRAP.md
    EOS
  end

  test do
    # Smoke: VERSION matches the formula version, lib/ is intact, doctor.sh
    # at least executes (it'll exit clean against an empty fleet).
    assert_equal version.to_s, File.read("#{libexec}/VERSION").strip
    assert_predicate libexec/"lib/agent_runner.py", :exist?
    assert_match(/passed/, shell_output("bash #{libexec}/bin/doctor.sh"))
  end
end
