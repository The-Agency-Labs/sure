#!/bin/bash
# Scrape one bank with that bank's logins decrypted into RAM for this run only.
#   scrape.sh <bank> [--interactive]
# Decrypts $STACK_DIR/config/credentials/<bank>.*.cred (systemd-creds) into a fresh
# directory under /dev/shm, mounts it read-only into the scraper container, and
# deletes it when the run ends, however it ends.
set -euo pipefail
BANK=${1:?usage: scrape.sh <bank> [--interactive]}; shift
[[ $BANK =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo "bad bank name: $BANK" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run as root (decrypting needs the VM's credential key)" >&2; exit 1; }
cd "$(dirname "$0")"
. ./env.sh
CREDS=$CREDENTIALS_DIR
SCRAPER_UID=${SCRAPER_UID:-2001}

shopt -s nullglob
files=("$CREDS/$BANK".*.cred)
[ ${#files[@]} -gt 0 ] || { echo "no logins for $BANK: add them with set-credential.sh $BANK username (and password)" >&2; exit 2; }

tmp=$(mktemp -d /dev/shm/ingest-creds.XXXXXX)
trap 'rm -rf "$tmp"' EXIT
for f in "${files[@]}"; do
  name=$(basename "$f" .cred)
  systemd-creds decrypt --name="$name" "$f" "$tmp/$name"
done
chown -R "$SCRAPER_UID" "$tmp"
chmod 0400 "$tmp"/*
chmod 0500 "$tmp"
docker compose run --rm --no-deps -v "$tmp:/run/ingest-creds:ro" scraper scrape "$BANK" "$@"
