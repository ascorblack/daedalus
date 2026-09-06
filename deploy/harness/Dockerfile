# The harness: the operator's coding-agent CLIs (claude, codex, grok) and their logins live here,
# never in the agent container. The agent hands tasks over HTTP; see server.py.
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-aiohttp curl ca-certificates git ripgrep bash procps && rm -rf /var/lib/apt/lists/*
ARG CODEX_VERSION=0.151.0
RUN npm install -g "@openai/codex@${CODEX_VERSION}" && npm cache clean --force
USER node
ENV HOME=/home/node
ENV PATH=/home/node/.local/bin:/home/node/.grok/bin:$PATH
# Claude Code's native installer; the version pin keeps the image reproducible.
ARG CLAUDE_VERSION=2.1.263
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- "${CLAUDE_VERSION}"
# grok comes with the mounted ~/.grok (its installer puts the binary there); the login files are mounted at run time.
HEALTHCHECK --interval=20s --timeout=3s --start-period=10s CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3300/healthz', timeout=2).status == 200 else 1)"]
COPY server.py /app/server.py
CMD ["python3", "/app/server.py"]
