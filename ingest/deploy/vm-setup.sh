#!/bin/bash
# Finance VM setup. Run as root on Ubuntu Server 26.04 after Docker (from
# Docker's apt repo) is installed and /opt/ingest holds the repo checkout.
# Safe to re-run.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
command -v docker >/dev/null || { echo "install Docker from Docker's apt repo first" >&2; exit 1; }

FINANCE_GID=2000   # group: can read raw files
SCRAPER_UID=2001   # writes raw/, owns browser profiles + vault credentials
LOADER_UID=2002    # reads raw/, writes normalized/, holds the Sure API key
REPO=/opt/ingest

echo "== users and group"
getent group finance >/dev/null || groupadd --system --gid "$FINANCE_GID" finance
id ingest-scraper >/dev/null 2>&1 || useradd --system --uid "$SCRAPER_UID" --gid finance --no-create-home --shell /usr/sbin/nologin ingest-scraper
id ingest-loader  >/dev/null 2>&1 || useradd --system --uid "$LOADER_UID"  --gid finance --no-create-home --shell /usr/sbin/nologin ingest-loader

echo "== virtiofs share at /srv/finance"
mkdir -p /srv/finance
if ! grep -qE '^finance-data\s+/srv/finance\s' /etc/fstab; then
  echo 'finance-data /srv/finance virtiofs defaults,nofail 0 0' >> /etc/fstab
  systemctl daemon-reload
fi
mountpoint -q /srv/finance || mount /srv/finance
findmnt -t virtiofs /srv/finance >/dev/null || { echo "/srv/finance is not a virtiofs mount; check the Proxmox directory mapping 'finance-data'" >&2; exit 1; }

# These live on naspool. virtiofs passes UIDs through, so this is the same as
# deploy/host/cassio-finance-perms.sh on the host; either side works.
install -d -o "$SCRAPER_UID" -g finance -m 2750 /srv/finance/raw
install -d -o "$LOADER_UID"  -g finance -m 0750 /srv/finance/normalized
install -d -o root -g root -m 0700 /srv/finance/sure-backups

echo "== VM-local secrets and state (never on the share, never backed up off the VM)"
install -d -o "$SCRAPER_UID" -g "$SCRAPER_UID" -m 0700 /srv/ingest-secrets /srv/ingest-secrets/bw /srv/ingest-secrets/profiles
install -d -o "$LOADER_UID"  -g "$LOADER_UID"  -m 0700 /srv/loader-secrets
install -d -o root -g root -m 0755 /srv/ingest-state
install -d -o "$SCRAPER_UID" -g "$SCRAPER_UID" -m 0700 /srv/ingest-state/bundles
install -d -o "$LOADER_UID"  -g finance -m 0750 /srv/ingest-state/review
install -d -o root -g root -m 0700 /srv/ingest-state/repair-queue
[ -e /srv/loader-secrets/sure_api_key ] || install -o "$LOADER_UID" -g "$LOADER_UID" -m 0400 /dev/null /srv/loader-secrets/sure_api_key
for f in bw_server bw_clientid bw_clientsecret bw_password; do
  [ -e "/srv/ingest-secrets/bw/$f" ] || install -o "$SCRAPER_UID" -g "$SCRAPER_UID" -m 0600 /dev/null "/srv/ingest-secrets/bw/$f"
done

echo "== config"
install -d -o root -g finance -m 0750 /etc/ingest
[ -e /etc/ingest/ingest.json ] || install -o root -g finance -m 0640 "$REPO/ingest/deploy/ingest/ingest.example.json" /etc/ingest/ingest.json
[ -e /etc/ingest/banks.list ]  || printf '# one bank per line, scraped in this order\n# simplii\n' > /etc/ingest/banks.list
[ -e "$REPO/ingest/deploy/ingest/.env" ] || install -m 0600 "$REPO/ingest/deploy/ingest/.env.example" "$REPO/ingest/deploy/ingest/.env"

echo "== Sure"
install -d -o root -g root -m 0700 /opt/sure
[ -e /opt/sure/compose.yml ] || install -m 0600 "$REPO/ingest/deploy/sure/compose.yml" /opt/sure/compose.yml
[ -e /opt/sure/.env ] || install -m 0600 "$REPO/ingest/deploy/sure/.env.example" /opt/sure/.env

echo "== Docker networks"
docker network inspect sure_net    >/dev/null 2>&1 || docker network create sure_net
docker network inspect scraper_net >/dev/null 2>&1 || docker network create scraper_net

echo "== systemd timer"
install -m 0644 "$REPO/ingest/deploy/systemd/ingest-nightly.service" /etc/systemd/system/
install -m 0644 "$REPO/ingest/deploy/systemd/ingest-nightly.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable ingest-nightly.timer

cat <<'NEXT'

Done. Still to do by hand (see ingest/README.md, "Setup"):
  1. Fill /opt/sure/.env, then: cd /opt/sure && docker compose up -d && docker compose --profile backup up -d backup
  2. Create a Sure API key (read_write); write it to /srv/loader-secrets/sure_api_key
  3. Fill /srv/ingest-secrets/bw/* (dedicated bank-logins vault account)
  4. Edit /etc/ingest/ingest.json and /etc/ingest/banks.list; set MFA_BIND in /opt/ingest/ingest/deploy/ingest/.env
  5. cd /opt/ingest/ingest/deploy/ingest && docker compose build --build-arg BW_VERSION=... --build-arg BW_SHA256=...
  6. systemctl start ingest-nightly.timer
NEXT
