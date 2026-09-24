#!/usr/bin/env bash
# The host terminal: ptyd on this server as a systemd user unit of yours, reached by the agent's
# container through a directory both sides see. deploy/setup.sh runs it; it can also be run by hand,
# as yourself (never with sudo — the unit, the binary and the shells it opens are yours):
#
#   bash deploy/host-terminal.sh install [--yes]   copy this build's ptyd out of the image, install
#                                                  the unit and start it (again: update and restart)
#   bash deploy/host-terminal.sh remove            stop it, and remove the unit and the binary
#   bash deploy/host-terminal.sh status            whether it runs and answers
#   bash deploy/host-terminal.sh render <run-dir>  print the unit as it would be installed
#   bash deploy/host-terminal.sh run-dir           print the run directory this checkout uses
#
# DAEDALUS_PTYD_IMAGE names the image the binary is copied from (default daedalus:local, the one
# compose builds).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/deploy/ptyd/daedalus-ptyd.service"
UNIT_NAME="daedalus-ptyd.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME"
# These three follow the %h paths in the template; the unit and this script must agree on them.
BIN_DIR="$HOME/.local/lib/daedalus"
BIN="$BIN_DIR/ptyd"
ENV_FILE_OUT="$HOME/.config/daedalus/ptyd.env"
STATE_DIR="$HOME/.local/state/daedalus-ptyd"
IMAGE="${DAEDALUS_PTYD_IMAGE:-daedalus:local}"
USER="${USER:-$(id -un)}"

say() { printf '%s\n' "$*"; }
fail() { printf 'host terminal: %s\n' "$*" >&2; exit 1; }

# run_dir is the directory compose mounts into the agent: DAEDALUS_HOST_TERMINALS_DIR from .env, or
# the sibling of this checkout that compose.yaml defaults to (relative there to deploy/, so ../..).
run_dir() {
  local value=""
  if [ -f "$ROOT/.env" ]; then
    value="$(grep -E '^DAEDALUS_HOST_TERMINALS_DIR=' "$ROOT/.env" | head -1 | cut -d= -f2- || true)"
  fi
  if [ -z "$value" ]; then
    value="$(cd "$ROOT/.." && pwd)/daedalus-host-terminals"
  elif [ "${value#/}" = "$value" ]; then
    # compose resolves a relative path against deploy/, where the compose file is.
    value="$(cd "$ROOT/deploy" && realpath -m "$value")"
  fi
  printf '%s\n' "$value"
}

