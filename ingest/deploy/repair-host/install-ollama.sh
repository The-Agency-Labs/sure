#!/bin/bash
# Install Ollama on the machine that answers repair requests (one with a GPU).
# Uses Ollama's official Linux build, which bundles its own CUDA runtimes; distro
# packages built against the newest CUDA can drop older cards (Pascal and earlier).
#
# It runs as a dedicated user under a hardened systemd unit, listens only on this
# machine's tailnet IP, unloads the model after every answer, and (with ufw)
# accepts port 11434 only from the finance VM.
#
# Run as root:
#   install-ollama.sh <version> <sha256 of ollama-linux-amd64.tar.zst> <listen ip> <allowed client ip> [tarball]
# Take the version and digest from https://github.com/ollama/ollama/releases.
set -euo pipefail

VERSION=${1:?version, e.g. v0.40.13}
SHA256=${2:?sha256 of ollama-linux-amd64.tar.zst}
LISTEN=${3:?this machine's tailnet IP}
ALLOW=${4:?the finance VM's tailnet IP}
TARBALL=${5:-}

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
command -v zstd >/dev/null || { echo "install zstd first" >&2; exit 1; }
ip -4 -o addr show | grep -qF "inet $LISTEN/" || { echo "$LISTEN is not an address on this machine" >&2; exit 1; }

if [ -z "$TARBALL" ]; then
  TARBALL=$(mktemp --suffix=.tar.zst)
  trap 'rm -f "$TARBALL"' EXIT
  curl -fL -o "$TARBALL" "https://github.com/ollama/ollama/releases/download/$VERSION/ollama-linux-amd64.tar.zst"
fi
echo "$SHA256  $TARBALL" | sha256sum -c -

echo "== user and files"
id ollama >/dev/null 2>&1 || useradd --system --home-dir /var/lib/ollama --create-home --shell /usr/sbin/nologin ollama
install -d -o ollama -g ollama -m 0750 /var/lib/ollama /var/lib/ollama/models
DEST=/opt/ollama/$VERSION
rm -rf "$DEST" && mkdir -p "$DEST"
tar --zstd -xf "$TARBALL" -C "$DEST"
chown -R root:root "$DEST"
ln -sfn "$DEST" /opt/ollama/current
ln -sf /opt/ollama/current/bin/ollama /usr/local/bin/ollama

echo "== systemd unit"
cat > /etc/systemd/system/ollama.service <<EOF
[Unit]
Description=Ollama (repair model for the finance VM)
Wants=network-online.target tailscaled.service
After=network-online.target tailscaled.service

[Service]
User=ollama
Group=ollama
# Listen on the tailnet IP only; wait for tailscaled to bring it up.
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 120); do ip -4 -o addr show | grep -qF "inet $LISTEN/" && exit 0; sleep 1; done; echo "tailnet IP $LISTEN not up" >&2; exit 1'
ExecStart=/opt/ollama/current/bin/ollama serve
Environment=OLLAMA_HOST=$LISTEN:11434
Environment=OLLAMA_MODELS=/var/lib/ollama/models
Environment=OLLAMA_KEEP_ALIVE=0
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_MAX_LOADED_MODELS=1
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/ollama
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ollama.service

echo "== firewall"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow in on tailscale0 from "$ALLOW" to "$LISTEN" port 11434 proto tcp comment "ollama: finance VM only"
  ufw deny in to "$LISTEN" port 11434 proto tcp comment "ollama: nobody else"
  ufw status numbered | grep 11434
else
  echo "ufw is not active: restrict 11434 to $ALLOW with your firewall, or with a Tailscale ACL" >&2
fi

echo "== check"
for i in $(seq 1 30); do curl -fsS "http://$LISTEN:11434/api/version" && break; sleep 1; done
echo
journalctl -u ollama -n 30 --no-pager | grep -iE "inference compute|cuda|no compatible GPUs" || true
