#!/bin/sh
# This service alone owns the Docker socket; model tools can never reach it.
set -u
# Compose resolves sibling env files even with --no-deps. This sidecar replaces
# only daedalus, so it must neither read proxy credentials nor recreate the proxy.
export DAEDALUS_SECRETS_FILE=/dev/null
trigger=${DAEDALUS_REBUILD_TRIGGER_DIR:-/run/daedalus-rebuild}
# A build can take minutes. Availability must not expire while the worker is busy.
heartbeat() {
    while true; do
        touch "$trigger/alive" "$trigger/dependencies-alive"
        sleep 5
    done
}
heartbeat &
heartbeat_pid=$!
trap 'kill "$heartbeat_pid" 2>/dev/null || true' EXIT
stage() {
    printf '{"stage":"%s","at":%s,"restart_at":%s}\n' "$1" "$(date +%s)" "${2:-0}" > "$trigger/dependencies-$job.progress.pending"
    mv "$trigger/dependencies-$job.progress.pending" "$trigger/dependencies-$job.progress"
}
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
        build_log="$trigger/dependencies-$job.log"
        stage building
        result=failed
        if docker compose --progress plain -f "$COMPOSE_FILE" --env-file .env build daedalus > "$build_log" 2>&1; then
            stage restart_pending "$(($(date +%s) + 30))"
            # Every signed-in page polls the notice. Keep serving it before replacing the app.
            sleep 30
            stage restarting
            if docker compose -f "$COMPOSE_FILE" --env-file .env up -d --no-build --no-deps daedalus >> "$build_log" 2>&1; then
                result=completed
            else
                result='container replacement failed; inspect rebuilder logs'
            fi
        else
            result='image build failed; the running container was not replaced; inspect rebuilder logs'
        fi
        cat "$build_log"
        printf '%s\n' "$result" > "$trigger/dependencies-$job.pending"
        mv "$trigger/dependencies-$job.pending" "$trigger/dependencies-$job.result"
        rm -f "$trigger/dependencies-running"
    elif [ -f "$trigger/rebuild" ]; then
        rm -f "$trigger/rebuild"
        docker compose -f "$COMPOSE_FILE" --env-file .env up -d --build --no-deps daedalus || echo 'rebuild failed'
    fi
    sleep 5
done
