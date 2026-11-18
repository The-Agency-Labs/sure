#!/bin/bash
# Finance VM setup. Run as root on Ubuntu Server 26.04 after Docker (from
# Docker's apt repo) is installed and $STACK_DIR/repo holds the repo checkout.
# Everything for this stack lives under STACK_DIR; the host's finance share is
# mounted at FINANCE_DIR (layout: deploy/ingest/env.sh). Safe to re-run.
#   STACK_DIR=~/sure FINANCE_DIR=~/mnt/finance vm-setup.sh   (these are the defaults)
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
command -v docker >/dev/null || { echo "install Docker from Docker's apt repo first" >&2; exit 1; }

FINANCE_GID=2000   # group: can read raw files
SCRAPER_UID=2001   # writes raw/, owns browser profiles
LOADER_UID=2002    # reads raw/, writes normalized/, holds the Sure API key
STACK_DIR=${STACK_DIR:-/root/sure}
FINANCE_DIR=${FINANCE_DIR:-/root/mnt/finance}
REPO=$STACK_DIR/repo
[ -d "$REPO/ingest" ] || { echo "no checkout at $REPO (see README, Setup)" >&2; exit 1; }

echo "== users and group"
getent group finance >/dev/null || groupadd --system --gid "$FINANCE_GID" finance
# Fixed UIDs above the system range so they match on the host (virtiofs); no
# login shell, no home, no password.
id ingest-scraper >/dev/null 2>&1 || useradd --uid "$SCRAPER_UID" --gid finance --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ingest-scraper
id ingest-loader  >/dev/null 2>&1 || useradd --uid "$LOADER_UID"  --gid finance --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ingest-loader

echo "== virtiofs share at $FINANCE_DIR"
mkdir -p "$FINANCE_DIR"
if ! grep -qE "^finance-data\s+$FINANCE_DIR\s" /etc/fstab; then
  echo "finance-data $FINANCE_DIR virtiofs defaults,nofail 0 0" >> /etc/fstab
  systemctl daemon-reload
fi
mountpoint -q "$FINANCE_DIR" || mount "$FINANCE_DIR"
findmnt -t virtiofs "$FINANCE_DIR" >/dev/null || { echo "$FINANCE_DIR is not a virtiofs mount; check the Proxmox directory mapping 'finance-data'" >&2; exit 1; }

# These live on the host's finance dataset. virtiofs passes UIDs through, so
# this is the same as deploy/host/host-finance-perms.sh on the host.
install -d -o "$SCRAPER_UID" -g finance -m 2750 "$FINANCE_DIR/raw"
install -d -o "$LOADER_UID"  -g finance -m 0750 "$FINANCE_DIR/normalized"
install -d -o root -g root -m 0700 "$FINANCE_DIR/sure-backups"

echo "== $STACK_DIR: config, secrets and state (never on the share, never backed up off the VM)"
# Root-only at the top; the containers reach their parts through bind mounts.
install -d -o root -g root -m 0700 "$STACK_DIR" "$STACK_DIR/secrets"
install -d -o "$SCRAPER_UID" -g "$SCRAPER_UID" -m 0700 "$STACK_DIR/secrets/scraper" "$STACK_DIR/secrets/scraper/profiles"
install -d -o "$LOADER_UID"  -g "$LOADER_UID"  -m 0700 "$STACK_DIR/secrets/loader"
install -d -o root -g root -m 0755 "$STACK_DIR/state"
install -d -o "$SCRAPER_UID" -g "$SCRAPER_UID" -m 0700 "$STACK_DIR/state/bundles"
install -d -o "$LOADER_UID"  -g finance -m 0750 "$STACK_DIR/state/review"
install -d -o root -g root -m 0700 "$STACK_DIR/state/repair-queue" "$STACK_DIR/state/last-success"
[ -e "$STACK_DIR/secrets/loader/sure_api_key" ] || install -o "$LOADER_UID" -g "$LOADER_UID" -m 0400 /dev/null "$STACK_DIR/secrets/loader/sure_api_key"

