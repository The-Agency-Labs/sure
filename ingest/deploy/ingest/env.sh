# Sourced by the deploy scripts: where this stack lives on the VM.
# STACK_DIR and FINANCE_DIR come from deploy/ingest/.env (see .env.example):
#   $STACK_DIR/repo        this repo
#   $STACK_DIR/venv        host tools (repair, due)
#   $STACK_DIR/config      ingest.json, banks.list, credentials/ (encrypted logins)
#   $STACK_DIR/secrets     scraper/ (browser profiles, sensitive.json), loader/ (Sure API key)
#   $STACK_DIR/state       bundles/, review/, repair-queue/, last-success/
#   $STACK_DIR/compose.yml + .env    Sure itself
#   $FINANCE_DIR           the host's finance share: raw/, normalized/, sure-backups/
_ingest_here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_ingest_env() { sed -n "s/^$1=//p" "$_ingest_here/.env" 2>/dev/null | tail -1; }
STACK_DIR=${STACK_DIR:-$(_ingest_env STACK_DIR)}
FINANCE_DIR=${FINANCE_DIR:-$(_ingest_env FINANCE_DIR)}
: "${STACK_DIR:?set STACK_DIR in $_ingest_here/.env}"
: "${FINANCE_DIR:?set FINANCE_DIR in $_ingest_here/.env}"
export STACK_DIR FINANCE_DIR
export INGEST_CONFIG="$STACK_DIR/config/ingest.json"
export CREDENTIALS_DIR="$STACK_DIR/config/credentials"
export STATE_DIR="$STACK_DIR/state"
export SECRETS_DIR="$STACK_DIR/secrets/scraper"
export BANKS_DIR="$STACK_DIR/repo/ingest/banks"
INGEST_BIN="$STACK_DIR/venv/bin/ingest"
