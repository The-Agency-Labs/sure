#!/bin/bash
# Store one bank login field, encrypted with this VM's credential key.
#   set-credential.sh <bank> <field>     e.g. set-credential.sh simplii password
# Bank scripts refer to it as "<bank>/<field>". Run it again to change a value.
# Asks for the value without echoing it; it never appears in arguments or logs.
set -euo pipefail
BANK=${1:?usage: set-credential.sh <bank> <field>}
FIELD=${2:?usage: set-credential.sh <bank> <field>  (username, password, ...)}
re='^[a-z0-9][a-z0-9_-]*$'
[[ $BANK =~ $re && $FIELD =~ $re ]] || { echo "names: lowercase letters, digits, - and _" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -t 0 ] || { echo "needs a terminal to type into: run it in an ssh -t session" >&2; exit 1; }
CREDS=${INGEST_CREDENTIALS:-/etc/ingest/credentials}
install -d -m 0700 "$CREDS"

read -rsp "$BANK/$FIELD: " value; echo
[ -n "$value" ] || { echo "empty, nothing saved" >&2; exit 1; }
umask 077
printf %s "$value" | systemd-creds encrypt --name="$BANK.$FIELD" - "$CREDS/$BANK.$FIELD.cred"
unset value
echo "saved $CREDS/$BANK.$FIELD.cred"
