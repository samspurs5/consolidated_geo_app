"""Overpass endpoints – thin proxy to the local Overpass API."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..services import overpass as op_svc

router = APIRouter()


class QueryRequest(BaseModel):
    query: str          # Overpass QL (without [out:…]; prefix)
    output: str = "json"


def _http(request: Request):
    return request.app.state.http


@router.post("/query")
async def run_query(body: QueryRequest, request: Request):
    """
    Execute an Overpass QL query against the local Overpass instance.

    Example body:
    ```json
    {
      "query": "node[amenity=restaurant](48.8,2.3,48.9,2.4);out body;",
      "output": "json"
    }
    ```
    """
    try:
        return await op_svc.query(_http(request), body.query, body.output)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/status")
async def overpass_status(request: Request):
    """Return raw Overpass API status text."""
    try:
        return {"status": await op_svc.status(_http(request))}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
