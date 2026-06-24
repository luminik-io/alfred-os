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
# SELF-UPDATING: this cask tracks the latest signed build via `sha256 :no_check`
# against the stable `/releases/latest/download/Alfred.dmg` asset. There is no
# per-release sha bump to do by hand.
#
# Why `:no_check` is the right call here (documented tradeoff): the desktop
# release workflow (.github/workflows/desktop-release.yml) builds, signs with a
# Developer ID Application cert, notarizes, and staples a fresh `Alfred.dmg` on
# every release, then attaches it under the stable `latest/download` name. The
# `.dmg` changes on every release, so a pinned `sha256`/`version` pair would go
# stale and break `brew install --cask` until someone re-pinned it. Pointing at
# `latest/download` with `:no_check` keeps the GUI install path working on every
# release with zero maintenance.
#
# Integrity is not lost: the `.dmg` is Developer-ID signed and Apple-notarized,
# so Gatekeeper verifies it on first launch, and the workflow also attaches a
# `checksums.txt` to each release for anyone who wants to verify by hand. If you
# ever prefer per-release pinning instead, replace the `:no_check` + `url` block
# with `version "X.Y.Z"`, the `sha256` from that release's `checksums.txt`, and
# a versioned `/releases/download/v#{version}/Alfred.dmg` URL.
cask "alfred-os" do
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
