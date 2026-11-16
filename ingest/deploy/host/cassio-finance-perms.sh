#!/bin/bash
# Run on cassio (Proxmox host) as root. virtiofs passes UIDs/GIDs through, so
# naspool/finance must be owned by the finance VM's service UIDs.
set -euo pipefail
BASE=${1:-/naspool/finance}
zfs list -H -o name naspool/finance >/dev/null
mkdir -p "$BASE/raw" "$BASE/normalized" "$BASE/sure-backups"
chown 2001:2000 "$BASE/raw";        chmod 2750 "$BASE/raw"         # ingest-scraper:finance
chown -R 2002:2000 "$BASE/normalized"; chmod 0750 "$BASE/normalized" # ingest-loader:finance
chown 0:0 "$BASE/sure-backups";     chmod 0700 "$BASE/sure-backups"
chown 0:0 "$BASE";                  chmod 0755 "$BASE"
ls -ln "$BASE"
