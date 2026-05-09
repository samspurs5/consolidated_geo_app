from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .routers import chat, data, overpass, route


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=60.0)
    settings.updates_dir.mkdir(parents=True, exist_ok=True)
    yield
    await app.state.http.aclose()


app = FastAPI(
    title="OSM Geo API",
    version="1.0.0",
    description=(
        "Unified routing (GraphHopper) and spatial query (Overpass) API "
        "backed by a single OSM PBF source of truth. Supports offline/air-gapped "
        "operation and incremental delta updates."
    ),
    lifespan=lifespan,
)

app.include_router(route.router, prefix="/route", tags=["routing"])
app.include_router(overpass.router, prefix="/overpass", tags=["overpass"])
app.include_router(data.router, prefix="/data", tags=["data"])
app.include_router(chat.router, prefix="/chat", tags=["chat"])

# Expose routing + overpass tools to external MCP clients (e.g. Claude
# Desktop) at /mcp. The /chat endpoint above uses Ollama's native
# tool-calling format internally, so this mount is purely additive.
try:
    from fastapi_mcp import FastApiMCP

    _mcp = FastApiMCP(
        app,
        name="OSM Geo Tools",
        description="Routing, isochrone, and Overpass query against a local OSM PBF.",
        include_tags=["routing", "overpass"],
    )
    _mcp.mount()
except Exception:  # noqa: BLE001  – optional dep, don't crash startup
    pass


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}


@app.get("/api", tags=["meta"])
async def api_root():
    return {
        "endpoints": {
            "routing":  "/route",
            "overpass": "/overpass",
            "data":     "/data",
            "docs":     "/docs",
            "ui":       "/ui/",
        }
    }


# Static frontend (MapLibre + pmtiles). Bundled into the api image at build time.
_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_static_dir), html=True), name="ui")

# Vector tiles file produced by scripts/build-tiles.sh, bind-mounted from the host.
# Served with HTTP range support so MapLibre can read pmtiles directly.
_tiles_dir = Path("/data/tiles")
if _tiles_dir.is_dir():
    app.mount("/tiles", StaticFiles(directory=str(_tiles_dir)), name="tiles")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/ui/")
