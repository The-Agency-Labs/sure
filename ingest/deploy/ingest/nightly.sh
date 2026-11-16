#!/bin/bash
# Nightly: scrape each bank in /etc/ingest/banks.list (one per line, in order,
# '#' comments allowed), one at a time, then load. A failed bank doesn't stop
# the others, and is never retried in the same night.
set -uo pipefail
cd "$(dirname "$0")"
status=0
while read -r bank; do
  bank="${bank%%#*}"; bank="${bank// /}"
  [ -z "$bank" ] && continue
  echo "== scrape $bank"
  docker compose run --rm --no-deps scraper scrape "$bank" || status=1
done < /etc/ingest/banks.list
echo "== load"
docker compose run --rm --no-deps loader load || status=1
exit $status
