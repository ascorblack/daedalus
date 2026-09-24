# Daedalus runtime image. The agent may edit this file; a change triggers an image rebuild.
#
# Four stages. The environment is assembled where a compiler, a Node and a Go are allowed to live,
# and only the results — the virtualenv, the built Mini App and the terminal daemon — are copied into
# the image that ships.
# The `speech` extra rides in the default image rather than being an opt-in like `browser`: the two
# wheels together are about fifteen megabytes against Playwright's several hundred, and an installation
# that never downloads a model never loads them. opus-tools comes in with it, from apt-packages.txt,
# because a Telegram voice note is Ogg Opus and a model reads samples: opusdec is one and a quarter
# megabytes and decodes exactly that. ffmpeg would decode everything else too and is deliberately not
# here — it drags four hundred and fifty megabytes of video codecs in behind it, to convert a voice clip.
#
# Two targets come out of it: `runtime` (the default, and what `:latest` is) and `browser`, which
# adds Playwright and a headless Chromium shell for the browser skills. They share every layer up
# to the last one, so having both costs the difference, not twice the image.

FROM ubuntu:24.04 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/venv \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/srv/venv/bin:/usr/local/bin:${PATH}"
# UV_PYTHON + UV_PYTHON_DOWNLOADS: the interpreter is the one apt already installed. Left to itself
# uv downloads a second CPython of its own next to it, which nothing ever runs.

