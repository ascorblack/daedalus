# Daedalus runtime image. The agent may edit this file; a change triggers an image rebuild.
#
# Three stages. The environment is assembled where a compiler and a Node are allowed to live, and
# only the results — the virtualenv and the built Mini App — are copied into the image that ships.
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


# -- what ships -------------------------------------------------------------------------------
FROM base AS runtime

COPY --from=venv /srv/venv /srv/venv
COPY --from=miniapp /build/dist /opt/miniapp-dist
# The bot and the core are mounted, not installed: pointing the interpreter at the mounted checkouts
# is what an editable install would have written anyway, and it needs no sync at the first start.
RUN printf '%s\n' /srv/daedalus /srv/protocore-exp > /srv/venv/lib/python3.12/site-packages/_daedalus_checkouts.pth

# The supervisor lives outside the repositories the agent can change.
COPY launcher /opt/launcher
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
