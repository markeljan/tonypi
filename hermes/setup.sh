#!/usr/bin/env bash
# Install Hermes Agent and the "tonypi" profile (robot MCP, SOUL, robot skills, providers). Idempotent.
#
#   hermes/setup.sh [--brain codex|anthropic|local] [--model NAME] [--mcp-url URL]
#
#   codex      ChatGPT subscription (default model gpt-6.1-terra; this account: gpt-6.1-sol/terra, gpt-6-luna, gpt-5.6-*). Log in once:  tonypi auth add openai-codex --type oauth --no-browser
#   anthropic  ANTHROPIC_API_KEY in ~/.hermes/profiles/tonypi/.env (or Claude Max OAuth: tonypi auth add anthropic)
#   local      LFM3-Omni-2B on tonypi-llm (:8080) only
# Cloud brains get LFM3 as fallback_providers. Writes the API server key to /home/pi/.config/tonypi/secrets.env.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRAIN=codex MODEL="" MCP_URL="http://127.0.0.1:8765/mcp"
while [ $# -gt 0 ]; do
    case "$1" in
        --brain) BRAIN="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --mcp-url) MCP_URL="$2"; shift 2 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done
PROFILE="$HOME/.hermes/profiles/tonypi"
SECRETS=/home/pi/.config/tonypi/secrets.env

# 1. Hermes itself (official installer; no browser / computer-use drivers on the Pi)
if ! command -v hermes >/dev/null; then
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/install.sh" https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh
    bash "$tmp/install.sh" --non-interactive --skip-browser --skip-computer-use
    rm -rf "$tmp"
fi

# 2. Blank profile: no bundled skills, its own memory/sessions; wrapper command `tonypi`
[ -d "$PROFILE" ] || hermes profile create tonypi --no-skills \
    --description "Tony, the TonyPi humanoid robot's brain: drives the body via the tonypi MCP server."

# 3. Config, persona, providers
install -m 600 "$REPO/hermes/SOUL.md" "$PROFILE/SOUL.md"
# robot skills: the repo copy replaces Hermes' copy of the same skill (skills Hermes wrote itself are kept). Not an
# external_dirs entry: Hermes' background review edited those files in place.
# first facts for Hermes memory, only into an empty memory (what Hermes learns later is kept)
[ -s "$PROFILE/memories/MEMORY.md" ] || { mkdir -p "$PROFILE/memories"; cp "$REPO/hermes/memory-seed.md" "$PROFILE/memories/MEMORY.md"; }
mkdir -p "$PROFILE/skills"
cp -r "$REPO/hermes/skills/." "$PROFILE/skills/"
python3 - "$REPO" "$PROFILE/config.yaml" "$BRAIN" "$MODEL" "$MCP_URL" <<'PY'
import sys, yaml
repo, out, brain, model, mcp_url = sys.argv[1:]
cfg = yaml.safe_load(open(f"{repo}/hermes/config.yaml").read().replace("__REPO__", repo))
cfg["mcp_servers"]["tonypi"]["url"] = mcp_url
if brain == "codex":
    cfg["model"] = {"default": model or "gpt-6.1-terra", "provider": "openai-codex"}
elif brain == "anthropic":  # untested (the owner uses the ChatGPT login)
    cfg["model"] = {"default": model or "claude-sonnet-5-5", "provider": "anthropic"}
    for task in cfg["auxiliary"].values():
        if "provider" in task:
            task.update(provider="anthropic", model=cfg["model"]["default"])
elif brain == "local":
    cfg.pop("fallback_providers")
    for task in cfg["auxiliary"].values():
        if "provider" in task:
            task.update(provider="custom", model="lfm3-omni-2b", base_url="http://127.0.0.1:8080/v1")
else:
    sys.exit(f"unknown brain {brain}")
try:  # keep the version stamp Hermes wrote
    cfg["_config_version"] = yaml.safe_load(open(out)).get("_config_version", 51)
except FileNotFoundError:
    pass
open(out, "w").write(yaml.safe_dump(cfg, sort_keys=False))
PY
chmod 600 "$PROFILE/config.yaml"

# 4. API server (the voice front end talks to it): localhost only, key shared via secrets.env
KEY="$(grep -s '^HERMES_API_KEY=' "$SECRETS" | cut -d= -f2- || true)"
if [ -z "$KEY" ]; then
    KEY="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
    mkdir -p "$(dirname "$SECRETS")"; touch "$SECRETS"; chmod 600 "$SECRETS"
    echo "HERMES_API_KEY=$KEY" >> "$SECRETS"
fi
env_set() {  # KEY VALUE in the profile .env
    sed -i "/^$1=/d" "$PROFILE/.env"; echo "$1=$2" >> "$PROFILE/.env"
}
env_set API_SERVER_ENABLED true
env_set API_SERVER_HOST 127.0.0.1
env_set API_SERVER_PORT 8642
env_set API_SERVER_KEY "$KEY"

echo "tonypi profile ready (brain: $BRAIN). Start the gateway:  tonypi gateway run   (or hermes/systemd units)"
[ "$BRAIN" = codex ] && ! tonypi auth status openai-codex 2>/dev/null | grep -qi "logged in\|valid\|active" && \
    echo "ChatGPT not logged in yet. Run:  tonypi auth add openai-codex --type oauth --no-browser"
exit 0
