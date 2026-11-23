#!/bin/bash
# Sign in to a bank yourself, in the VM's own browser profile for that bank.
# Use it for a new bank's first sign-in, or when a nightly run stops on a
# reCAPTCHA. Run it in your own terminal (the screen password prints here):
#   ssh -t <vm> /root/sure/repo/ingest/deploy/ingest/attend.sh amex
# Then open the printed link in your browser, sign in, tick "Remember me",
# and close Chrome (its X, top right) when done.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh
BANK=${1:?usage: attend.sh <bank> [url]}
URL=${2:-$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(next(s['url'] for s in d['login'] if s['action']=='goto'))" "$STACK_DIR/repo/ingest/banks/$BANK.json")}
BIND=$(sed -n 's/^MFA_BIND=//p' .env)
HOST=$(python3 -c "import json,sys; from urllib.parse import urlsplit; print(urlsplit(json.load(open(sys.argv[1]))['mfa']['public_base']).hostname or '')" "$INGEST_CONFIG" 2>/dev/null || true)
PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 8)  # x11vnc uses 8 chars

if systemctl is-active -q "ingest-scrape-$BANK" 2>/dev/null; then echo "a $BANK run is in progress; wait for it" >&2; exit 1; fi
echo
echo "  Open:      http://${HOST:-$BIND}:6080/vnc.html?autoconnect=1&resize=scale"
echo "  Password:  $PASS"
echo
echo "  Sign in, answer any reCAPTCHA or code, tick 'Remember me',"
echo "  then close Chrome (X, top right). This ends by itself."
echo
docker compose run --rm --no-deps -p "${BIND:-127.0.0.1}:6080:6080" -e VNC_PASSWORD="$PASS" \
  --entrypoint /usr/local/bin/scraper-attend scraper "$BANK" "$URL" 2>&1 | grep -v -e '^ Container' -e 'Creating' || true
