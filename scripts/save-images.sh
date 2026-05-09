#!/usr/bin/env bash
# Save all stack images to a single gzipped tarball for air-gapped transfer.
#
# Run this on a CONNECTED machine after `docker compose pull` and
# `docker compose build`. The resulting archive can be moved to an
# air-gapped host and loaded with scripts/load-images.sh.
#
# Usage:
#   ./scripts/save-images.sh [output-file]
# Default output: ./geo-stack-images.tar.gz

set -euo pipefail

OUT="${1:-geo-stack-images.tar.gz}"

# Image names must stay in sync with docker-compose.yml.
# The api image tag follows the compose project name (directory name) by
# default: <project>-api:latest. Override with API_IMAGE if your project
# name differs (`docker compose images api` to confirm).
API_IMAGE="${API_IMAGE:-consolidated_geo_app-api:latest}"

IMAGES=(
    wiktorn/overpass-api:latest
    israelhikingmap/graphhopper:latest
    "$API_IMAGE"
)

echo "Verifying images are present locally..."
for img in "${IMAGES[@]}"; do
    if ! docker image inspect "$img" >/dev/null 2>&1; then
        echo "ERROR: image not found locally: $img"
        echo "       Run 'docker compose pull && docker compose build' first."
        exit 1
    fi
done

echo "Saving to: $OUT"
docker save "${IMAGES[@]}" | gzip > "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo "Done. Archive size: $SIZE"
echo
echo "Transfer to the air-gapped host along with:"
echo "  - this repository"
echo "  - data/osm/region.pbf"
echo "Then run: ./scripts/load-images.sh $OUT"
