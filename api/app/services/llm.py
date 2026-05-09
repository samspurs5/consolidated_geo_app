"""Chat service: drives Ollama with tool-calling, dispatching to local
GraphHopper / Overpass services. Tool definitions follow the OpenAI/MCP
JSON-schema convention so the same tools can be exposed externally via
fastapi-mcp at /mcp."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from ollama import AsyncClient

from ..config import settings
from . import graphhopper as gh_svc
from . import overpass as op_svc

log = logging.getLogger(__name__)


def _http_error_msg(e: httpx.HTTPStatusError) -> str:
    """Pull the most informative line out of a 4xx/5xx response so the LLM
    can see *why* a tool call failed (and retry sensibly)."""
    resp = e.response
    if resp is None:
        return str(e)
    try:
        body = resp.json()
        if isinstance(body, dict):
            for key in ("message", "error", "hints"):
                if key in body and body[key]:
                    return f"HTTP {resp.status_code}: {body[key]}"
    except Exception:  # noqa: BLE001
        pass
    text = (resp.text or "").strip()[:500]
    return f"HTTP {resp.status_code}: {text or '(empty body)'}"


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "route",
            "description": "Compute a route between two geographic points. Returns distance (m) and travel time (ms).",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_lat": {"type": "number"},
                    "from_lon": {"type": "number"},
                    "to_lat":   {"type": "number"},
                    "to_lon":   {"type": "number"},
                    "profile":  {"type": "string", "enum": ["car", "foot", "bike"]},
                },
                "required": ["from_lat", "from_lon", "to_lat", "to_lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "isochrone",
            "description": "Compute the area reachable from a point within a time limit (in minutes).",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":     {"type": "number"},
                    "lon":     {"type": "number"},
                    "minutes": {"type": "number"},
                    "profile": {"type": "string", "enum": ["car", "foot", "bike"]},
                },
                "required": ["lat", "lon", "minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "overpass_query",
            "description": (
                "Run an Overpass QL query against local OSM data. Use this to find features by tag in a "
                "bounding box. Provide the QL body without a [out:json]; prefix. "
                "Example: node[amenity=restaurant](43.7,7.4,43.8,7.5);out body;"
            ),
            "parameters": {
                "type": "object",
                "properties": {"ql": {"type": "string"}},
                "required": ["ql"],
            },
        },
    },
]


SYSTEM_PROMPT = (
    "You are a concise geographic assistant for a self-hosted OSM stack.\n"
    "You have three tools: route, isochrone, overpass_query.\n"
    "When the user asks about places, use overpass_query with the bounding box of the current map view.\n"
    "When asked for a route, call route. When asked what's reachable, call isochrone.\n"
    "Keep replies short — the map renders results visually."
)


async def _dispatch(name: str, args: dict, http: httpx.AsyncClient) -> tuple[dict, dict | None]:
    """Execute a tool. Returns (summary_for_llm, overlay_for_frontend or None)."""
    if name == "route":
        result = await gh_svc.route(
            http,
            [(args["from_lat"], args["from_lon"]), (args["to_lat"], args["to_lon"])],
            profile=args.get("profile", "car"),
        )
        path = (result.get("paths") or [{}])[0]
        summary = {"distance_m": path.get("distance"), "time_ms": path.get("time")}
        overlay = {"kind": "route", "geojson": {"type": "Feature", "geometry": path.get("points")}}
        return summary, overlay

    if name == "isochrone":
        result = await gh_svc.isochrone(
            http,
            args["lat"], args["lon"],
            profile=args.get("profile", "car"),
            time_limit=int(float(args["minutes"]) * 60),
        )
        polys = result.get("polygons") or []
        summary = {"polygons": len(polys), "minutes": args["minutes"]}
        overlay = {"kind": "iso", "geojson": {"type": "FeatureCollection", "features": polys}}
        return summary, overlay

    if name == "overpass_query":
        result = await op_svc.query(http, args["ql"])
        elements = result.get("elements", []) if isinstance(result, dict) else []
        summary = {
            "count": len(elements),
            "sample": [
                {"type": el.get("type"), "id": el.get("id"), "tags": el.get("tags") or {}}
                for el in elements[:5]
            ],
        }
        features = []
        for el in elements:
            tags = el.get("tags") or {}
            if el.get("type") == "node" and el.get("lat") is not None:
                features.append({
                    "type": "Feature", "properties": tags,
                    "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                })
            elif el.get("geometry"):
                features.append({
                    "type": "Feature", "properties": tags,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[g["lon"], g["lat"]] for g in el["geometry"]],
                    },
                })
        overlay = {"kind": "ovp", "geojson": {"type": "FeatureCollection", "features": features}}
        return summary, overlay

    raise ValueError(f"unknown tool: {name}")


def _to_dict(obj: Any) -> Any:
    """The ollama library returns pydantic objects; normalise to plain dicts."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "_asdict"):
        return obj._asdict()
    return obj


