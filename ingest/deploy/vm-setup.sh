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
# Fixed UIDs above the system range so they match on the host (virtiofs); no
# login shell, no home, no password.
id ingest-scraper >/dev/null 2>&1 || useradd --uid "$SCRAPER_UID" --gid finance --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ingest-scraper
id ingest-loader  >/dev/null 2>&1 || useradd --uid "$LOADER_UID"  --gid finance --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ingest-loader

echo "== virtiofs share at /srv/finance"
mkdir -p /srv/finance
if ! grep -qE '^finance-data\s+/srv/finance\s' /etc/fstab; then
  echo 'finance-data /srv/finance virtiofs defaults,nofail 0 0' >> /etc/fstab
  systemctl daemon-reload
fi
mountpoint -q /srv/finance || mount /srv/finance
findmnt -t virtiofs /srv/finance >/dev/null || { echo "/srv/finance is not a virtiofs mount; check the Proxmox directory mapping 'finance-data'" >&2; exit 1; }

# These live on the host's finance dataset. virtiofs passes UIDs through, so
# this is the same as deploy/host/host-finance-perms.sh on the host.
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

echo "== AppArmor profile for the scraper (Chrome sandbox needs user namespaces)"
install -m 0644 "$REPO/ingest/deploy/ingest/apparmor-ingest-scraper" /etc/apparmor.d/ingest-scraper
apparmor_parser -r -W /etc/apparmor.d/ingest-scraper

echo "== repair model storage (Ollama on this VM)"
install -d -o root -g root -m 0700 /srv/ollama

echo "== Docker networks"
docker network inspect sure_net    >/dev/null 2>&1 || docker network create sure_net
docker network inspect scraper_net >/dev/null 2>&1 || docker network create scraper_net

echo "== Docker starts after Tailscale (Sure binds the tailnet IP only)"
install -d -m 0755 /etc/systemd/system/docker.service.d
install -m 0644 "$REPO/ingest/deploy/systemd/docker.service.d/10-after-tailscale.conf" /etc/systemd/system/docker.service.d/
TS_IP=$(tailscale ip -4 2>/dev/null || true)
[ -n "$TS_IP" ] && echo "   this VM's tailnet IP: $TS_IP  (use it for SURE_BIND_ADDR in /opt/sure/.env)" \
  || echo "   tailscale has no IPv4 address yet: bring it up, then set SURE_BIND_ADDR" >&2

echo "== systemd timer (installed, not enabled until setup is finished)"
install -m 0644 "$REPO/ingest/deploy/systemd/ingest-nightly.service" /etc/systemd/system/
install -m 0644 "$REPO/ingest/deploy/systemd/ingest-nightly.timer" /etc/systemd/system/
systemctl daemon-reload

cat <<'NEXT'

Done. Still to do by hand (see ingest/README.md, "Setup"):
  1. Fill /opt/sure/.env (SURE_BIND_ADDR = the tailnet IP above), then: cd /opt/sure && docker compose up -d && docker compose --profile backup up -d backup
  2. Create a Sure API key (read_write); write it to /srv/loader-secrets/sure_api_key
  3. Fill /srv/ingest-secrets/bw/* (dedicated bank-logins vault account)
  4. Edit /etc/ingest/ingest.json and /etc/ingest/banks.list
  5. cd /opt/ingest/ingest/deploy/ingest && docker compose build --build-arg BW_VERSION=... --build-arg BW_SHA256=...
  6. Repair model: docker compose --profile repair up -d ollama && docker compose exec ollama ollama pull qwen2.5:3b
  7. First sign-in per bank by hand: docker compose run --rm scraper scrape <bank> --interactive
  8. systemctl enable --now ingest-nightly.timer
NEXT
