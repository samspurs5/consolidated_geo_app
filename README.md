# consolidated_geo_app

Self-hosted, **offline-first** geo stack:

- **GraphHopper** (routing) — `localhost:8989`
- **Overpass API** (OSM querying) — `localhost/api/interpreter`
- **Ollama** (local LLM, default `qwen2.5:3b`) — internal only
- **FastAPI** unified backend with delta-update orchestration, chat assistant, and an MCP server — `localhost:8000`
- **MapLibre + pmtiles** static frontend served by the FastAPI service — `localhost:8000/ui/`

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

## Frontend (optional)

A minimal MapLibre-based UI is bundled with the api service at
`http://localhost:8000/ui/`. Click two points on the map, pick a profile, hit
**Find route** — it calls `/route` and draws the GeoJSON the GraphHopper
backend returns.

The map itself is rendered client-side from a single `.pmtiles` file built
from the **same `data/osm/region.pbf`** you already have. No tile server
needed.

```bash
# 1. Fetch the JS deps once on a connected machine (MapLibre + pmtiles).
#    They're bundled into the api image at build time.
./scripts/fetch-frontend-vendor.sh

# 2. Build vector tiles from the current PBF (uses planetiler in Docker).
./scripts/build-tiles.sh
# → data/tiles/region.pmtiles

# 3. (Re)start the stack.
docker compose up -d --build api

# 4. Open the UI.
open http://localhost:8000/ui/
```

The pmtiles file is served by FastAPI's static handler with HTTP Range
support, so MapLibre fetches only the tiles in view (no preloading the whole
file). Restart `api` after rebuilding tiles — `data/tiles/` is bind-mounted,
so no rebuild needed for tile changes, just a container restart to clear any
caches.

The included `style.json` is a no-text minimal style (water, landcover,
roads, buildings, boundaries) so it works fully offline without any font or
sprite assets. If you want labels, add a glyph URL pointing at a local font
mirror.

---

## Chat assistant + MCP

The frontend has a small chat panel at the bottom of the side panel. It
sends messages to `/chat`, which drives a local Ollama model with
tool-calling enabled. The model can call three tools backed by the same
APIs you already have:

| Tool             | Backend                                |
| ---------------- | -------------------------------------- |
| `route`          | `gh_svc.route` → GraphHopper           |
| `isochrone`      | `gh_svc.isochrone` → GraphHopper       |
| `overpass_query` | `op_svc.query` → Overpass              |

Each tool returns both a textual summary (so the model can talk about it)
and a GeoJSON overlay (so the frontend draws it on the map). The chat panel
forwards the current map bbox with every request, so the model can build
Overpass queries against what the user is actually looking at.

Default model: **`qwen2.5:3b`** (~2 GB resident). Qwen 2.5 leads the
Berkeley Function Calling Leaderboard at every size class and emits clean
JSON tool arguments with very few hallucinated fields, which matters when
the LLM is wiring up an Overpass query you'll actually execute. The 3B
default is sized for a 16 GB laptop running the full stack + browser; bump
it on bigger boxes:

| Model           | Resident | Notes                                       |
| --------------- | -------- | ------------------------------------------- |
| `qwen2.5:3b`    | ~2 GB    | **default**, fits comfortably on 16 GB      |
| `llama3.2:3b`   | ~2 GB    | solid Meta-tuned alternative                |
| `qwen2.5:7b`    | ~5 GB    | stronger; tight on 16 GB, fine on 32 GB+    |
| `qwen2.5:14b`   | ~9 GB    | needs a workstation                         |

Override via `OLLAMA_MODEL` in `.env`, then
`docker compose up -d ollama` to pull the new tag.

Each chat turn shows a trace of the tool calls the model made — the tool
name, the JSON arguments it passed, and the summary returned to it — so
you can see exactly what the LLM did before forming its reply.

### Memory budget on a 16 GB laptop

Default settings are tuned to leave 6–8 GB for the OS and your browser:

| Component   | Resident          |
| ----------- | ----------------- |
| GraphHopper | ~1.3 GB (`-Xmx1g`) |
| Overpass    | 0.5–2 GB (region size dependent) |
| Ollama + qwen2.5:3b | ~2 GB     |
| FastAPI     | ~150 MB           |
| Docker overhead | ~500 MB       |