async def chat(
    user_messages: list[dict],
    http: httpx.AsyncClient,
    bbox: list[float] | None = None,
    max_iterations: int = 5,
) -> dict:
    client = AsyncClient(host=settings.ollama_url)

    system = SYSTEM_PROMPT
    if bbox and len(bbox) == 4:
        s, w, n, e = bbox
        system += f"\nCurrent map view (use as bbox in Overpass QL): {s:.5f},{w:.5f},{n:.5f},{e:.5f}"

    # Tell the model what region GraphHopper actually has loaded — its bbox
    # may be much smaller than the user's map view, and routing points
    # outside it return 400. /info is cheap and cached server-side.
    try:
        info = await gh_svc.info(http)
        gh_bbox = info.get("bbox")
        if gh_bbox and len(gh_bbox) == 4:
            mw, ms, me, mn = gh_bbox  # GraphHopper: [minLon, minLat, maxLon, maxLat]
            system += (
                f"\nRouting data is limited to: south={ms:.5f}, west={mw:.5f}, "
                f"north={mn:.5f}, east={me:.5f}. "
                f"All route/isochrone points must fall strictly inside this box."
            )
        profiles = [p.get("name") for p in info.get("profiles", []) if p.get("name")]
        if profiles:
            system += f"\nAvailable routing profiles: {', '.join(profiles)}."
    except Exception:  # noqa: BLE001
        pass

    messages: list[dict] = [{"role": "system", "content": system}, *user_messages]
    overlays: list[dict] = []
    trace: list[dict] = []

    for _ in range(max_iterations):
        resp = await client.chat(model=settings.ollama_model, messages=messages, tools=TOOLS)
        msg = _to_dict(resp.get("message") if isinstance(resp, dict) else resp.message)
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            return {"reply": msg.get("content", ""), "overlays": overlays, "trace": trace}

        messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tool_calls})

        for tc in tool_calls:
            tc = _to_dict(tc)
            fn = _to_dict(tc.get("function", {}))
            name = fn.get("name")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            try:
                summary, overlay = await _dispatch(name, args, http)
                if overlay:
                    overlays.append(overlay)
                content = json.dumps(summary)
                trace.append({"name": name, "arguments": args, "result": summary})
            except httpx.HTTPStatusError as e:
                err = _http_error_msg(e)
                log.warning("tool %s failed: %s", name, err)
                content = json.dumps({"error": err})
                trace.append({"name": name, "arguments": args, "error": err})
            except Exception as e:  # noqa: BLE001
                log.exception("tool %s failed", name)
                content = json.dumps({"error": str(e)})
                trace.append({"name": name, "arguments": args, "error": str(e)})
            messages.append({"role": "tool", "name": name, "content": content})

    return {"reply": "Stopped after the tool-call iteration limit.", "overlays": overlays, "trace": trace}
