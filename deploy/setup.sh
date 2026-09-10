#!/usr/bin/env bash
# First-run setup: asks for what the stack needs, writes .env and the secrets file outside the checkout,
# and starts the containers. Safe to re-run: existing values are offered as defaults and never printed back
# in full. Run from the repository root: bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ENV_FILE="$ROOT/.env"
SECRETS_DIR="$(cd .. && pwd)/daedalus-secrets"
SECRETS_FILE="$SECRETS_DIR/keyproxy.env"
CORE_DIR="$(cd .. && pwd)/protocore-exp"

say() { printf '\n%s\n' "$*"; }
current() { grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- || true; }
ask() {  # ask VAR "prompt" [secret] — keeps the current value on an empty answer
  local var="$1" prompt="$2" secret="${3:-}" file="${4:-$ENV_FILE}" cur ans shown
  cur="$(current "$var" "$file")"
  if [ -n "$cur" ]; then shown="$cur"; [ -n "$secret" ] && shown="${cur:0:4}…${cur: -3}"; prompt="$prompt [$shown]"; fi
  if [ -n "$secret" ]; then read -r -s -p "$prompt: " ans; echo; else read -r -p "$prompt: " ans; fi
  ans="${ans:-$cur}"
  if grep -qE "^$var=" "$file" 2>/dev/null; then
    python3 - "$file" "$var" "$ans" <<'PY'
import sys, pathlib, re
path, var, value = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); text = p.read_text()
p.write_text(re.sub(rf"^{re.escape(var)}=.*$", lambda m: f"{var}={value}", text, count=1, flags=re.M))
PY
  else
    printf '%s=%s\n' "$var" "$ans" >> "$file"
  fi
}

say "Daedalus setup. Values go to $ENV_FILE and $SECRETS_FILE."
command -v docker >/dev/null || { echo "docker is not installed"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose is not available"; exit 1; }
[ -d "$CORE_DIR" ] || { say "The core checkout is missing: cloning protocore-exp next to this repository."; git clone -q https://github.com/ascorblack/protocore-exp "$CORE_DIR"; }
[ -t 0 ] || { echo "setup.sh asks questions: run it in a terminal"; exit 1; }
[ -f "$ENV_FILE" ] || cp deploy/env.example "$ENV_FILE"
chmod 600 "$ENV_FILE"
mkdir -p "$SECRETS_DIR" && chmod 700 "$SECRETS_DIR"
[ -f "$SECRETS_FILE" ] || cp deploy/keyproxy.env.example "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"

say "1/4 Telegram (https://t.me/BotFather for the token; @userinfobot for your numeric id; https://my.telegram.org/apps for the API pair)"
ask TELEGRAM_BOT_TOKEN "Bot token" secret
ask OWNER_USER_ID "Your numeric Telegram user id"
ask TELEGRAM_API_ID "Telegram API id"
ask TELEGRAM_API_HASH "Telegram API hash" secret

say "2/4 Model keys (stored in $SECRETS_FILE, never inside the checkout). Leave a key empty to skip that provider."
ask DEEPSEEK_API_KEY "DeepSeek API key" secret "$SECRETS_FILE"
ask OPENROUTER_API_KEY "OpenRouter API key" secret "$SECRETS_FILE"
ask KEYPROXY_USD_PER_DAY "Daily spend cap for the key proxy, USD" "" "$SECRETS_FILE"

say "3/4 GitHub and the app"
ask GITHUB_TOKEN "Fine-grained GitHub token for the two agent repositories (pull requests + contents); empty disables self-development" secret
ask MINIAPP_PUBLIC_URL "Public HTTPS address of the app (a reverse proxy in front of port 8765); empty for LAN only"
ask USD_PER_DAY "Daily spend cap enforced by the supervisor, USD"
if [ -z "$(current SEARXNG_SECRET "$ENV_FILE")" ]; then
  python3 - "$ENV_FILE" <<'PY'
import sys, pathlib, secrets, re
p = pathlib.Path(sys.argv[1]); text = p.read_text()
p.write_text(re.sub(r"^SEARXNG_SECRET=.*$", f"SEARXNG_SECRET={secrets.token_hex(24)}", text, count=1, flags=re.M))
PY
fi
python3 - "$ENV_FILE" "$ROOT" "$CORE_DIR" <<'PY'
import sys, pathlib, re
path, root, core = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); text = p.read_text()
for var, value in (("DAEDALUS_COMPOSE_PROJECT_DIR", root), ("DAEDALUS_COMPOSE_FILE", f"{root}/deploy/compose.yaml"), ("DAEDALUS_CORE_PROJECT_DIR", core)):
    text = re.sub(rf"^{var}=.*$", f"{var}={value}", text, count=1, flags=re.M) if re.search(rf"^{var}=", text, re.M) else text + f"\n{var}={value}"
p.write_text(text)
PY

say "4/4 Starting the stack (the first build takes a few minutes)."
docker compose -f deploy/compose.yaml --env-file "$ENV_FILE" up -d --build
say "Done. Send /start to the bot in Telegram. Logs: docker logs -f deploy-daedalus-1"
say "Next: /bind in a supergroup with topics for parallel sessions; /app for the Mini App; provider keys can be added later in $SECRETS_FILE."
