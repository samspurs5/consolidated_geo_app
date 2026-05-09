#!/usr/bin/env bash
# Download an OSM extract (Geofabrik or any PBF URL) and place it in ./data/osm/.
# Also fetches the companion state.txt so incremental updates work.
#
# Usage:
#   ./scripts/download-extract.sh <URL>
#
# Examples (online):
#   ./scripts/download-extract.sh https://download.geofabrik.de/europe/monaco-latest.osm.pbf
#   ./scripts/download-extract.sh https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf
#
# For air-gapped setups, serve the .pbf via any HTTP server and run:
#   ./scripts/download-extract.sh http://192.168.1.10/my-region.pbf

set -euo pipefail

URL="${1:?Usage: $0 <pbf-url>}"
DEST_DIR="$(dirname "$0")/../data/osm"
mkdir -p "$DEST_DIR"

PBF_FILE="$DEST_DIR/region.pbf"

echo "▶ Downloading PBF from: $URL"
curl -L --progress-bar -o "$PBF_FILE" "$URL"
echo "✓ Saved to: $PBF_FILE"

# Try to download the companion state.txt (Geofabrik convention)
# state.txt lives at <base-url>/../<region>-updates/state.txt
STATE_URL="${URL/latest.osm.pbf/updates\/state.txt}"
if [ "$STATE_URL" != "$URL" ]; then
    echo "▶ Attempting to download state.txt from: $STATE_URL"
    if curl -L --fail --silent -o "$DEST_DIR/state.txt" "$STATE_URL"; then
        echo "✓ state.txt saved – incremental updates enabled."
    else
        echo "⚠ state.txt not found – only full re-imports will be available."
    fi
fi

echo ""
echo "Next steps:"
echo "  1. docker compose up -d"
echo "  2. Wait for services to initialise, then test:"
echo "     curl http://localhost:8000/health"
echo "     curl http://localhost:8000/data/status"
