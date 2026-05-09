"""Chat endpoint — drives Ollama with tool calls into the local geo stack."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..services import llm as llm_svc

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    # [south, west, north, east] — current map view, used as the default bbox
    # for Overpass queries the model emits.
    bbox: list[float] | None = None


@router.post("")
async def chat(body: ChatRequest, request: Request):
    try:
        return await llm_svc.chat(
            [m.model_dump() for m in body.messages],
            request.app.state.http,
            bbox=body.bbox,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))
