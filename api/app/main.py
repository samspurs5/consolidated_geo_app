from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .config import settings
from .routers import data, overpass, route


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


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}


@app.get("/", tags=["meta"])
async def root():
    return {
        "endpoints": {
            "routing":  "/route",
            "overpass": "/overpass",
            "data":     "/data",
            "docs":     "/docs",
        }
    }
