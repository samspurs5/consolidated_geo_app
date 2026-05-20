"""Live data feed: WebSocket fanout + snapshot endpoint."""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services import live as live_svc

router = APIRouter()


@router.websocket("/ws")
async def live_ws(ws: WebSocket) -> None:
    """Stream live feature updates to the browser.

    On connect: a {type: snapshot, features: [...]} dump of the current cache.
    Then: {type: upsert, feature: {...}} for each subsequent broker message."""
    await ws.accept()
    await live_svc.cache.add_client(ws)
    try:
        # We don't expect client messages; the receive loop just detects
        # disconnects.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        live_svc.cache.remove_client(ws)


@router.get("/features", tags=["live"])
async def list_features():
    """Return the current cache as a flat list (also used by the LLM tool)."""
    return {"features": await live_svc.cache.snapshot()}
