#!/usr/bin/env bash
# Build a self-contained vector-tiles file from data/osm/region.pbf using
# planetiler. The output is a single .pmtiles file the frontend reads
# directly via HTTP range requests — no tile server needed.
#
# Usage:
#   ./scripts/build-tiles.sh
#
# Notes:
#   * Schema: openmaptiles (planetiler's default)
#   * Output: data/tiles/region.pmtiles
#   * Memory: planetiler scales with input size. For larger regions, set
#     JAVA_OPTS to give it more heap, e.g. JAVA_OPTS="-Xmx8g"
#   * The Java image is pulled once on a connected machine; from then on
#     this is a pure local-file operation (no network during build).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PBF="$REPO_ROOT/data/osm/region.pbf"
OUT_DIR="$REPO_ROOT/data/tiles"
OUT_FILE="$OUT_DIR/region.pmtiles"

if [ ! -f "$PBF" ]; then
    echo "ERROR: PBF not found at $PBF"
    echo "       Run ./scripts/download-extract.sh <url> first."
    exit 1
fi

mkdir -p "$OUT_DIR"

JAVA_OPTS="${JAVA_OPTS:--Xmx2g}"
PLANETILER_IMAGE="${PLANETILER_IMAGE:-ghcr.io/onthegomap/planetiler:latest}"

echo "Building vector tiles..."
echo "  input : $PBF"
echo "  output: $OUT_FILE"
echo "  image : $PLANETILER_IMAGE"
echo "  heap  : $JAVA_OPTS"

# MSYS_NO_PATHCONV=1 stops Git Bash on Windows from rewriting the Unix-style
# /data/... arguments below into C:/Program Files/Git/data/... before they
# reach docker. Harmless no-op on Linux/macOS.
#
# --download fetches the three small auxiliary datasets the openmaptiles
# schema needs (lake_centerlines, water-polygons, natural_earth, ~600 MB
# total) into data/sources/ on first run. Subsequent runs reuse the cache.
MSYS_NO_PATHCONV=1 docker run --rm \
    -e JAVA_TOOL_OPTIONS="$JAVA_OPTS" \
    -v "$REPO_ROOT/data:/data" \
    "$PLANETILER_IMAGE" \
    --osm-path=/data/osm/region.pbf \
    --output=/data/tiles/region.pmtiles \
    --download \
    --force

SIZE=$(du -h "$OUT_FILE" | cut -f1)
echo "Done. $OUT_FILE ($SIZE)"
echo
echo "Restart the api container so it picks up the new tiles:"
echo "  docker compose restart api"
echo "Then open http://localhost:8000/ui/"
