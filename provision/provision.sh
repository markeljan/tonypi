#!/usr/bin/env bash
# TonyPi first-boot provisioning for Raspberry Pi OS Lite 64-bit (trixie). Idempotent: safe to re-run.
# Runs as root (tonypi-provision.service on first boot, or: sudo /home/pi/tonypi/provision/provision.sh).
# Log: /var/log/tonypi-provision.log
set -uo pipefail   # no -e: one failed step must not block the rest; steps are stamped only on success

USER_NAME=pi
HOME_DIR=/home/$USER_NAME
REPO=$HOME_DIR/tonypi
SEED=/opt/tonypi-seed
MODELS=$HOME_DIR/models
LLAMA_SRC=$HOME_DIR/src/llama.cpp
STAMP_DIR=/var/lib/tonypi
mkdir -p "$STAMP_DIR"

log() { echo "[$(date '+%F %T')] $*"; }
as_user() { sudo -u "$USER_NAME" -H env PATH="$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }
step_done() { [[ -f $STAMP_DIR/$1.done ]]; }
mark() { touch "$STAMP_DIR/$1.done"; }

# ---------------------------------------------------------------------------
log "repo"
if [[ ! -d $REPO && -d $SEED ]]; then
  cp -a "$SEED" "$REPO"
fi
chown -R "$USER_NAME:$USER_NAME" "$REPO"

# ---------------------------------------------------------------------------
log "wait for network"
for _ in $(seq 1 60); do
  if curl -fsI --max-time 5 https://deb.debian.org >/dev/null 2>&1; then break; fi
  sleep 5
done

# ---------------------------------------------------------------------------
if ! step_done apt; then
  log "apt packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get -y full-upgrade
  PKGS=(git curl ca-certificates jq tmux htop rsync unzip bzip2
    build-essential cmake ninja-build pkg-config ccache libssl-dev
    python3-dev python3-venv
    i2c-tools v4l-utils alsa-utils libasound2-dev libportaudio2 portaudio19-dev
    libsndfile1 ffmpeg espeak-ng
    python3-lgpio python3-libgpiod)
  if ! apt-get -y install "${PKGS[@]}"; then
    for p in "${PKGS[@]}"; do apt-get -y install "$p" || log "  apt: could not install $p"; done
  fi
  apt-get -y autoremove
  command -v cmake >/dev/null && mark apt
fi

# ---------------------------------------------------------------------------
log "user groups, udev, journald, boot config"
for g in dialout gpio i2c spi video audio input render plugdev; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$USER_NAME"
done
install -m 0644 "$REPO/provision/files/99-tonypi.rules" /etc/udev/rules.d/99-tonypi.rules
udevadm control --reload-rules && udevadm trigger || true

mkdir -p /etc/systemd/journald.conf.d
# persistent: a freeze leaves logs behind (2026-10-29)
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\nRuntimeMaxUse=64M\n' > /etc/systemd/journald.conf.d/tonypi.conf
systemctl restart systemd-journald || true

log "earlyoom (kill optional jobs before RAM runs out; robot core protected)"
dpkg -s earlyoom >/dev/null 2>&1 || apt-get -y install earlyoom || log "  apt: could not install earlyoom"
install -m 0644 "$REPO/provision/files/earlyoom.default" /etc/default/earlyoom
systemctl enable earlyoom >/dev/null 2>&1 && systemctl restart earlyoom || true

CFG=/boot/firmware/config.txt
add_cfg() { grep -qxF "$1" "$CFG" || echo "$1" >> "$CFG"; }
add_cfg "dtparam=uart0=on"
add_cfg "dtparam=i2c_arm=on"
add_cfg "usb_max_current_enable=1"
# the controller UART must never carry a login console
sed -i -E 's/console=(serial0|ttyAMA0),[0-9]+ ?//g' /boot/firmware/cmdline.txt
systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
grep -qxF i2c-dev /etc/modules || echo i2c-dev >> /etc/modules
modprobe i2c-dev && udevadm trigger --subsystem-match=i2c-dev --action=add || true   # usable now, not only after reboot

# ---------------------------------------------------------------------------
if ! step_done uv; then
  log "uv"
  as_user sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' && mark uv
fi
# on PATH for non-login shells and services too, not only via ~/.profile
for b in uv uvx; do
  [[ -x $HOME_DIR/.local/bin/$b ]] && ln -sf "$HOME_DIR/.local/bin/$b" /usr/local/bin/$b
done

# ---------------------------------------------------------------------------
if ! step_done llama; then
  log "llama.cpp (native build)"
  as_user mkdir -p "$HOME_DIR/src"
  if [[ ! -d $LLAMA_SRC ]]; then
    as_user git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA_SRC"
  else
    as_user git -C "$LLAMA_SRC" pull --ff-only || true
  fi
  as_user cmake -S "$LLAMA_SRC" -B "$LLAMA_SRC/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DGGML_CPU_KLEIDIAI=ON \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON
  as_user cmake --build "$LLAMA_SRC/build" -j4 --target llama-server llama-cli llama-bench llama-mtmd-cli
  as_user cmake --build "$LLAMA_SRC/build" -j4 --target llama-tts || log "  llama-tts target not available"
  for b in llama-server llama-cli llama-bench llama-mtmd-cli llama-tts; do
    [[ -x $LLAMA_SRC/build/bin/$b ]] && ln -sf "$LLAMA_SRC/build/bin/$b" /usr/local/bin/$b
  done
  if [[ -x /usr/local/bin/llama-server ]]; then mark llama; else log "  llama.cpp build FAILED"; fi
fi

# ---------------------------------------------------------------------------
log "models"
as_user mkdir -p "$MODELS"
# name|url  (public Hugging Face files; re-runs skip finished downloads)
while IFS='|' read -r name url; do
  [[ -z $name || $name == \#* ]] && continue
  if [[ ! -s $MODELS/$name ]]; then
    log "  downloading $name"
    as_user curl -fL --retry 5 -C - -o "$MODELS/$name.part" "$url" && as_user mv "$MODELS/$name.part" "$MODELS/$name"
  fi
done < "$REPO/provision/models.txt"
for t in "$MODELS"/*.tar.bz2; do
  [[ -e $t && ! -d ${t%.tar.bz2} ]] && as_user tar -xjf "$t" -C "$MODELS"
done

# ---------------------------------------------------------------------------
log "python env"
as_user bash -c "cd '$REPO' && uv sync --all-extras" || log "  uv sync failed (fix and re-run)"

# ---------------------------------------------------------------------------
log "systemd units"
for unit in "$REPO"/provision/systemd/*.service; do
  [[ -e $unit ]] || continue
  install -m 0644 "$unit" /etc/systemd/system/
done
systemctl daemon-reload
ln -sf "$REPO/.venv/bin/tonypi-chat" /usr/local/bin/tonypi-chat
# USB mic gain to max (default ~82%): the robot's mic is weak at a distance
amixer -c 0 -q cset name='Mic Capture Volume' 496 && alsactl store || log "  could not set mic gain"
for u in tonypi-llm.service tonypi-tonyd.service tonypi-agent.service tonypi-mcp.service; do
  [[ -f /etc/systemd/system/$u ]] && systemctl enable --now "$u" || true
done

if [[ -f $STAMP_DIR/apt.done && -f $STAMP_DIR/uv.done && -f $STAMP_DIR/llama.done ]]; then
  touch "$STAMP_DIR/provisioned"
  log "provisioning complete"
else
  log "provisioning INCOMPLETE — see above; re-run: sudo $REPO/provision/provision.sh"
fi
