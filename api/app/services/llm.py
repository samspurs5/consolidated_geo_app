"""Chat service: drives Ollama with tool-calling, dispatching to local
GraphHopper / Overpass services. Tool definitions follow the OpenAI/MCP
JSON-schema convention so the same tools can be exposed externally via
fastapi-mcp at /mcp."""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

import httpx
from ollama import AsyncClient

from ..config import settings
from . import graphhopper as gh_svc
from . import overpass as op_svc

log = logging.getLogger(__name__)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _greedy_tour(matrix: list[list[float]], start: int = 0) -> list[int]:
    """Nearest-neighbour tour starting at `start`. Returns ordered indices."""
    n = len(matrix)
    visited = {start}
    order = [start]
    while len(order) < n:
        last = order[-1]
        nxt = min(
            (i for i in range(n) if i not in visited),
            key=lambda i: matrix[last][i],
        )
        order.append(nxt)
        visited.add(nxt)
    return order


def _tour_distance(order: list[int], matrix: list[list[float]]) -> float:
    return sum(matrix[order[i]][order[i + 1]] for i in range(len(order) - 1))


def _two_opt(order: list[int], matrix: list[list[float]]) -> list[int]:
    """One pass of 2-opt swap improvement, repeated until no improvement found."""
    best = order[:]
    best_d = _tour_distance(best, matrix)
    improved = True
    while improved:
        improved = False
        for i in range(1, len(best) - 2):
            for j in range(i + 1, len(best) - 1):
                cand = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                d = _tour_distance(cand, matrix)
                if d < best_d:
                    best, best_d = cand, d
                    improved = True
    return best


# Strip a leading [out:...]; settings clause — the proxy already prepends one.
_OUT_PREFIX_RE = re.compile(r"^\s*\[\s*out\s*:[^\]]+\]\s*;\s*", re.IGNORECASE)
# Pull the actual error out of Overpass's HTML 400 response.
_OVERPASS_HTML_ERR_RE = re.compile(r"<strong[^>]*>([^<]+)</strong>\s*:\s*([^<]+)", re.IGNORECASE)


def _sanitize_ql(ql: str, bbox: list[float] | None) -> str:
    """Defensive cleanup of model-emitted Overpass QL:
    - drop a leading [out:...]; clause (the proxy adds its own)
    - substitute {{bbox}} with the active map bbox (Overpass order: s,w,n,e)
    - trim whitespace
    """
    s = ql.strip()
    s = _OUT_PREFIX_RE.sub("", s)
    if bbox and len(bbox) == 4 and "{{bbox}}" in s:
        south, west, north, east = bbox
        s = s.replace("{{bbox}}", f"{south},{west},{north},{east}")
    return s


def _http_error_msg(e: httpx.HTTPStatusError) -> str:
    """Pull the most informative line out of a 4xx/5xx response so the LLM
    can see *why* a tool call failed (and retry sensibly)."""
    resp = e.response
    if resp is None:
        return str(e)
    # JSON-shaped (GraphHopper)
    try:
        body = resp.json()
        if isinstance(body, dict):
            for key in ("message", "error", "hints"):
                if key in body and body[key]:
                    return f"HTTP {resp.status_code}: {body[key]}"
    except Exception:  # noqa: BLE001
        pass
    text = (resp.text or "")
    # HTML-shaped (Overpass) — extract <strong>Error</strong>: ... fragment(s)
    matches = _OVERPASS_HTML_ERR_RE.findall(text)
    if matches:
        joined = "; ".join(f"{label.strip()}: {detail.strip()}" for label, detail in matches[:3])
        return f"HTTP {resp.status_code}: {joined}"
    return f"HTTP {resp.status_code}: {(text.strip() or '(empty body)')[:500]}"


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
    {
        "type": "function",
        "function": {
            "name": "nearest_amenity",
            "description": (
                "Find the closest amenities of a given type to a point, sorted by straight-line "
                "distance. Use for 'where's the nearest pharmacy', 'show 5 cafes near here'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":         {"type": "number"},
                    "lon":         {"type": "number"},
                    "amenity":     {"type": "string", "description": "OSM amenity tag, e.g. cafe, restaurant, pharmacy, atm"},
                    "max_results": {"type": "integer", "description": "default 5"},
                    "radius_m":    {"type": "integer", "description": "search radius in metres, default 2000"},
                },
                "required": ["lat", "lon", "amenity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reachable_pois",
            "description": (
                "Find places of a given amenity type reachable from a point within a time budget. "
                "Combines isochrone + Overpass. Use for 'restaurants within a 10-minute walk', "
                "'cafes I can bike to in 15 minutes'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":     {"type": "number"},
                    "lon":     {"type": "number"},
                    "minutes": {"type": "number"},
                    "amenity": {"type": "string"},
                    "profile": {"type": "string", "enum": ["car", "foot", "bike"]},
                },
                "required": ["lat", "lon", "minutes", "amenity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tour_planner",
            "description": (
                "Plan an optimised multi-stop tour visiting many places of a given amenity type "
                "within the current map view. Returns ordered stops, total distance/time and "
                "the tour route. Use for 'plan a bar crawl', 'walking tour of museums', "
                "'cafe-hopping route'. Requires the chat map view to be set."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "amenity":         {"type": "string", "description": "e.g. bar, cafe, restaurant, museum"},
                    "min_distance_m":  {"type": "number", "description": "lower bound on total tour distance in metres, default 1000"},
                    "profile":         {"type": "string", "enum": ["car", "foot", "bike"]},
                    "max_stops":       {"type": "integer", "description": "cap on number of stops, default 8"},
                },
                "required": ["amenity"],
            },
        },
    },
]


