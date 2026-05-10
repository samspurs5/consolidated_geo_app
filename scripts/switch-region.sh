#!/usr/bin/env bash
# Switch the loaded OSM region by replacing data/osm/region.pbf and
# rebuilding all derived state (graph cache, Overpass DB, optional tiles).
#
# Usage:
#   ./scripts/switch-region.sh <source>
#
# <source> is one of:
#   * a full URL to a .pbf
#       https://download.geofabrik.de/europe/monaco-latest.osm.pbf
#   * a local .pbf path
#       /path/to/region.pbf
#   * a Geofabrik path (no scheme, no -latest.osm.pbf suffix)
#       europe/monaco
#       europe/great-britain/england/greater-manchester
#
# Flags (env vars):
#   REBUILD_TILES=1   also regenerate data/tiles/region.pmtiles via planetiler
#   KEEP_OLD=1        save the current PBF as data/osm/region-previous.pbf
#   HEALTH_TIMEOUT=N  override the default 600 s wait for services to come up

set -euo pipefail

cd "$(dirname "$0")/.."

INPUT="${1:?Usage: $0 <url|local-path|geofabrik-shorthand>}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-600}"

# ── 0. Resolve the source ──────────────────────────────────────────────
if [ -f "$INPUT" ]; then
    SRC_KIND="local"
    SRC="$INPUT"
elif [[ "$INPUT" == http://* || "$INPUT" == https://* ]]; then
    SRC_KIND="url"
    SRC="$INPUT"
else
    SRC_KIND="url"
    SRC="https://download.geofabrik.de/${INPUT}-latest.osm.pbf"
fi

PBF=data/osm/region.pbf
PREV=data/osm/region-previous.pbf
STATE=data/osm/state.txt

echo "Switching to: $SRC"
echo

# ── 1. Stop services so we can wipe derived state ──────────────────────
echo "[1/6] Stopping services and removing the Overpass DB volume..."
# `down` removes containers + network but preserves named volumes; we then
# explicitly drop overpass-db so it gets re-imported from the new PBF.
docker compose down >/dev/null 2>&1 || true
docker volume rm consolidated_geo_app_overpass-db >/dev/null 2>&1 || true

# ── 2. Rotate / remove old PBF ─────────────────────────────────────────
if [ -f "$PBF" ]; then
    if [ "${KEEP_OLD:-0}" = "1" ]; then
        mv "$PBF" "$PREV"
        echo "[2/6] Saved previous PBF to $PREV"
    else
        rm -f "$PBF"
        echo "[2/6] Removed previous PBF"
    fi
fi
rm -f "$STATE"  # belongs to the previous region

# ── 3. Fetch / copy the new PBF ────────────────────────────────────────
mkdir -p data/osm
if [ "$SRC_KIND" = "local" ]; then
    echo "[3/6] Copying $SRC -> $PBF"
    cp "$SRC" "$PBF"
else
    echo "[3/6] Downloading $SRC"
    curl -fL --progress-bar -o "$PBF" "$SRC"
    # Companion state.txt for incremental updates (Geofabrik convention).
    STATE_URL="${SRC/latest.osm.pbf/updates\/state.txt}"
    if [ "$STATE_URL" != "$SRC" ]; then
        if curl -fL --silent -o "$STATE" "$STATE_URL"; then
            echo "       state.txt fetched (incremental updates available)"
        else
            rm -f "$STATE"
        fi
    fi
fi
echo "       $PBF ($(du -h "$PBF" | cut -f1))"

# ── 4. Wipe the GraphHopper graph cache ────────────────────────────────
echo "[4/6] Wiping GraphHopper graph cache..."
find data/default-gh -mindepth 1 ! -name '.gitkeep' -delete 2>/dev/null || true

# ── 5. Optionally rebuild vector tiles ────────────────────────────────
if [ "${REBUILD_TILES:-0}" = "1" ]; then
    echo "[5/6] Rebuilding vector tiles via planetiler..."
    ./scripts/build-tiles.sh
else
    echo "[5/6] Skipping tile rebuild (set REBUILD_TILES=1 to regenerate)"
    echo "       Old tiles will display until you rerun ./scripts/build-tiles.sh"
fi

# ── 6. Bring services back up and wait for healthy ────────────────────
echo "[6/6] Starting services and waiting up to ${HEALTH_TIMEOUT}s for healthy..."
docker compose up -d >/dev/null

deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while :; do
    op=$(docker inspect -f '{{.State.Health.Status}}' overpass 2>/dev/null || echo starting)
    gh=$(docker inspect -f '{{.State.Health.Status}}' graphhopper 2>/dev/null || echo starting)
    if [ "$op" = "healthy" ] && [ "$gh" = "healthy" ]; then
        echo "       overpass=healthy graphhopper=healthy"
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo
        echo "ERROR: services did not reach healthy in ${HEALTH_TIMEOUT}s"
        echo "       overpass=$op graphhopper=$gh"
        echo "       Tail the logs:  docker compose logs -f overpass graphhopper"
        exit 1
    fi
    printf '       overpass=%-10s graphhopper=%-10s\r' "$op" "$gh"
    sleep 5
done

echo
echo "Region switched. Verify:"
echo "  curl -s http://localhost:8989/info | python3 -c 'import sys,json; d=json.load(sys.stdin); print(\"bbox\", d.get(\"bbox\"))'"
echo "  open http://localhost:8000/ui/"
