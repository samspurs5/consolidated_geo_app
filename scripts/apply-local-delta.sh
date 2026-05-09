#!/usr/bin/env bash
# Apply one or more local .osc or .osc.gz delta files to the source-of-truth PBF
# without any network access (fully offline).
#
# Usage:
#   ./scripts/apply-local-delta.sh changes1.osc.gz [changes2.osc.gz ...]
#
# Requires osmium-tool on the host, OR run from inside the api container:
#   docker exec geo-api bash -c "osmium apply-changes /data/osm/region.pbf \
#       /data/osm/updates/changes.osc.gz -o /data/osm/region.pbf.new --overwrite \
#       && mv /data/osm/region.pbf.new /data/osm/region.pbf"

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <change-file.osc.gz> [<change-file2.osc.gz> ...]"
    exit 1
fi

PBF="$(dirname "$0")/../data/osm/region.pbf"
TMP="${PBF}.applying"

if [ ! -f "$PBF" ]; then
    echo "ERROR: PBF not found at $PBF"
    exit 1
fi

echo "▶ Applying ${#@} delta file(s) to $PBF …"
osmium apply-changes "$PBF" "$@" -o "$TMP" --overwrite --progress
mv "$TMP" "$PBF"
echo "✓ PBF updated."

echo ""
echo "To reload both services:"
echo "  curl -X POST http://localhost:8000/data/reload"
