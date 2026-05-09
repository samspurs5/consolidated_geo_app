#!/usr/bin/env bash
# Download the JS/CSS dependencies the frontend needs (MapLibre GL + pmtiles).
# Run this once on a connected machine. The files land in
# api/app/static/vendor/ and are bundled into the api Docker image, so
# the frontend renders fully offline thereafter.
#
# Usage:
#   ./scripts/fetch-frontend-vendor.sh

set -euo pipefail

VENDOR_DIR="$(cd "$(dirname "$0")/.." && pwd)/api/app/static/vendor"
mkdir -p "$VENDOR_DIR"

# Pinned versions — bump deliberately.
MAPLIBRE_VERSION="4.7.1"
PMTILES_VERSION="3.2.1"

declare -A FILES=(
    ["maplibre-gl.js"]="https://unpkg.com/maplibre-gl@${MAPLIBRE_VERSION}/dist/maplibre-gl.js"
    ["maplibre-gl.css"]="https://unpkg.com/maplibre-gl@${MAPLIBRE_VERSION}/dist/maplibre-gl.css"
    ["pmtiles.js"]="https://unpkg.com/pmtiles@${PMTILES_VERSION}/dist/pmtiles.js"
)

for name in "${!FILES[@]}"; do
    url="${FILES[$name]}"
    out="$VENDOR_DIR/$name"
    echo "Fetching $name"
    curl -L --fail --silent --show-error -o "$out" "$url"
    echo "  -> $out ($(du -h "$out" | cut -f1))"
done

echo
echo "Done. Vendor files in $VENDOR_DIR"
echo "Rebuild the api image to bundle them: docker compose build api"
