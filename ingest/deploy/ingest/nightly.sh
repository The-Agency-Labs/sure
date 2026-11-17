#!/bin/bash
# Nightly: scrape the banks that are due tonight (see `ingest due` and
# /etc/ingest/banks.list: every night by default, or every N days from a payday),
# one at a time, then load. A failed bank doesn't stop the others and is never
# retried the same night; it stays due, so it is tried again the next night.
set -uo pipefail
cd "$(dirname "$0")"
INGEST=${INGEST_BIN:-/opt/ingest-venv/bin/ingest}
LIST=${BANKS_LIST:-/etc/ingest/banks.list}
STATE=${LAST_SUCCESS_DIR:-/var/lib/ingest/last-success}
install -d -m 0700 "$STATE"

if ! due=$("$INGEST" due --list "$LIST" --state "$STATE"); then
  echo "banks list is invalid; nothing scraped" >&2
  exit 1
fi
status=0
for bank in $due; do
  echo "== scrape $bank"
  if ./scrape.sh "$bank"; then
    date +%F > "$STATE/$bank"
  else
    status=1
  fi
done
echo "== load"
docker compose run --rm --no-deps loader load || status=1
exit $status
