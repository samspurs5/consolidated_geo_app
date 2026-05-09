# consolidated_geo_app

Self-hosted, **offline-first** geo stack:

- **GraphHopper** (routing) — `localhost:8989`
- **Overpass API** (OSM querying) — `localhost/api/interpreter`
- **FastAPI** unified frontend with delta-update orchestration — `localhost:8000`

Both data services ingest from a single source of truth — `data/osm/region.pbf` —
and stay in sync via `osmium apply-changes` on local `.osc` / `.osc.gz` files.
No internet is required at runtime once the PBF is in place.

---

## Quick start (validated end-to-end with Monaco)

```bash
# 1. Drop a PBF into data/osm/region.pbf
./scripts/download-extract.sh https://download.geofabrik.de/europe/monaco-latest.osm.pbf

# 2. Bring up the stack
docker compose up -d

# 3. Wait for both services to report healthy
docker ps --format 'table {{.Names}}\t{{.Status}}'
# overpass      Up ... (healthy)
# graphhopper   Up ... (healthy)
```

First-boot ingestion typically takes:

| Region size           | GraphHopper | Overpass |
| --------------------- | ----------- | -------- |
| Monaco (~440 KB PBF)  | ~15 s       | ~10 s    |
| US state              | 5–15 min    | 5–20 min |
| Country               | 30+ min     | 30+ min  |

---

## Verifying ingestion

### GraphHopper — real routing

```bash
curl -s "http://localhost:8989/info" | jq '{version, bbox, profiles}'
# {
#   "version": "12.0",
#   "bbox": [7.406567, 43.72332, 7.439599, 43.751916],
#   "profiles": [{"name": "car"}, {"name": "foot"}, {"name": "bike"}]
# }

curl -s "http://localhost:8989/route?point=43.7384,7.4246&point=43.7297,7.4197&profile=car&points_encoded=false" \
  | jq '.paths[0] | {distance, time}'
# { "distance": 2374.413, "time": 184999 }
```

### Overpass — real OSM queries

```bash
curl -s -G --data-urlencode \
  'data=[out:json][timeout:25];node[amenity=restaurant](43.72,7.40,43.76,7.45);out count;' \
  http://localhost/api/interpreter | jq '.elements[0].tags'
# { "nodes": "90", "ways": "0", "relations": "0", "total": "90" }
```

---

## Offline delta updates

Drop one or more `.osc` / `.osc.gz` files into `data/osm/updates/` and apply them
to the source-of-truth PBF without touching the network.

### Option A — host-side `osmium` (recommended for ad-hoc updates)

```bash
# Requires osmium-tool on the host (apt install osmium-tool)
./scripts/apply-local-delta.sh data/osm/updates/changes.osc.gz

# Restart both services so they pick up the new PBF
docker compose restart graphhopper overpass
```

### Option B — `osmium` via the Overpass image (no host install needed)

The `wiktorn/overpass-api` image already bundles `osmium-tool`, so you can use
it as a transient one-shot:

```bash
docker run --rm -v "$PWD/data/osm:/d" wiktorn/overpass-api:latest \
  osmium apply-changes /d/region.pbf /d/updates/changes.osc.gz \
  -o /d/region.applying.osm.pbf --overwrite

mv data/osm/region.applying.osm.pbf data/osm/region.pbf
docker compose restart graphhopper overpass
```

### Option C — through the FastAPI service

```bash
# Apply a delta file already mounted into the api container
curl -X POST http://localhost:8000/data/apply-delta \
  -F "file=@data/osm/updates/changes.osc.gz"

# Or trigger a coordinated reload after manual changes
curl -X POST http://localhost:8000/data/reload
```

---

## Worked example: synthetic delta

This is the exact flow used to validate the stack end-to-end.

