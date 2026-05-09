"""Routing endpoints – thin proxy to GraphHopper."""
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..services import graphhopper as gh_svc

router = APIRouter()


class RouteRequest(BaseModel):
    points: list[tuple[float, float]]  # [[lat, lon], ...]
    profile: Literal["car", "foot", "bike"] = "car"
    locale: str = "en"
    instructions: bool = True
    calc_points: bool = True
    points_encoded: bool = False


class IsochroneRequest(BaseModel):
    lat: float
    lon: float
    profile: Literal["car", "foot", "bike"] = "car"
    time_limit: int = 600        # seconds
    distance_limit: int = -1     # metres; -1 = unused
    buckets: int = 1


class MatrixRequest(BaseModel):
    from_points: list[tuple[float, float]]
    to_points: list[tuple[float, float]]
    profile: Literal["car", "foot", "bike"] = "car"


def _http(request: Request):
    return request.app.state.http


@router.post("")
async def compute_route(body: RouteRequest, request: Request):
    """Compute a route between two or more points."""
    try:
        return await gh_svc.route(
            _http(request),
            body.points,
            profile=body.profile,
            locale=body.locale,
            instructions=body.instructions,
            calc_points=body.calc_points,
            points_encoded=body.points_encoded,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/isochrone")
async def compute_isochrone(body: IsochroneRequest, request: Request):
    """Return an isochrone polygon (reachable area) from a point."""
    try:
        return await gh_svc.isochrone(
            _http(request),
            body.lat, body.lon,
            profile=body.profile,
            time_limit=body.time_limit,
            distance_limit=body.distance_limit,
            buckets=body.buckets,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/matrix")
async def compute_matrix(body: MatrixRequest, request: Request):
    """Compute a distance/time matrix between sets of points."""
    try:
        return await gh_svc.matrix(
            _http(request),
            body.from_points,
            body.to_points,
            profile=body.profile,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/info")
async def server_info(request: Request):
    """Return GraphHopper server info (loaded profiles, bbox, etc.)."""
    try:
        return await gh_svc.info(_http(request))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
