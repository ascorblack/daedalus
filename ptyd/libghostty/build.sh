#!/bin/sh
# Builds libghostty-vt, the screen emulator ptyd links statically, from the pinned sources in
# pins.env, and prints the pkg-config directory to build ptyd against:
#
#   export PKG_CONFIG_PATH="$(libghostty/build.sh)"
#   go build ./cmd/ptyd
#
# Everything is fetched into a cache (PTYD_CACHE, default ptyd/.cache) and verified: the Zig tarball
# by its published SHA-256, the Ghostty source by its commit hash, Zig's own dependencies by the
# hashes in Ghostty's build.zig.zon. A second run with the same pins and patches builds nothing.
#
# LIBGHOSTTY_TARGET selects the Zig target triple (default: this machine's architecture, linux-gnu).
# An explicit target also keeps the library at the architecture's baseline CPU, so a binary built on
# a new machine still runs on an old one. A Zig of the pinned version already on PATH is used as is.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
. "$here/pins.env"
cache=${PTYD_CACHE:-$here/../.cache}
mkdir -p "$cache"
cache=$(cd "$cache" && pwd)
jobs=${LIBGHOSTTY_JOBS:-4}

say() { printf 'libghostty: %s\n' "$*" >&2; }

host_arch=$(uname -m)
case "$host_arch" in
  x86_64 | amd64) host_arch=x86_64 ;;
  aarch64 | arm64) host_arch=aarch64 ;;
  *) say "no pinned Zig for $host_arch"; exit 1 ;;
esac
target=${LIBGHOSTTY_TARGET:-$host_arch-linux-gnu}

sha256() { sha256sum "$1" | cut -d' ' -f1; }

# Zig.
zig=zig
if ! command -v zig >/dev/null 2>&1 || [ "$(zig version)" != "$ZIG_VERSION" ]; then
  zig_dir="$cache/zig-$host_arch-linux-$ZIG_VERSION"
  if [ ! -x "$zig_dir/zig" ]; then
    eval "want=\$ZIG_SHA256_$host_arch"
    tarball="$cache/zig-$host_arch-linux-$ZIG_VERSION.tar.xz"
    say "fetching Zig $ZIG_VERSION"
    curl -fsSL -o "$tarball.part" "https://ziglang.org/download/$ZIG_VERSION/zig-$host_arch-linux-$ZIG_VERSION.tar.xz"
    got=$(sha256 "$tarball.part")
    if [ "$got" != "$want" ]; then
      say "Zig tarball SHA-256 is $got, expected $want"
      rm -f "$tarball.part"
      exit 1
    fi
    mv "$tarball.part" "$tarball"
    tar -C "$cache" -xJf "$tarball"
    rm -f "$tarball"
  fi
  zig="$zig_dir/zig"
fi

# The output directory is named by everything that affects the library, so a changed pin or patch
# can never be served a stale build.
key=$( (echo "$GHOSTTY_COMMIT $ZIG_VERSION $target ${LIBGHOSTTY_CPU:-}"; cat "$here"/patches/*.patch) | sha256sum | cut -c1-16)
prefix="$cache/libghostty-$key"
if [ -f "$prefix/.built" ]; then
  echo "$prefix/share/pkgconfig"
  exit 0
fi

# The Ghostty source at the pinned commit, clean, with the patches applied. LIBGHOSTTY_SOURCE may
# name a local clone to fetch from instead of GitHub; the commit hash is checked either way.
src="$cache/ghostty-src"
if [ ! -d "$src/.git" ]; then
  git init -q "$src"
fi
if ! git -C "$src" cat-file -e "$GHOSTTY_COMMIT^{commit}" 2>/dev/null; then
  say "fetching Ghostty $GHOSTTY_COMMIT"
  git -C "$src" fetch -q --depth 1 "${LIBGHOSTTY_SOURCE:-$GHOSTTY_REPOSITORY}" "$GHOSTTY_COMMIT"
fi
git -C "$src" checkout -q -f --detach "$GHOSTTY_COMMIT"
git -C "$src" clean -q -f -d -x
if [ "$(git -C "$src" rev-parse HEAD)" != "$GHOSTTY_COMMIT" ]; then
  say "Ghostty checkout is not at $GHOSTTY_COMMIT"
  exit 1
fi
for p in "$here"/patches/*.patch; do
  git -C "$src" apply --whitespace=nowarn "$p"
done

say "building libghostty-vt for $target (a minute or more)"
rm -rf "$prefix"
(
  cd "$src"
  ZIG_GLOBAL_CACHE_DIR="$cache/zig-global" ZIG_LOCAL_CACHE_DIR="$cache/zig-local" \
    "$zig" build -Demit-lib-vt -Doptimize=ReleaseFast -Dtarget="$target" ${LIBGHOSTTY_CPU:+-Dcpu="$LIBGHOSTTY_CPU"} -j"$jobs" --prefix "$prefix" >&2
)
# Only the static library is linked; the shared one is removed so nothing can pick it up instead.
rm -f "$prefix"/lib/*.so* "$prefix"/lib/*.dylib "$prefix"/lib/*.dll
touch "$prefix/.built"
echo "$prefix/share/pkgconfig"