SYSTEM_PROMPT = (
    "You are a concise geographic assistant for a self-hosted OSM stack.\n"
    "\n"
    "Tool selection — pick the SMALLEST tool that answers the question:\n"
    "  * 'route from A to B' → route\n"
    "  * 'reachable area in N minutes' → isochrone\n"
    "  * 'nearest X to a point' → nearest_amenity\n"
    "  * 'X reachable in N minutes from a point' → reachable_pois\n"
    "  * 'plan a tour / crawl / hop through many X' → tour_planner\n"
    "  * Anything else (custom tag combinations, ways, relations) → overpass_query\n"
    "\n"
    "OVERPASS QL RULES (the wrapper rejects malformed queries):\n"
    "  * Provide ONLY the body. Do NOT include [out:json]; — the wrapper prepends it.\n"
    "  * End every statement with a semicolon. End the query with `out body;` (or `out geom;` for ways).\n"
    "  * Use {{bbox}} as a literal placeholder for the current map view; the wrapper substitutes it.\n"
    "  * Example — restaurants in view: `node[amenity=restaurant]({{bbox}});out body;`\n"
    "  * Example — named roads in view: `way[highway][name]({{bbox}});out geom;`\n"
    "\n"
    "Reply briefly — the map renders results visually."
)


async def _dispatch(
    name: str, args: dict, http: httpx.AsyncClient, bbox: list[float] | None = None,
) -> tuple[dict, dict | None]:
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
        args["ql"] = _sanitize_ql(args["ql"], bbox)  # also reflected in the chat trace
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

    if name == "nearest_amenity":
        lat = float(args["lat"])
        lon = float(args["lon"])
        amenity = args["amenity"]
        max_results = int(args.get("max_results") or 5)
        radius_m = int(args.get("radius_m") or 2000)
        ql = f"node[amenity={amenity}](around:{radius_m},{lat},{lon});out body;"
        result = await op_svc.query(http, ql)
        nodes = [
            e for e in (result.get("elements") or [])
            if e.get("type") == "node" and e.get("lat") is not None
        ]
        for n in nodes:
            n["_dist_m"] = _haversine_m(lat, lon, n["lat"], n["lon"])
        nodes.sort(key=lambda n: n["_dist_m"])
        nodes = nodes[:max_results]
        summary = {
            "count": len(nodes),
            "results": [
                {
                    "name": (n.get("tags") or {}).get("name") or "(unnamed)",
                    "lat": n["lat"], "lon": n["lon"],
                    "distance_m": round(n["_dist_m"]),
                    "tags": n.get("tags") or {},
                }
                for n in nodes
            ],
        }
        features = [
            {
                "type": "Feature",
                "properties": {**(n.get("tags") or {}), "rank": idx + 1, "distance_m": round(n["_dist_m"])},
                "geometry": {"type": "Point", "coordinates": [n["lon"], n["lat"]]},
            }
            for idx, n in enumerate(nodes)
        ]
        overlay = {"kind": "ovp", "geojson": {"type": "FeatureCollection", "features": features}}
        return summary, overlay

    if name == "reachable_pois":
        lat = float(args["lat"])
        lon = float(args["lon"])
        minutes = float(args["minutes"])
        amenity = args["amenity"]
        profile = args.get("profile") or "foot"
        iso = await gh_svc.isochrone(
            http, lat, lon, profile=profile, time_limit=int(minutes * 60),
        )
        polys = iso.get("polygons") or []
        if not polys:
            return {"error": "isochrone returned no polygons"}, None
        coords: list[list[float]] = []
        for p in polys:
            for ring in (p.get("geometry") or {}).get("coordinates") or []:
                coords.extend(ring)
        if not coords:
            return {"error": "isochrone polygon empty"}, None
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        bbox_str = f"{min(lats)},{min(lons)},{max(lats)},{max(lons)}"
        ql = f"node[amenity={amenity}]({bbox_str});out body;"
        op_result = await op_svc.query(http, ql)
        nodes = [
            e for e in (op_result.get("elements") or [])
            if e.get("type") == "node" and e.get("lat") is not None
        ]
        for n in nodes:
            n["_dist_m"] = _haversine_m(lat, lon, n["lat"], n["lon"])
        nodes.sort(key=lambda n: n["_dist_m"])
        summary = {
            "amenity": amenity, "minutes": minutes,
            "count": len(nodes),
            "sample": [
                {
                    "name": (n.get("tags") or {}).get("name") or "(unnamed)",
                    "distance_m": round(n["_dist_m"]),
                }
                for n in nodes[:8]
            ],
        }
        # Render the isochrone polygon. Points are described in the summary;
        # the LLM can ask the user to drill in if they want them on the map.
        overlay = {"kind": "iso", "geojson": {"type": "FeatureCollection", "features": polys}}
        return summary, overlay

    if name == "tour_planner":
        amenity = args.get("amenity") or "bar"
        min_distance_m = float(args.get("min_distance_m") or 1000)
        profile = args.get("profile") or "foot"
        max_stops = int(args.get("max_stops") or 8)
        if not bbox or len(bbox) != 4:
            return {"error": "tour_planner needs the chat map view to be set"}, None
        south, west, north, east = bbox
        ql = f"node[amenity={amenity}]({south},{west},{north},{east});out body;"
        op_result = await op_svc.query(http, ql)
        nodes = [
            e for e in (op_result.get("elements") or [])
            if e.get("type") == "node" and e.get("lat") is not None
        ]
        if len(nodes) < 2:
            return {"error": f"only {len(nodes)} {amenity}s in view; widen the map"}, None
        # Prefer named places, then proximity to the bbox centroid; cap the matrix size.
        cx, cy = (west + east) / 2, (south + north) / 2
        named = [n for n in nodes if (n.get("tags") or {}).get("name")]
        unnamed = [n for n in nodes if not (n.get("tags") or {}).get("name")]
        pool = named + unnamed
        pool.sort(key=lambda n: (n["lat"] - cy) ** 2 + (n["lon"] - cx) ** 2)
        pool = pool[: max(2, max_stops)]
        points = [(n["lat"], n["lon"]) for n in pool]
        m = await gh_svc.matrix(http, points, points, profile=profile)
        distances = m.get("distances")
        if not distances:
            return {"error": "matrix returned no distances"}, None
        order = _greedy_tour(distances, start=0)
        order = _two_opt(order, distances)
        # Render the actual route through the chosen sequence
        route_resp = await gh_svc.route(http, [points[i] for i in order], profile=profile)
        path = (route_resp.get("paths") or [{}])[0]
        stops = [
            {
                "name": (pool[i].get("tags") or {}).get("name") or f"{amenity}",
                "lat": pool[i]["lat"], "lon": pool[i]["lon"],
            }
            for i in order
        ]
        summary = {
            "amenity": amenity,
            "stops": len(stops),
            "total_distance_m": round(path.get("distance") or 0),
            "total_time_min": round((path.get("time") or 0) / 60000, 1),
            "min_distance_target_m": min_distance_m,
            "achieved_min_distance": (path.get("distance") or 0) >= min_distance_m,
            "stop_names": [s["name"] for s in stops],
        }
        features: list[dict] = []
        if path.get("points"):
            features.append({
                "type": "Feature", "properties": {"role": "tour"},
                "geometry": path["points"],
            })
        for idx, s in enumerate(stops, start=1):
            features.append({
                "type": "Feature",
                "properties": {"role": "stop", "order": idx, "name": s["name"]},
                "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
            })
        overlay = {"kind": "tour", "geojson": {"type": "FeatureCollection", "features": features}}
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
                summary, overlay = await _dispatch(name, args, http, bbox=bbox)
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
