#!/bin/bash
# Run the host-side ingest tools (repair, due, check) with this stack's paths.
#   ingest.sh repair --queue      ingest.sh due --today 2026-12-01
set -euo pipefail
. "$(dirname "$0")/env.sh"
exec "$INGEST_BIN" "$@"
