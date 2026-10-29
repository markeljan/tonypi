#!/bin/bash
# Install pi for the TonyPi harness (option 3). Idempotent. Run as pi: bash pi/setup.sh
set -euo pipefail
NODE_VERSION=24.22.2
PI_VERSION=1.4.0
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! node --version 2>/dev/null | grep -q "^v$NODE_VERSION$"; then
  tmp=$(mktemp -d)
  curl -sSLo "$tmp/node.tar.xz" "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-arm64.tar.xz"
  (cd "$tmp" && curl -sSL "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt" | grep linux-arm64.tar.xz \
    | sed "s|node-v$NODE_VERSION-linux-arm64.tar.xz|node.tar.xz|" | sha256sum -c -)
  sudo tar -xJf "$tmp/node.tar.xz" -C /usr/local --strip-components=1 --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md
  rm -rf "$tmp"
fi
pi --version 2>/dev/null | grep -qx "$PI_VERSION" || sudo npm install -g "@earendil-works/pi-coding-agent@$PI_VERSION"

mkdir -p ~/.pi/agent
ln -sf "$HERE/models.json" ~/.pi/agent/models.json      # provider "local": LFM3-Omni on llama-server :8080
ln -sf "$HERE/settings.json" ~/.pi/agent/settings.json  # fast retries, so a dead cloud falls back to LFM3 quickly

echo "pi $(pi --version) on node $(node --version)."
for p in openai anthropic; do echo "  $p: $(pi auth check --provider $p </dev/null 2>&1)"; done
cat <<'EOF'
Cloud brain (optional; without it Tony uses the onboard LFM3):
  pi            then  /login  → "OpenAI (ChatGPT subscription)" → "Sign in with ChatGPT"
                (open the URL on any device; if it ends on a localhost page that doesn't load, paste that URL back)
                and/or /login → Anthropic (Claude Pro/Max; billed as extra usage).   /quit when done.
Run:  tonypi-pi            (front end: voice + /run/tonypi/pi.sock; stop tonypi-agent first, it owns the mic)
      tonypi-pi-chat "wave"
EOF
