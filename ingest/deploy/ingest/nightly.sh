#!/bin/bash
# Nightly: scrape the banks that are due tonight (see `ingest due` and
# $STACK_DIR/config/banks.list: every night by default, or every N days from a payday),
# one at a time, then load. A failed bank doesn't stop the others and is never
# retried the same night; it stays due, so it is tried again the next night.
# A bank's first run, and any run after a missed night, fetches its full
# history (scrape --full-history) so a gap is filled; otherwise only the
# bank's latest window. CATCHUP_AFTER_DAYS (default 2) sets "missed".
set -uo pipefail
cd "$(dirname "$0")"
. ./env.sh
INGEST=$INGEST_BIN
LIST=$STACK_DIR/config/banks.list
STATE=$STATE_DIR/last-success
install -d -m 0700 "$STATE"

if ! due=$("$INGEST" due --list "$LIST" --state "$STATE"); then
  echo "banks list is invalid; nothing scraped" >&2
  exit 1
fi
CATCHUP_AFTER_DAYS=${CATCHUP_AFTER_DAYS:-2}
today=$(date +%s)
status=0
for bank in $due; do
  history=()
  if [ ! -s "$STATE/$bank" ]; then
    history=(--full-history)
  else
    last=$(date -d "$(cat "$STATE/$bank")" +%s 2>/dev/null || echo 0)
    [ $(( (today - last) / 86400 )) -ge "$CATCHUP_AFTER_DAYS" ] && history=(--full-history)
  fi
  echo "== scrape $bank ${history[*]:-}"
  if ./scrape.sh "$bank" "${history[@]}"; then
    date +%F > "$STATE/$bank"
  else
    status=1
  fi
done
echo "== load"
docker compose run --rm --no-deps loader load || status=1
exit $status
