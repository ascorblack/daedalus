#!/bin/sh
# Installs the Daedalus desktop launcher into a folder of its own:
#
#   curl -fsSL https://raw.githubusercontent.com/ascorblack/daedalus/main/desktop/install.sh | sh
#
# It takes the newest desktop-v* release, downloads the archive for this machine, checks it against
# the release's SHA256SUMS, and unpacks it into ./Daedalus (or $DAEDALUS_DIR). Everything the
# installation owns - the checkouts, the keys, the database - is then made by the launcher inside
# that same folder, so removing the folder removes the installation.
#
# Nothing here clears a quarantine attribute, and nothing needs to: a file fetched with curl is not
# quarantined in the first place, so the app opens on macOS whether or not the release was signed
# by a Developer ID certificate.
set -eu

repo="${DAEDALUS_REPO:-ascorblack/daedalus}"
dir="${DAEDALUS_DIR:-./Daedalus}"
api="https://api.github.com/repos/$repo"

say() { printf '%s\n' "$*"; }
fail() { printf '%s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is needed to download the release."

case "$(uname -s)" in
  Darwin) platform="macos" ;;
  Linux) platform="linux" ;;
  *) fail "This installer covers macOS and Linux. On Windows, download the zip from https://github.com/$repo/releases and unpack it with Expand-Archive." ;;
esac

case "$(uname -m)" in
  x86_64 | amd64) arch="amd64" ;;
  arm64 | aarch64) arch="arm64" ;;
  *) fail "There is no build for $(uname -m)." ;;
esac

# The macOS release is one universal bundle for both kinds of Mac; Linux is one tarball per
# architecture, holding the executable with its mode, because a tarball keeps a mode and a zip of a
# bare file effectively does not.
if [ "$platform" = "macos" ]; then
  asset="Daedalus-macOS.zip"
else
  asset="daedalus-desktop-linux-$arch.tar.gz"
fi

# The newest release whose tag names the launcher: the repository releases other things too, so
# "latest" on its own is not necessarily this.
say "Looking for the newest desktop release of ${repo}..."
tag="$(curl -fsSL "$api/releases?per_page=30" |
  grep -o '"tag_name"[ ]*:[ ]*"desktop-v[^"]*"' |
  head -n 1 |
  sed -e 's/.*"desktop-v/desktop-v/' -e 's/"$//')"
[ -n "$tag" ] || fail "No desktop-v* release found in $repo."

base="https://github.com/$repo/releases/download/$tag"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM

say "Downloading $asset from ${tag}..."
curl -fL --progress-bar -o "$work/$asset" "$base/$asset" ||
  fail "$tag has no $asset. See https://github.com/$repo/releases/tag/$tag."
curl -fsSL -o "$work/SHA256SUMS" "$base/SHA256SUMS" ||
  fail "$tag publishes no SHA256SUMS; refusing to install something unverifiable."

# The checksum is the whole reason a release publishes SHA256SUMS: a download that was truncated or
# tampered with in transit is caught here rather than by a launcher that fails to run.
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$work/$asset" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$work/$asset" | cut -d' ' -f1)"
else
  fail "Neither sha256sum nor shasum is available to check the download."
fi
expected="$(grep "  $asset\$" "$work/SHA256SUMS" | cut -d' ' -f1)"
[ -n "$expected" ] || fail "SHA256SUMS does not mention $asset."
[ "$actual" = "$expected" ] || fail "The download does not match its checksum; not installing it."
say "Checksum matches."

mkdir -p "$dir"
target="$(cd "$dir" && pwd)"

# The same folder again is an update: the launcher is replaced and everything it made - the
# checkouts, the keys, the models, the database under data/ - stays where it is. A running launcher
# is asked to quit first, because a binary swapped under a running process is the old one until it
# exits, and the next launch would only bring that old window to the front.
if [ -d "$target/data" ]; then
  say "Updating the installation in ${target} (your data stays)."
  if pkill -x daedalus-desktop 2>/dev/null; then
    say "Closing the running launcher..."
    n=0
    while pgrep -x daedalus-desktop >/dev/null 2>&1 && [ "$n" -lt 30 ]; do sleep 1; n=$((n + 1)); done
  fi
fi

if [ "$platform" = "macos" ]; then
  rm -rf "$target/Daedalus.app"
  # ditto, not unzip: the bundle carries symlinks and the signature's own extended attributes, and
  # unzip drops both - which leaves an app macOS refuses as damaged.
  ditto -x -k "$work/$asset" "$target"
  say ""
  say "Installed $tag into $target."
  say "Open it:  open '$target/Daedalus.app'"
  say "Or double-click Daedalus in that folder. Docker Desktop must be installed and running."
else
  tar -xzf "$work/$asset" -C "$target"
  chmod +x "$target/daedalus-desktop"
  say ""
  say "Installed $tag into $target."
  say "Run it:  cd '$target' && ./daedalus-desktop"
  say "Docker Engine with the compose plugin must be installed and running."
fi
say "The launcher makes everything else - the checkouts, the keys, the data - inside that folder."