# render prints the unit with its run directory filled in. A path systemd would read differently
# than written is refused rather than escaped: a double quote or a backslash would end or bend the
# quoted argument, and % starts a specifier, so it is doubled.
render() {
  local dir="$1"
  case "$dir" in
    /*) ;;
    *) fail "the run directory must be an absolute path, not $dir" ;;
  esac
  case "$dir" in
    *'"'* | *\\* | *$'\n'*) fail "the run directory $dir holds a quote, a backslash or a newline, which a unit file cannot carry" ;;
  esac
  local escaped="${dir//%/%%}"
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "${line//@RUN_DIR@/$escaped}"
  done <"$TEMPLATE"
}

# prepare_dir creates the run directory as yours, 0700. One Docker created before this (a bind mount
# whose source was missing) belongs to root, and the unit, running as you, could not write to it.
prepare_dir() {
  local dir="$1" owner
  mkdir -p "$dir" 2>/dev/null || true
  [ -d "$dir" ] || fail "cannot create $dir"
  owner="$(stat -c %u "$dir")"
  if [ "$owner" != "$(id -u)" ]; then
    say "$dir belongs to uid $owner, not to you: Docker created it as root before the host terminal existed."
    say "Hand it over, then run this again:  sudo chown $(id -un): \"$dir\""
    return 1
  fi
  chmod 700 "$dir"
}

user_systemd() {
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    say "systemctl --user cannot reach your user manager. Log in to this server as yourself directly (ssh, not su or sudo)"
    say "and run this again; the host terminal is a unit of your own user manager."
    return 1
  fi
}

confirm() {  # confirm "question" — yes only on an explicit y
  local answer
  [ "${ASSUME_YES:-}" = 1 ] && return 0
  [ -t 0 ] || return 1
  read -r -p "$1 [y/N]: " answer
  case "$answer" in y | Y | yes | YES) return 0 ;; esac
  return 1
}

wait_endpoint() {
  local dir="$1"
  for _ in $(seq 1 30); do
    [ -f "$dir/endpoint" ] && return 0
    sleep 0.5
  done
  return 1
}

install_unit() {
  local dir restarting=""
  dir="$(run_dir)"
  user_systemd || exit 1
  prepare_dir "$dir" || exit 1
  if systemctl --user is-active --quiet "$UNIT_NAME"; then
    restarting=1
    say "The host terminal is running. Updating it restarts it, and that ends every open host terminal."
    confirm "Update and restart it now?" || { say "Left as it is."; return 0; }
  fi

  # The same binary as the container's, so the two daemons and this build speak one protocol. It is
  # linked against glibc only, so it runs here when this system's glibc is as new as the image's.
  command -v docker >/dev/null || fail "docker is not installed; the binary is copied out of the image"
  docker image inspect "$IMAGE" >/dev/null 2>&1 || fail "no image $IMAGE: build the stack first (docker compose -f deploy/compose.yaml --env-file .env build daedalus)"
  mkdir -p "$BIN_DIR"
  docker run --rm --entrypoint cat "$IMAGE" /usr/local/bin/ptyd >"$BIN.new" || { rm -f "$BIN.new"; fail "the image $IMAGE has no /usr/local/bin/ptyd; rebuild it from this checkout"; }
  chmod 755 "$BIN.new"
  if ! "$BIN.new" version >/dev/null 2>&1; then
    rm -f "$BIN.new"
    fail "the daemon from the image does not run on this system (its C library is older than the image's); use native mode on this machine instead"
  fi
  mv -f "$BIN.new" "$BIN"

  mkdir -p "$(dirname "$ENV_FILE_OUT")" "$UNIT_DIR"
  # The PATH of the shell running this, so a CLI under ~/.local/bin is found by what the daemon runs.
  printf 'PATH=%s\n' "$PATH" >"$ENV_FILE_OUT.tmp"
  if [ -n "${LANG:-}" ]; then printf 'LANG=%s\n' "$LANG" >>"$ENV_FILE_OUT.tmp"; fi
  mv -f "$ENV_FILE_OUT.tmp" "$ENV_FILE_OUT"
  render "$dir" >"$UNIT_FILE.tmp"
  mv -f "$UNIT_FILE.tmp" "$UNIT_FILE"

  # Without lingering, systemd stops a user's units when the user's last session ends, and the host
  # terminal would die at logout.
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != "yes" ]; then
    if ! loginctl enable-linger "$USER" 2>/dev/null; then
      say "Could not keep your user manager running after logout; without it the host terminal stops when you log out."
      say "Fix it once with:  sudo loginctl enable-linger $USER"
    fi
  fi

  systemctl --user daemon-reload
  if [ -n "$restarting" ]; then
    systemctl --user restart "$UNIT_NAME"
  else
    systemctl --user enable --now "$UNIT_NAME"
  fi
  if wait_endpoint "$dir"; then
    say "The host terminal answers in $dir ($("$BIN" version))."
    say "Remove it with: systemctl --user disable --now daedalus-ptyd"
  else
    say "The host terminal did not come up within 15 s. Its log: journalctl --user -u daedalus-ptyd -n 50"
    exit 1
  fi
}

remove_unit() {
  user_systemd || exit 1
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_FILE" "$BIN" "$ENV_FILE_OUT"
  systemctl --user daemon-reload
  say "The host terminal is removed. Its journal of agent writes stays in $STATE_DIR."
}

status_unit() {
  local dir
  dir="$(run_dir)"
  say "run directory: $dir"
  if [ -f "$UNIT_FILE" ]; then
    say "unit: $(systemctl --user is-active "$UNIT_NAME" 2>/dev/null || true) ($UNIT_FILE)"
  else
    say "unit: not installed"
  fi
  if [ -f "$dir/endpoint" ]; then say "endpoint: present"; else say "endpoint: absent"; fi
}

case "${1:-}" in
  install)
    if [ "${2:-}" = "--yes" ]; then ASSUME_YES=1; fi
    install_unit
    ;;
  remove) remove_unit ;;
  status) status_unit ;;
  render)
    [ -n "${2:-}" ] || fail "render needs the run directory"
    render "$2"
    ;;
  run-dir) run_dir ;;
  prepare-dir) prepare_dir "$(run_dir)" ;;
  *)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
