#!/bin/sh
# Headed Chrome needs a display; Xvfb gives it one inside the container.
set -eu
mkdir -p "$HOME"
exec xvfb-run -a --server-args="-screen 0 1920x1080x24 -nolisten tcp" python -m ingest "$@"
