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

# GitHub CLI (pull requests), Node (Mini App build), Docker CLI (rebuild helper), uv.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /usr/share/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" > /etc/apt/sources.list.d/docker.list \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh docker-ce-cli docker-compose-plugin nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv python install 3.12

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
