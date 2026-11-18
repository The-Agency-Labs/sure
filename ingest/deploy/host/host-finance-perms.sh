#!/bin/bash
# Run on the Proxmox host as root, once the finance dataset exists.
# virtiofs passes UIDs/GIDs through, so the dataset must be owned by the
# finance VM's service UIDs (same values as in deploy/ingest/.env).
#   host-finance-perms.sh <zfs dataset>        e.g. tank/finance
set -euo pipefail
DATASET=${1:?usage: $0 <zfs dataset backing the finance share>}
FINANCE_GID=${FINANCE_GID:-2000}
SCRAPER_UID=${SCRAPER_UID:-2001}
LOADER_UID=${LOADER_UID:-2002}

BASE=$(zfs get -H -o value mountpoint "$DATASET")
[ -d "$BASE" ] || { echo "$DATASET is not mounted ($BASE)" >&2; exit 1; }
mkdir -p "$BASE/raw" "$BASE/normalized" "$BASE/sure-backups"
chown "$SCRAPER_UID:$FINANCE_GID" "$BASE/raw";            chmod 2750 "$BASE/raw"
chown -R "$LOADER_UID:$FINANCE_GID" "$BASE/normalized";   chmod 0750 "$BASE/normalized"
chown 0:0 "$BASE/sure-backups";                           chmod 0700 "$BASE/sure-backups"
chown 0:0 "$BASE";                                        chmod 0755 "$BASE"
ls -ln "$BASE"
