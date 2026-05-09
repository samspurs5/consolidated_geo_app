"""Thin async proxy to the GraphHopper HTTP API."""
from typing import Any

import httpx

from ..config import settings


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> Any:
    r = await client.get(f"{settings.graphhopper_url}{path}", params=params)
    r.raise_for_status()
    return r.json()


async def route(
    client: httpx.AsyncClient,
    points: list[tuple[float, float]],
    profile: str = "car",
    locale: str = "en",
    instructions: bool = True,
    calc_points: bool = True,
    points_encoded: bool = False,
) -> dict:
    params: dict[str, Any] = {
        "point": [f"{lat},{lon}" for lat, lon in points],
        "profile": profile,
        "locale": locale,
        "instructions": str(instructions).lower(),
        "calc_points": str(calc_points).lower(),
        "points_encoded": str(points_encoded).lower(),
        "type": "json",
    }
    return await _get(client, "/route", params)


async def isochrone(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    profile: str = "car",
    time_limit: int = 600,
    distance_limit: int = -1,
    buckets: int = 1,
) -> dict:
    params: dict[str, Any] = {
        "point": f"{lat},{lon}",
        "profile": profile,
        "time_limit": time_limit,
        "distance_limit": distance_limit,
        "buckets": buckets,
        "type": "json",
    }
    return await _get(client, "/isochrone", params)


async def matrix(
    client: httpx.AsyncClient,
    from_points: list[tuple[float, float]],
    to_points: list[tuple[float, float]],
    profile: str = "car",
) -> dict:
    params: dict[str, Any] = {
        "from_point": [f"{lat},{lon}" for lat, lon in from_points],
        "to_point":   [f"{lat},{lon}" for lat, lon in to_points],
        "profile":    profile,
        "out_array":  ["weights", "times", "distances"],
        "type":       "json",
    }
    return await _get(client, "/matrix", params)


async def info(client: httpx.AsyncClient) -> dict:
    return await _get(client, "/info", {})
