"""
OSM data management service.

Responsibilities
----------------
* Report status of the local PBF file (size, mtime, osmium fileinfo).
* Stream-download a replacement PBF from a URL (works offline with a
  local HTTP server or a mounted file share).
* Import a pre-downloaded PBF file by its local path.
* Apply one or more .osc / .osc.gz delta files using `osmium apply-changes`.
* Pull the latest diffs from a replication server (online only) using
  `pyosmium-up-to-date`, which reads/writes a state.txt alongside the PBF.
* Coordinate container restarts so GraphHopper and Overpass reload from
  the updated PBF (requires /var/run/docker.sock to be mounted).
"""

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
import docker
import docker.errors
import httpx

try:
    import osmium  # noqa: F401 – used only if pyosmium is installed
    _OSMIUM_AVAILABLE = True
except ImportError:
    _OSMIUM_AVAILABLE = False

from ..config import settings

log = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

async def _run(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(), err.decode()


def _docker_client() -> docker.DockerClient:
    return docker.from_env()


# ── public API ───────────────────────────────────────────────────────────────

async def get_status() -> dict:
    pbf = settings.pbf_path
    if not pbf.exists():
        return {"pbf_exists": False, "pbf_path": str(pbf)}

    stat = pbf.stat()
    info: dict = {
        "pbf_exists": True,
        "pbf_path": str(pbf),
        "pbf_size_bytes": stat.st_size,
        "pbf_mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }

    # osmium fileinfo -e gives timestamp + replication header (best-effort)
    try:
        code, out, _ = await _run("osmium", "fileinfo", "-e", str(pbf))
        if code == 0:
            for line in out.splitlines():
                if "Timestamp:" in line:
                    info["osm_timestamp"] = line.split(":", 1)[1].strip()
                if "Replication URL:" in line:
                    info["replication_url"] = line.split(":", 1)[1].strip()
                if "Sequence number:" in line:
                    info["sequence_number"] = line.split(":", 1)[1].strip()
    except FileNotFoundError:
        info["osmium_available"] = False

    if settings.state_path.exists():
        info["state_file"] = settings.state_path.read_text()

    return info


async def download_pbf(url: str, http_client: httpx.AsyncClient) -> dict:
    """
    Stream-download a PBF from *url* into data/osm/region.pbf.

    Works with any HTTP/HTTPS URL, including a local server
    (http://192.168.x.x/region.pbf) for air-gapped deployments.
    """
    tmp = settings.pbf_path.with_suffix(".pbf.tmp")
    log.info("Downloading PBF from %s → %s", url, tmp)

    async with http_client.stream("GET", url, follow_redirects=True, timeout=None) as r:
        r.raise_for_status()
        async with aiofiles.open(tmp, "wb") as f:
            async for chunk in r.aiter_bytes(65536):
                await f.write(chunk)

    tmp.replace(settings.pbf_path)
    log.info("PBF download complete: %s", settings.pbf_path)
    return {"downloaded": str(settings.pbf_path)}


async def import_local_pbf(src: str) -> dict:
    """
    Copy (or rename) a local PBF file into place as the source of truth.
    *src* must be an absolute path visible inside the api container.
    """
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(src)
    shutil.copy2(src_path, settings.pbf_path)
    log.info("Imported local PBF %s → %s", src_path, settings.pbf_path)
    return {"imported": str(settings.pbf_path)}


async def apply_delta_files(delta_paths: list[Path]) -> dict:
    """
    Apply one or more local .osc or .osc.gz change files to the PBF in-place
    using `osmium apply-changes`.  Works fully offline.
    """
    if not settings.pbf_path.exists():
        raise FileNotFoundError(f"Source PBF not found: {settings.pbf_path}")

    tmp = settings.pbf_path.with_suffix(".pbf.applying")
    cmd = [
        "osmium", "apply-changes",
        str(settings.pbf_path),
        *[str(p) for p in delta_paths],
        "-o", str(tmp),
        "--overwrite",
        "--progress",
    ]
    log.info("Running: %s", " ".join(cmd))
    try:
        code, out, err = await _run(*cmd)
    except FileNotFoundError:
        raise RuntimeError(
            "osmium-tool is not installed. "
            "Inside Docker this is available; outside Docker install osmium-tool."
        )
    if code != 0:
        raise RuntimeError(f"osmium apply-changes failed (exit {code}):\n{err}")

    tmp.replace(settings.pbf_path)
    log.info("Delta applied; PBF updated: %s", settings.pbf_path)
    return {"applied_deltas": [str(p) for p in delta_paths], "stdout": out, "stderr": err}


async def fetch_and_apply_replication_diffs() -> dict:
    """
    Fetch latest diffs from a replication server and apply them.
    Requires REPLICATION_URL to be set and network access to that server.

    Uses pyosmium-up-to-date which reads/writes a state.txt alongside the PBF
    so incremental state is preserved across calls.
    """
    if not settings.replication_url:
        raise ValueError(
            "REPLICATION_URL is not set. "
            "For offline updates supply delta files via POST /data/apply-delta."
        )
    if not settings.pbf_path.exists():
        raise FileNotFoundError(f"Source PBF not found: {settings.pbf_path}")

    cmd = [
        "pyosmium-up-to-date",
        "-v",
        "-r", settings.replication_url,
        "-s", str(settings.state_path),
        str(settings.pbf_path),
    ]
    log.info("Running: %s", " ".join(cmd))
    try:
        code, out, err = await _run(*cmd)
    except FileNotFoundError:
        raise RuntimeError(
            "pyosmium-up-to-date is not installed. "
            "Inside Docker this is available via the pyosmium package."
        )
    if code != 0:
        raise RuntimeError(f"pyosmium-up-to-date failed (exit {code}):\n{err}")
    return {"stdout": out, "stderr": err}


async def reload_graphhopper() -> dict:
    """Restart the GraphHopper container so it rebuilds the graph from the updated PBF."""
    try:
        dc = _docker_client()
        container = dc.containers.get(settings.graphhopper_container)
        log.info("Restarting GraphHopper container: %s", container.name)
        container.restart(timeout=10)
        return {"restarted": settings.graphhopper_container}
    except docker.errors.NotFound:
        raise RuntimeError(f"Container not found: {settings.graphhopper_container}")


async def reload_overpass() -> dict:
    """
    Restart the Overpass container and force it to re-import the PBF.

    Strategy:
      1. Stop the container.
      2. Remove the overpass-db volume so the DB is recreated from the PBF.
      3. Start the container (OVERPASS_MODE=init will re-run).
    """
    try:
        dc = _docker_client()
        container = dc.containers.get(settings.overpass_container)
        log.info("Stopping Overpass container: %s", container.name)
        container.stop(timeout=30)

        try:
            vol = dc.volumes.get(settings.overpass_db_volume)
            log.info("Removing Overpass DB volume: %s", vol.name)
            vol.remove(force=True)
        except docker.errors.NotFound:
            log.warning("Overpass DB volume not found, skipping removal.")

        log.info("Starting Overpass container: %s", container.name)
        container.start()
        return {"restarted": settings.overpass_container, "db_cleared": True}
    except docker.errors.NotFound:
        raise RuntimeError(f"Container not found: {settings.overpass_container}")


async def reload_all() -> dict:
    gh = await reload_graphhopper()
    op = await reload_overpass()
    return {"graphhopper": gh, "overpass": op}
