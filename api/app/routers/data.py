"""
Data management endpoints.

All endpoints operate on the single source-of-truth PBF at /data/osm/region.pbf.

Offline workflow
----------------
1.  Copy a .pbf into ./data/osm/ on the host then POST /data/import-local
    with {"src": "/data/osm/my-region.pbf"}.
2.  POST /data/reload to rebuild both services.
3.  For incremental updates, place .osc/.osc.gz files into ./data/osm/updates/
    and call POST /data/apply-delta.

Online workflow
---------------
1.  POST /data/download with {"url": "https://download.geofabrik.de/…pbf"}.
2.  POST /data/reload.
3.  For incremental network updates: POST /data/fetch-update.
"""

import tempfile
from pathlib import Path

import aiofiles
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services import osm_data as data_svc

router = APIRouter()


def _http(request: Request):
    return request.app.state.http


# ── status ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def data_status():
    """Return info about the current PBF file (size, timestamp, replication state)."""
    return await data_svc.get_status()


# ── loading ───────────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    url: str


@router.post("/download")
async def download_pbf(body: DownloadRequest, request: Request):
    """
    Stream-download a PBF from *url* and save it as the source of truth.

    Works with any HTTP/HTTPS URL, including a local file server
    (e.g. http://192.168.1.10/region.pbf) for air-gapped deployments.
    Does NOT restart services – call POST /data/reload afterwards.
    """
    try:
        return await data_svc.download_pbf(body.url, _http(request))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ImportLocalRequest(BaseModel):
    src: str  # absolute path inside the container, e.g. /data/osm/my-region.pbf


@router.post("/import-local")
async def import_local(body: ImportLocalRequest):
    """
    Copy a pre-existing PBF from *src* (a path inside the container) into
    place as the source of truth.  Useful when the file is already on a
    mounted volume.  Does NOT restart services.
    """
    try:
        return await data_svc.import_local_pbf(body.src)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload")
async def upload_pbf(file: UploadFile = File(...)):
    """
    Upload a PBF file directly via multipart form-data.
    Intended for small extracts (< few GB).  For large files use
    POST /data/download with a local HTTP URL instead.
    Does NOT restart services.
    """
    from ..config import settings
    tmp = settings.pbf_path.with_suffix(".pbf.upload")
    try:
        async with aiofiles.open(tmp, "wb") as f:
            while chunk := await file.read(65536):
                await f.write(chunk)
        tmp.replace(settings.pbf_path)
        return {"uploaded": str(settings.pbf_path), "size_bytes": settings.pbf_path.stat().st_size}
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── delta updates ─────────────────────────────────────────────────────────────

@router.post("/apply-delta")
async def apply_delta(files: list[UploadFile] = File(...)):
    """
    Upload one or more .osc or .osc.gz delta files and apply them to the PBF.

    This is the primary offline update mechanism.  Generate delta files with:
        osmium derive-changes old.pbf new.pbf -o changes.osc.gz

    Does NOT restart services – call POST /data/reload afterwards.
    """
    from ..config import settings
    settings.updates_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    try:
        for f in files:
            dest = settings.updates_dir / f.filename
            async with aiofiles.open(dest, "wb") as out:
                while chunk := await f.read(65536):
                    await out.write(chunk)
            saved.append(dest)

        result = await data_svc.apply_delta_files(saved)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Clean up uploaded deltas after applying
        for p in saved:
            p.unlink(missing_ok=True)


@router.post("/fetch-update")
async def fetch_update():
    """
    Pull the latest diffs from the configured REPLICATION_URL and apply them.

    Requires network access to the replication server.
    For air-gapped deployments use POST /data/apply-delta instead.
    Does NOT restart services – call POST /data/reload afterwards.
    """
    try:
        return await data_svc.fetch_and_apply_replication_diffs()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── service reload ────────────────────────────────────────────────────────────

@router.post("/reload")
async def reload_all():
    """
    Restart both GraphHopper and Overpass so they reload from the updated PBF.

    GraphHopper rebuilds its routing graph; Overpass reinitialises its DB.
    Both operations are asynchronous – use GET /health on each service to
    confirm they are back up before routing.
    """
    try:
        return await data_svc.reload_all()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reload/graphhopper")
async def reload_graphhopper():
    """Restart only GraphHopper."""
    try:
        return await data_svc.reload_graphhopper()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reload/overpass")
async def reload_overpass():
    """
    Stop Overpass, clear its database, and restart it so it re-imports
    the source-of-truth PBF.
    """
    try:
        return await data_svc.reload_overpass()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
