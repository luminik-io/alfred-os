# frozen_string_literal: true

# Homebrew cask for Alfred Desktop, the signed native client.
#
# This is the recommended GUI install path. The cask depends on the
# `alfred-os` formula and the app also bundles core resources so Setup can
# install or repair the local runtime from inside Alfred.
#
#   brew install alfred-os            # CLI (formula)
#   brew install --cask alfred-os     # desktop app (this cask)
#
# Pinned to the published v0.5.3 desktop app. The `sha256` below is the
# checksum of the `Alfred.dmg` asset on the v0.5.3 release, so every install is
# verified against a known build. To refresh for a future release, bump
# `version` and recompute the checksum against the published asset:
#
#   curl -fL -o Alfred.dmg \
#     https://github.com/luminik-io/alfred-os/releases/download/vX.Y.Z/Alfred.dmg
#   shasum -a 256 Alfred.dmg
#
# Then verify: `brew audit --cask --new Casks/alfred-os.rb` and
# `brew install --cask ./Casks/alfred-os.rb`.
cask "alfred-os" do
  version "0.5.3"
  sha256 "2b3009c14665b81fd224362e0630cef3874056b21696a877432c3130b1a32ada"

  url "https://github.com/luminik-io/alfred-os/releases/download/v#{version}/Alfred.dmg",
      verified: "github.com/luminik-io/alfred-os/"
  name "Alfred Desktop"
  desc "Native desktop client for the Alfred local coding-agent fleet"
  homepage "https://alfred.luminik.io/"

  # The app can install/repair bundled core resources, and the formula gives
  # Homebrew users the CLI wrappers on PATH immediately.
  depends_on formula: "alfred-os"
  depends_on macos: :big_sur

  app "Alfred.app"

  postflight do
    ohai "Alfred Desktop installed."
    puts <<~EOS
      Open the app and follow Setup:
        open -a Alfred

      Setup can install or repair Alfred core, deploy the local CLI/agents,
      start alfred serve, and guide GitHub, engine, repo, roster, Slack, and
      doctor checks.

      Headless CLI path remains available:
        alfred-install
        alfred serve --port 7010 --no-browser

      See https://alfred.luminik.io/concepts/desktop-client/.
    EOS
  end

  zap trash: [
    "~/Library/Application Support/Alfred",
    "~/Library/Caches/io.luminik.alfred",
    "~/Library/Preferences/io.luminik.alfred.plist",
    "~/Library/Saved Application State/io.luminik.alfred.savedState",
  ]
end
