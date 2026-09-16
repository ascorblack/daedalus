#!/usr/bin/env bash
# Cross-compiles the launcher for the five platforms we publish, in the `nowebview` variant. The
# build runs inside the Go container, so the host needs Docker and nothing else — the same
# dependency the launcher itself has.
#
# What this does not build is the window. The window is the operating system's own web view, which
# means cgo, and cgo means a compiler and that platform's headers: a macOS build has to be made on
# macOS and a Windows build on Windows. That is what the release workflow's matrix is for. A binary
# built here behaves as the launcher did before there was a window — it opens the app in a
# Chromium-family browser in application mode, or in the default browser — which is also exactly
# what the published Linux binaries do, and for a reason the README gives.
#
# To build the windowed launcher for the machine you are on, on macOS or Windows:
#
#   go build -trimpath -o daedalus-desktop .
set -euo pipefail
cd "$(dirname "$0")"

GO_IMAGE="${GO_IMAGE:-golang:1.23}"
VERSION="${VERSION:-$(git describe --tags --always --dirty 2>/dev/null || date -u +%Y-%m-%d)}"

rm -rf dist
mkdir -p dist

docker run --rm \
  -v "$PWD":/src -w /src \
  -u "$(id -u):$(id -g)" \
  -e HOME=/tmp -e GOENV=/tmp/goenv -e GOFLAGS=-mod=mod -e GOCACHE=/tmp/gocache -e GOMODCACHE=/tmp/gomod \
  -e CGO_ENABLED=0 -e VERSION="$VERSION" \
  "$GO_IMAGE" sh -euc '
    go vet ./...
    go test ./...
    go vet -tags nowebview ./...
    go test -tags nowebview ./...
    for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64; do
      goos="${target%/*}"; goarch="${target#*/}"
      out="dist/daedalus-desktop-$goos-$goarch"
      case "$goos" in windows) out="$out.exe";; esac
      echo "building $out"
      GOOS="$goos" GOARCH="$goarch" go build -tags nowebview -trimpath -ldflags "-s -w -X main.version=$VERSION" -o "$out" .
    done
  '

ls -lh dist
