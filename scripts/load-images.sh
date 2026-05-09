#!/usr/bin/env bash
# Load stack images from a tarball produced by scripts/save-images.sh.
# Run this on the AIR-GAPPED host before `docker compose up -d`.
#
# Usage:
#   ./scripts/load-images.sh [archive]
# Default archive: ./geo-stack-images.tar.gz

set -euo pipefail

ARCHIVE="${1:-geo-stack-images.tar.gz}"

if [ ! -f "$ARCHIVE" ]; then
    echo "ERROR: archive not found: $ARCHIVE"
    exit 1
fi

echo "Loading images from: $ARCHIVE"
gunzip -c "$ARCHIVE" | docker load

echo
echo "Loaded images:"
docker images --format '  {{.Repository}}:{{.Tag}}  ({{.Size}})' \
    | grep -E '^(  wiktorn/overpass-api|  israelhikingmap/graphhopper|.*-api):' \
    | sort -u

echo
echo "Next: docker compose up -d"
