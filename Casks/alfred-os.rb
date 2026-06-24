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