```bash
docker compose up -d                   # ollama starts and pulls the model on first boot
./scripts/init-ollama.sh               # optional: re-pull / verify
open http://localhost:8000/ui/         # chat panel is at the bottom of the side panel
```

The first `docker compose up` will spend a few minutes pulling the model
into the `ollama-data` named volume; subsequent boots are instant.

### MCP server

The same three tools are exposed as a [Model Context Protocol](https://modelcontextprotocol.io)
server at `http://localhost:8000/mcp` (SSE). Any MCP-aware client — Claude
Desktop, the `mcp` CLI, custom agents — can connect and use them
identically to the chat panel. Wiring is automatic via
[`fastapi-mcp`](https://github.com/tadata-org/fastapi_mcp); the tools are
filtered to the `routing` and `overpass` tags only.

Example Claude Desktop config snippet:

```json
{
  "mcpServers": {
    "geo": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/mcp"]
    }
  }
}
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
│   ├── default-gh/             # GraphHopper's CH graph cache
│   └── tiles/
│       └── region.pmtiles      # Vector tiles for the frontend (gitignored)
├── graphhopper/
│   └── config.yml              # v12-compatible, SRTM disabled (offline)
├── api/                        # FastAPI: routing + overpass + delta orchestration
│   └── app/static/             # MapLibre frontend (HTML + style + JS deps)
└── scripts/
    ├── download-extract.sh         # Fetch a Geofabrik PBF + state.txt
    ├── apply-local-delta.sh        # Apply .osc files via host osmium
    ├── build-tiles.sh              # Build .pmtiles from region.pbf via planetiler
    ├── fetch-frontend-vendor.sh    # Download MapLibre + pmtiles JS deps
    ├── init-ollama.sh              # Pull / refresh the chat LLM
    ├── save-images.sh              # Bundle images for air-gapped transfer
    └── load-images.sh              # Restore images on the air-gapped host
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

Once the container images are present and `data/osm/region.pbf` is in place,
nothing in this stack reaches the internet:

- GraphHopper elevation downloads are disabled in `graphhopper/config.yml`.
- Overpass diff fetching is disabled (`OVERPASS_DIFF_URL=""`).
- Delta updates are applied from local `.osc` files only via the bundled
  `osmium` (no host install required — see Option B above).

### First install on an air-gapped host

The OSM data path is fully offline, but the **container images** still need to
come from somewhere. Build/pull them once on a connected machine, ship the
tarball, then load on the target.

```bash
# === On a connected machine ===
docker compose pull            # overpass + graphhopper + ollama from Docker Hub
./scripts/fetch-frontend-vendor.sh  # MapLibre + pmtiles JS into api/app/static/vendor/
docker compose build api       # bundles vendor JS into the api image
docker compose up -d ollama    # boot once so the entrypoint pulls the LLM into ollama-data
./scripts/save-images.sh       # → geo-stack-images.tar.gz

# Optional: bundle a PBF + pre-built tiles too (so first boot is instant)
./scripts/download-extract.sh https://download.geofabrik.de/europe/monaco-latest.osm.pbf
./scripts/build-tiles.sh       # → data/tiles/region.pmtiles

# === Transfer to the air-gapped host ===
# - this repository (with api/app/static/vendor/ populated)
# - geo-stack-images.tar.gz
# - data/osm/region.pbf
# - data/tiles/region.pmtiles  (optional, otherwise rebuild on the host)
# - the ollama-data volume contents, if you want chat to work offline:
#     docker run --rm -v consolidated_geo_app_ollama-data:/m -v "$PWD":/o \
#         alpine tar czf /o/ollama-models.tar.gz -C /m .
#   then on the target:
#     docker volume create consolidated_geo_app_ollama-data
#     docker run --rm -v consolidated_geo_app_ollama-data:/m -v "$PWD":/o \
#         alpine tar xzf /o/ollama-models.tar.gz -C /m

# === On the air-gapped host ===
./scripts/load-images.sh       # docker load < geo-stack-images.tar.gz
docker compose up -d           # no network calls, no pulls, no builds
```

After that, every subsequent boot, every routing query, every Overpass query,
and every `.osc` delta application is fully offline.

### Keeping data fresh

To get newer `.osc` files into an air-gapped environment, mirror them from a
Geofabrik update directory (e.g. `monaco-updates/000/...`) onto removable media
or a local HTTP server, and drop them into `data/osm/updates/` before running
`apply-local-delta.sh`.
