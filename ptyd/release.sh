#!/bin/sh
# Builds the ptyd a release carries for one platform:
#
#   ptyd/release.sh <goos> <goarch> <output>
#
# linux and windows are cross-compiled from a Linux machine, with the pinned Zig as the C compiler:
#   linux    against musl and linked fully statically, so the one binary starts on any
#            distribution, whatever its glibc (the image's daemon is linked against glibc instead,
#            since it runs in the image it was built for);
#   windows  against MinGW, needing only the C runtime every Windows 10 and later has.
# darwin is built on a Mac with its own compiler (-arch picks the slice), because the system
# libraries a Mac program links are Apple's, not Zig's.
#
# The version is the same digest of the daemon's sources the image stamps (src-<12 hex>), so a
# daemon from a release and one from an image of the same sources say they are the same.
set -eu

goos=$1
goarch=$2
out=$3
here=$(cd "$(dirname "$0")" && pwd)

case "$goos/$goarch" in
  linux/amd64) target=x86_64-linux-musl ;;
  linux/arm64) target=aarch64-linux-musl ;;
  windows/amd64) target=x86_64-windows-gnu ;;
  windows/arm64) target=aarch64-windows-gnu ;;
  darwin/amd64) target=x86_64-macos ;;
  darwin/arm64) target=aarch64-macos ;;
  *) echo "release.sh: no release build for $goos/$goarch" >&2; exit 2 ;;
esac

cd "$here"
# Before anything is built, so the cache the build fills is never part of what it digests.
# Computed exactly as deploy/Dockerfile computes it, over the files .dockerignore lets through.
if command -v sha256sum >/dev/null 2>&1; then sum=sha256sum; else sum="shasum -a 256"; fi
# shellcheck disable=SC2086 # $sum is a command and its flags
digest=$(find . \( -path ./.cache -o -path ./ptyd -o -name '*.test' \) -prune -o -type f -print0 \
  | LC_ALL=C sort -z | xargs -0 $sum | $sum | cut -c1-12)
PKG_CONFIG_PATH=$(LIBGHOSTTY_TARGET=$target libghostty/build.sh)
export PKG_CONFIG_PATH

ldflags="-s -w -X github.com/ascorblack/daedalus/ptyd/internal/version.Version=src-$digest"
case "$goos" in
  darwin)
    arch=$( [ "$goarch" = amd64 ] && echo x86_64 || echo arm64 )
    CC="clang -arch $arch"
    ;;
  *)
    zig=$(libghostty/build.sh zig)
    CC="$zig cc -target $target"
    # Zig keeps its caches with the library's, not in the home directory.
    cache=${PTYD_CACHE:-$here/.cache}
    export ZIG_GLOBAL_CACHE_DIR="$cache/zig-global" ZIG_LOCAL_CACHE_DIR="$cache/zig-local"
    ;;
esac
if [ "$goos" = linux ]; then
  # -s once more for the external linker, which Go's own -s does not reach.
  ldflags="$ldflags -linkmode external -extldflags '-static -s'"
fi
CGO_ENABLED=1 GOOS=$goos GOARCH=$goarch CC=$CC go build -trimpath -ldflags "$ldflags" -o "$out" ./cmd/ptyd
echo "release.sh: $out ($goos/$goarch, src-$digest)" >&2