COPY deploy/apt-packages.txt /tmp/apt-packages.txt
RUN apt-get update \
    && xargs -a /tmp/apt-packages.txt apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/info /usr/share/lintian /usr/share/perl/*/pod

# uv builds and syncs the environment; it stays in the image because the supervisor's preflight runs
# `uv sync` when the agent changes a dependency. uvx goes: nothing in the image runs a tool from an
# ephemeral environment any more.
# No Node, no GitHub CLI, no compiler — the first two build the Mini App and the third builds a
# wheel, and both of those happen in a builder stage.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh \
    && rm -f /usr/local/bin/uvx \
    && git config --system --add safe.directory '*'


# -- the virtualenv ---------------------------------------------------------------------------
# Built from the lock file alone, so the stage needs neither checkout. The two path dependencies —
# the bot itself and the core — are left out here and reach the interpreter as source paths in the
# final stage: they are bind-mounted at run time and the agent edits them, so an installed copy
# would be a stale second copy.
FROM base AS venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml uv.lock ./
# The last step stamps the environment with what it was built for. The runtime mounts a volume over
# /srv/venv and Docker seeds it from here, so the first start finds the stamp already matching the checkout
# and runs no uv at all; a checkout that later declares different dependencies no longer matches it, and
# the supervisor syncs before it starts the bot.
RUN uv venv /srv/venv \
    && uv export --frozen --extra speech --no-emit-project --no-emit-package protocore --format requirements-txt -o /build/requirements.txt \
    && uv export --frozen --extra browser --no-emit-project --no-emit-package protocore --format requirements-txt -o /build/requirements-browser.txt \
    && uv pip install --python /srv/venv/bin/python --no-cache --requirements /build/requirements.txt \
    && python3 -c "import hashlib, pathlib; d = hashlib.sha256(); [d.update(pathlib.Path(n).read_bytes()) for n in ('uv.lock', 'pyproject.toml')]; pathlib.Path('/srv/venv/.daedalus-dependencies').write_text(d.hexdigest())"


# -- the Mini App -----------------------------------------------------------------------------
# The bundle is not in git, and the runtime image has no Node to build one. Built here and baked in;
# the supervisor copies it into a checkout that has none.
FROM node:22-slim AS miniapp

WORKDIR /build
COPY miniapp/package.json miniapp/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY miniapp ./
RUN npm run build


# -- the terminal daemon ----------------------------------------------------------------------
# ptyd owns the terminals. It is Go with a C library linked into it — libghostty-vt, the screen
# emulator, which Zig builds from the sources ptyd/libghostty/pins.env names — and neither toolchain
# belongs in the image that ships, so they live in this stage and only the one binary leaves it.
# The binary lands in the same image as the agent because the `terminals` service in
# deploy/compose.yaml is that image started with ptyd as its entrypoint: one image to pull, and the
# daemon is always the build the host beside it was tested against.
#
# Debian bookworm's Go rather than the default tag's newer Debian: the library is static but the C
# runtime is not, and a binary linked against a newer glibc than Ubuntu 24.04's refuses to start.
FROM golang:1.26-bookworm AS ptyd

RUN apt-get update \
    && apt-get install -y --no-install-recommends xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/ptyd
# The library first, from the pins and the patches alone, so the minutes of Zig are paid again only
# when one of those moves, never for a change to the daemon's Go. Only the installed library is kept:
# the compiler, the Ghostty checkout and Zig's caches are a gigabyte nothing after this step reads.
COPY ptyd/libghostty ./libghostty
RUN PTYD_CACHE=/opt/libghostty libghostty/build.sh > /opt/libghostty.pkgconfig \
    && find /opt/libghostty -mindepth 1 -maxdepth 1 ! -name 'libghostty-*' -exec rm -rf {} +

COPY ptyd/go.mod ptyd/go.sum ./
RUN go mod download
COPY ptyd ./
# The version is a digest of the daemon's sources. The host compares the version of the daemon that
# is running with the one in its own image, and the digest differs exactly when the daemon changed —
# a rebuild of the agent for any other reason does not offer to end every terminal for nothing.
RUN export PKG_CONFIG_PATH="$(cat /opt/libghostty.pkgconfig)" \
    && digest="$(find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)" \
    && CGO_ENABLED=1 go build -trimpath \
        -ldflags "-s -w -X github.com/ascorblack/daedalus/ptyd/internal/version.Version=src-$digest" \
        -o /out/ptyd ./cmd/ptyd \
    && /out/ptyd version


# -- what ships -------------------------------------------------------------------------------
FROM base AS runtime

COPY --from=venv /srv/venv /srv/venv
COPY --from=miniapp /build/dist /opt/miniapp-dist
# The terminals service runs this as its entrypoint. The agent's container carries it too, and only
# asks it its version: that is how the host knows a newer daemon is in the image than the one running.
COPY --from=ptyd /out/ptyd /usr/local/bin/ptyd
# The bot and the core are mounted, not installed: pointing the interpreter at the mounted checkouts
# is what an editable install would have written anyway, and it needs no sync at the first start.
RUN printf '%s\n' /srv/daedalus /srv/protocore-exp > /srv/venv/lib/python3.12/site-packages/_daedalus_checkouts.pth

# The supervisor lives outside the repositories the agent can change.
COPY launcher /opt/launcher
COPY deploy/dependencies /opt/dependency-recipe
RUN python3 /opt/launcher/dependencies.py /opt/dependency-recipe/local.json
# The key proxy runs from this image as a second container with its own environment; see
# deploy/compose.yaml. Its dependencies are already in the virtualenv above.
COPY deploy/keyproxy /opt/keyproxy

WORKDIR /srv/daedalus
# The virtualenv lives outside the mounted checkout so host and container never share one.
ENV DAEDALUS_VENV_EXTRAS=speech
ENV DAEDALUS_BOT_REPO=/srv/daedalus \
    DAEDALUS_CORE_REPO=/srv/protocore-exp \
    DAEDALUS_STATE=/srv/state \
    DAEDALUS_WORKSPACES=/srv/workspaces \
    DAEDALUS_SUPERVISOR_SOCKET=/run/daedalus/supervisor.sock \
    DAEDALUS_BAKED_APP=/opt/miniapp-dist \
    DAEDALUS_BOT_CMD="/srv/venv/bin/python -m daedalus serve"

ENTRYPOINT ["python3", "/opt/launcher/supervisor.py"]


# -- the browser variant ----------------------------------------------------------------------
# `:browser` = the image above plus Playwright and the headless Chromium shell (not the full
# Chromium build, which is half again as large and drags a GPU stack in with it). The browser lives
# outside the virtualenv so a re-sync of the environment never re-downloads it.
FROM runtime AS browser

ENV DAEDALUS_VENV_EXTRAS=speech,browser
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
COPY --from=venv /build/requirements-browser.txt /tmp/requirements-browser.txt
COPY deploy/apt-packages-browser.txt /tmp/apt-packages-browser.txt
RUN apt-get update \
    && grep -v '^#' /tmp/apt-packages-browser.txt | xargs apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man \
    && uv pip install --python /srv/venv/bin/python --no-cache --requirements /tmp/requirements-browser.txt \
    && playwright install chromium-headless-shell \
    && rm -rf /root/.cache \
    && shell="$(find /opt/pw-browsers -type f \( -name chrome-headless-shell -o -name headless_shell \) | head -1)" \
    && test -n "$shell" \
    && ln -s "$shell" /usr/local/bin/chromium
ENV CHROME_PATH=/usr/local/bin/chromium