```bash
# 1. Author a hand-crafted change (note: positive node IDs only — negative
#    placeholder IDs are rejected by GraphHopper's PBF parser).
cat > data/osm/updates/test-add-node.osc <<'OSC'
<?xml version="1.0" encoding="UTF-8"?>
<osmChange version="0.6" generator="manual-test">
  <create>
    <node id="9999999999" version="1" timestamp="2024-01-01T00:00:00Z"
          lat="43.7384" lon="7.4246">
      <tag k="amenity" v="bench"/>
      <tag k="name" v="ClaudeTestBench"/>
    </node>
  </create>
</osmChange>
OSC

# 2. Apply it (using the bundled osmium in the overpass image)
docker run --rm -v "$PWD/data/osm:/d" wiktorn/overpass-api:latest \
  osmium apply-changes /d/region.pbf /d/updates/test-add-node.osc \
  -o /d/region.applying.osm.pbf --overwrite
mv data/osm/region.applying.osm.pbf data/osm/region.pbf

# 3. Wipe caches so both services re-ingest the new PBF
rm -rf data/default-gh/* data/default-gh/.??*
docker compose down
docker volume rm consolidated_geo_app_overpass-db
docker compose up -d overpass graphhopper

# 4. Verify the delta took effect
curl -s -G --data-urlencode \
  'data=[out:json];node[name=ClaudeTestBench];out body;' \
  http://localhost/api/interpreter
# elements: [{ "type":"node", "id": 9999999999, "lat": 43.7384, "lon": 7.4246,
#             "tags": {"amenity":"bench", "name":"ClaudeTestBench"} }]
# osm_base timestamp moves to 2024-01-01T00:00:00Z
```

> **Note on cache invalidation.** GraphHopper writes its CH graph to
> `data/default-gh/`. After applying a delta you must clear that directory (or
> mount a fresh one) so the next start re-imports from the updated PBF. Likewise,
> the `overpass-db` named volume holds Overpass's database snapshot; recreate it
> when the underlying PBF changes.

---

## Repository layout

```
.
├── docker-compose.yml          # Three services, one shared PBF volume
├── data/
│   ├── osm/
│   │   ├── region.pbf          # Source of truth (gitignored)
│   │   └── updates/            # Drop .osc / .osc.gz files here
│   └── default-gh/             # GraphHopper's CH graph cache
├── graphhopper/
│   └── config.yml              # v12-compatible, SRTM disabled (offline)
├── api/                        # FastAPI: routing + overpass + delta orchestration
└── scripts/
    ├── download-extract.sh     # Fetch a Geofabrik PBF + state.txt
    └── apply-local-delta.sh    # Apply .osc files via host osmium
```

---

## Why the compose file looks the way it does

A few non-obvious overrides that are required for the stock images to work
together — see `docker-compose.yml` for inline comments.

- **GraphHopper** — the `israelhikingmap/graphhopper` image's default
  entrypoint hardcodes `-c config-example.yml` and never sets the input PBF, so
  startup fails with *"cannot use file for DataReader as it wasn't specified"*.
  We override the entrypoint to also pass `-i /data/osm/region.pbf`, and we
  shadow the bundled config with `graphhopper/config.yml` (which disables SRTM
  elevation downloads — required for offline operation).

- **Overpass** — the `wiktorn/overpass-api` entrypoint always saves
  `OVERPASS_PLANET_URL` to `/db/planet.osm.bz2` *by name*, regardless of actual
  content. Feeding it a `.pbf` produces *"the file is not bzip"*. We use
  `OVERPASS_PLANET_PREPROCESS` to rename it back and re-encode via the bundled
  `osmium cat`. Separately, `/db` is the overpass user's home dir at mode
  `0700`, which the named volume inherits — the `nginx` user that runs
  `fcgiwrap` then can't traverse it to reach the dispatcher socket. The
  entrypoint is wrapped with `chmod 0755 /db` to fix that.

---

## Air-gapped operation

Once `data/osm/region.pbf` is in place, nothing in this stack reaches the
internet:

- GraphHopper elevation downloads are disabled in `graphhopper/config.yml`.
- Overpass diff fetching is disabled (`OVERPASS_DIFF_URL=""`).
- Delta updates are applied from local `.osc` files only.

To get fresh `.osc` files into an air-gapped environment, mirror them from a
Geofabrik update directory (e.g. `monaco-updates/000/...`) onto removable media
or a local HTTP server, and drop them into `data/osm/updates/` before running
`apply-local-delta.sh`.
