# frozen_string_literal: true

# Homebrew cask for Alfred Desktop, the signed native client.
#
# This is the GUI half of the dual install path. The CLI ships as the
# `alfred-os` formula (Formula/alfred-os.rb); this cask installs the signed,
# notarized macOS desktop app that drives the fleet over `alfred serve`.
#
#   brew install alfred-os            # CLI (formula)
#   brew install --cask alfred-os     # desktop app (this cask)
#
# OPERATOR-GATED: this cask tracks the latest signed build via `sha256 :no_check`
# until the operator pins a published checksum (see step 3). Signed macOS
# release assets are attached to the GitHub Release by the operator before
# publish (public releases start as draft releases; CI builds with --no-bundle
# and never signs). Finish this cask after the signed asset is published:
#
#   1. Publish the release so `Alfred.dmg` is attached to the `v0.5.3` tag.
#   2. Compute the real checksum against the published, signed asset:
#        curl -fL -o Alfred.dmg \
#          https://github.com/luminik-io/alfred-os/releases/download/v0.5.3/Alfred.dmg
#        shasum -a 256 Alfred.dmg
#   3. Pin the published build by replacing the `:no_check` line below with the
#      real values once the signed asset is attached:
#        version "0.5.3"
#        sha256 "<shasum-from-step-2>"
#        url "https://github.com/luminik-io/alfred-os/releases/download/v#{version}/Alfred.dmg"
#      Pinning restores integrity verification against a known checksum.
#   4. Verify: `brew audit --cask --new Casks/alfred-os.rb` and
#      `brew install --cask ./Casks/alfred-os.rb`.
#
# Until then this cask tracks the latest published signed build with
# `sha256 :no_check`. That keeps the GUI install path working and discoverable
# the moment the operator attaches a signed `Alfred.dmg` to a release, instead
# of hard-failing every `brew install --cask` on an all-zeros placeholder
# checksum (Homebrew verifies the declared sha256 before any DSL hook runs, so
# a placeholder cannot be guarded with `preflight`/`odie`).
cask "alfred-os" do
  # TODO(operator): pin `version` + `sha256` to the published v0.5.3 asset to
  # restore checksum verification. See the header for the exact command.
  sha256 :no_check

  url "https://github.com/luminik-io/alfred-os/releases/latest/download/Alfred.dmg",
      verified: "github.com/luminik-io/alfred-os/"
  name "Alfred Desktop"
  desc "Native desktop client for the Alfred local coding-agent fleet"
  homepage "https://alfred.luminik.io/"

  # The desktop app needs the CLI fleet to talk to over `alfred serve`.
  depends_on formula: "alfred-os"
  depends_on macos: :big_sur

  app "Alfred.app"

  postflight do
    ohai "Alfred Desktop installed."
    puts <<~EOS
      Start the local runtime, then open the app:
        alfred serve --port 7010 --no-browser
        open -a Alfred

      Or let the in-app Setup wizard start the runtime for you on first run.
      The desktop app is a control surface only; it does not run agents by
      itself. See https://alfred.luminik.io/concepts/desktop-client/.
    EOS
  end

  zap trash: [
    "~/Library/Application Support/Alfred",
    "~/Library/Caches/io.luminik.alfred",
    "~/Library/Preferences/io.luminik.alfred.plist",
    "~/Library/Saved Application State/io.luminik.alfred.savedState",
  ]
end