echo "== config"
install -d -o root -g finance -m 0750 "$STACK_DIR/config"
# Bank logins, encrypted with this VM's systemd-creds key (deploy/ingest/set-credential.sh).
install -d -o root -g root -m 0700 "$STACK_DIR/config/credentials"
[ -e "$STACK_DIR/config/ingest.json" ] || install -o root -g finance -m 0640 "$REPO/ingest/deploy/ingest/ingest.example.json" "$STACK_DIR/config/ingest.json"
[ -e "$STACK_DIR/config/banks.list" ]  || printf '# one bank per line, scraped in this order; see `ingest due --help`\n# simplii\n# sunlife every=14d anchor=<a payday, YYYY-MM-DD> offset=2d\n' > "$STACK_DIR/config/banks.list"
if [ ! -e "$REPO/ingest/deploy/ingest/.env" ]; then
  install -m 0600 "$REPO/ingest/deploy/ingest/.env.example" "$REPO/ingest/deploy/ingest/.env"
  sed -i "s|^STACK_DIR=.*|STACK_DIR=$STACK_DIR|; s|^FINANCE_DIR=.*|FINANCE_DIR=$FINANCE_DIR|" "$REPO/ingest/deploy/ingest/.env"
fi

echo "== Sure ($STACK_DIR/compose.yml)"
[ -e "$STACK_DIR/compose.yml" ] || install -m 0600 "$REPO/ingest/deploy/sure/compose.yml" "$STACK_DIR/compose.yml"
if [ ! -e "$STACK_DIR/.env" ]; then
  install -m 0600 "$REPO/ingest/deploy/sure/.env.example" "$STACK_DIR/.env"
  sed -i "s|^BACKUP_DIR=.*|BACKUP_DIR=$FINANCE_DIR/sure-backups|" "$STACK_DIR/.env"
fi

echo "== AppArmor profile for the scraper (Chrome sandbox needs user namespaces)"
install -m 0644 "$REPO/ingest/deploy/ingest/apparmor-ingest-scraper" /etc/apparmor.d/ingest-scraper
apparmor_parser -r -W /etc/apparmor.d/ingest-scraper

echo "== Docker networks"
docker network inspect sure_net    >/dev/null 2>&1 || docker network create sure_net
docker network inspect scraper_net >/dev/null 2>&1 || docker network create scraper_net

echo "== Docker starts after Tailscale (Sure binds the tailnet IP only)"
install -d -m 0755 /etc/systemd/system/docker.service.d
install -m 0644 "$REPO/ingest/deploy/systemd/docker.service.d/10-after-tailscale.conf" /etc/systemd/system/docker.service.d/
TS_IP=$(tailscale ip -4 2>/dev/null || true)
[ -n "$TS_IP" ] && echo "   this VM's tailnet IP: $TS_IP  (use it for SURE_BIND_ADDR in $STACK_DIR/.env)" \
  || echo "   tailscale has no IPv4 address yet: bring it up, then set SURE_BIND_ADDR" >&2

echo "== systemd timer (installed, not enabled until setup is finished)"
sed -e "s|@STACK_DIR@|$STACK_DIR|g" -e "s|@FINANCE_DIR@|$FINANCE_DIR|g" \
  "$REPO/ingest/deploy/systemd/ingest-nightly.service" > /etc/systemd/system/ingest-nightly.service
chmod 0644 /etc/systemd/system/ingest-nightly.service
install -m 0644 "$REPO/ingest/deploy/systemd/ingest-nightly.timer" /etc/systemd/system/
systemctl daemon-reload

cat <<NEXT

Done. Still to do by hand (see ingest/README.md, "Setup"):
  1. Fill $STACK_DIR/.env, then: cd $STACK_DIR && docker compose up -d && docker compose --profile backup up -d backup
  2. Create a Sure API key (read_write); write it to $STACK_DIR/secrets/loader/sure_api_key
  3. Bank logins: deploy/ingest/set-credential.sh <bank> username (and password)
  4. Edit $STACK_DIR/config/ingest.json and $STACK_DIR/config/banks.list
  5. cd $REPO/ingest/deploy/ingest && docker compose build
  6. Repair model on a GPU machine: deploy/repair-host/install-ollama.sh (see README)
  7. First sign-in per bank by hand: deploy/ingest/scrape.sh <bank> --interactive
  8. systemctl enable --now ingest-nightly.timer
NEXT
