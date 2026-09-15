#!/usr/bin/env bash
# Cross-compiles the launcher for the five platforms we publish. The build runs inside the Go
# container, so the host needs Docker and nothing else — the same dependency the launcher itself has.
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
    for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64; do
      goos="${target%/*}"; goarch="${target#*/}"
      out="dist/daedalus-desktop-$goos-$goarch"
      case "$goos" in windows) out="$out.exe";; esac
      echo "building $out"
      GOOS="$goos" GOARCH="$goarch" go build -trimpath -ldflags "-s -w -X main.version=$VERSION" -o "$out" .
    done
  '

ls -lh dist
