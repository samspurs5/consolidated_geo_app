"""Thin async proxy to the Overpass HTTP API."""
import httpx

from ..config import settings


async def query(client: httpx.AsyncClient, ql: str, output: str = "json") -> dict | str:
    """Execute an Overpass QL query. Returns parsed JSON or raw text."""
    data = f"[out:{output}];{ql}"
    r = await client.post(
        f"{settings.overpass_url}/interpreter",
        data={"data": data},
        timeout=120.0,
    )
    r.raise_for_status()
    if output == "json":
        return r.json()
    return r.text


async def status(client: httpx.AsyncClient) -> str:
    r = await client.get(f"{settings.overpass_url}/status", timeout=10.0)
    r.raise_for_status()
    return r.text
