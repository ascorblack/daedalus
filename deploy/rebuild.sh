#!/bin/sh
# This service alone owns the Docker socket; model tools can never reach it.
set -u
# Compose resolves sibling env files even with --no-deps. This sidecar replaces
# only daedalus, so it must neither read proxy credentials nor recreate the proxy.
export DAEDALUS_SECRETS_FILE=/dev/null
trigger=${DAEDALUS_REBUILD_TRIGGER_DIR:-/run/daedalus-rebuild}
# A sidecar crash does not erase the accepted request. Rebuilding the same recipe is safe;
# replacing a container twice is preferable to reporting success for an abandoned build.
if [ -f "$trigger/dependencies-running" ] && [ ! -f "$trigger/dependencies-request" ]; then
    mv "$trigger/dependencies-running" "$trigger/dependencies-request"
fi
while true; do
    touch "$trigger/alive" "$trigger/dependencies-alive"
    if [ -f "$trigger/dependencies-request" ]; then
        job=$(cat "$trigger/dependencies-request")
        case "$job" in
            ''|*[!a-f0-9]*) rm -f "$trigger/dependencies-request"; continue ;;
        esac
        if [ "${#job}" -ne 32 ]; then
            rm -f "$trigger/dependencies-request"
            continue
        fi
        mv "$trigger/dependencies-request" "$trigger/dependencies-running"
        result=failed
        if docker compose -f "$COMPOSE_FILE" --env-file .env build daedalus > "$trigger/dependencies-build.log" 2>&1; then
            if docker compose -f "$COMPOSE_FILE" --env-file .env up -d --no-build --no-deps daedalus >> "$trigger/dependencies-build.log" 2>&1; then
                result=completed
            else
                result='container replacement failed; inspect rebuilder logs'
            fi
        else
            result='image build failed; the running container was not replaced; inspect rebuilder logs'
        fi
        cat "$trigger/dependencies-build.log"
        printf '%s\n' "$result" > "$trigger/dependencies-$job.pending"
        mv "$trigger/dependencies-$job.pending" "$trigger/dependencies-$job.result"
        rm -f "$trigger/dependencies-running"
    elif [ -f "$trigger/rebuild" ]; then
        rm -f "$trigger/rebuild"
        docker compose -f "$COMPOSE_FILE" --env-file .env up -d --build --no-deps daedalus || echo 'rebuild failed'
    fi
    sleep 5
done
