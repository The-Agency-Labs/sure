#!/bin/sh
# Headed Chrome needs a display; Xvfb gives it one inside the container.
set -eu
mkdir -p "$HOME"
exec xvfb-run -a --server-args="-screen 0 1366x900x24 -nolisten tcp" python -m ingest "$@"
