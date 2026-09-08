# Daedalus runtime image. The agent may edit this file; a change triggers an image rebuild.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PATH="/root/.local/bin:/usr/local/bin:${PATH}"

COPY deploy/apt-packages.txt /tmp/apt-packages.txt
RUN apt-get update \
    && xargs -a /tmp/apt-packages.txt apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (pull requests), Node (Mini App build), uv. No Docker client: the container is the boundary.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv python install 3.12 \
    && git config --system --add safe.directory '*'

# Headless Chromium for the browser skills (Playwright's own build, shared by the Python package pinned in
# pyproject and by `npx lighthouse` through CHROME_PATH). The browser lives outside the virtualenv so a
# rebuild of the venv never re-downloads it.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN uvx --from playwright==1.62.0 playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s "$(find /opt/pw-browsers -type f -name chrome -path '*chrome-linux*' | head -1)" /usr/local/bin/chromium
ENV CHROME_PATH=/usr/local/bin/chromium

# The supervisor lives outside the repositories the agent can change.
COPY launcher /opt/launcher

WORKDIR /srv/daedalus
# The virtualenv lives outside the mounted checkout so host and container never share one.
ENV UV_PROJECT_ENVIRONMENT=/srv/venv \
    DAEDALUS_BOT_REPO=/srv/daedalus \
    DAEDALUS_CORE_REPO=/srv/protocore-exp \
    DAEDALUS_STATE=/srv/state \
    DAEDALUS_WORKSPACES=/srv/workspaces \
    DAEDALUS_SUPERVISOR_SOCKET=/run/daedalus/supervisor.sock

ENTRYPOINT ["python3", "/opt/launcher/supervisor.py"]
