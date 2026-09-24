# The image ptyd is built and tested in: the Go that go.mod names, plus xz to unpack the pinned Zig
# (build.sh fetches and verifies Zig itself, so the image does not pin it twice).
FROM golang:1.26
RUN apt-get update && apt-get install -y --no-install-recommends xz-utils && rm -rf /var/lib/apt/lists/*
