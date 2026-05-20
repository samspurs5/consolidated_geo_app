"""MQTT bridge + in-memory live-feature cache + WebSocket fanout.

Architecture:

    External provider ─┐
    Built-in adapter ──┤  publish to  live/<provider>/<id>   ──▶  Mosquitto
                       ┘                                              │
                                                                      │ subscribe live/#
                                                                      ▼
                                                                  this module
                                                                   /        \\
                                                              cache         broadcast
                                                              (LLM)         (WebSocket)

Messages on `live/<provider>/<id>` are JSON of shape:

    {
      "id": "wolf-42",
      "provider": "movebank",
      "lat": 43.7396,
      "lon": 7.4283,
      "timestamp": "2026-05-10T12:34:56Z",
      "properties": {"species": "Canis lupus", ...}
    }

`id` and `provider` are inferred from the topic when missing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiomqtt
from fastapi import WebSocket

from ..config import settings

log = logging.getLogger(__name__)


class LiveCache:
    """Latest-wins cache of live features, keyed by (provider, id).

    Held in memory by the api process and refreshed continuously by the
    MQTT bridge task. Doubles as a fan-out hub to WebSocket clients."""

    def __init__(self, max_age_s: int = 3600) -> None:
        self._features: dict[tuple[str, str], dict] = {}
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.max_age_s = max_age_s

    async def upsert(self, feature: dict) -> None:
        provider = feature.get("provider")
        fid = feature.get("id")
        if not provider or fid is None:
            return
        feature["_received_ts"] = time.time()
        async with self._lock:
            self._features[(str(provider), str(fid))] = feature
        await self._broadcast({"type": "upsert", "feature": feature})

    async def snapshot(self) -> list[dict]:
        cutoff = time.time() - self.max_age_s
        async with self._lock:
            self._features = {
                k: v for k, v in self._features.items()
                if v.get("_received_ts", 0) >= cutoff
            }
            return list(self._features.values())

    async def add_client(self, ws: WebSocket) -> None:
        snap = await self.snapshot()
        await ws.send_json({"type": "snapshot", "features": snap})
        self._clients.add(ws)

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def _broadcast(self, msg: dict) -> None:
        if not self._clients:
            return
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001 — any send failure: drop the client
                dead.append(ws)
        for ws in dead:
            self.remove_client(ws)


cache = LiveCache(max_age_s=settings.live_max_age_s)


async def mqtt_loop() -> None:
    """Background task: subscribe to live/# and feed parsed messages into the cache.

    Auto-reconnects on broker failure with a fixed 5 s backoff."""
    while True:
        try:
            async with aiomqtt.Client(
                hostname=settings.mqtt_host, port=settings.mqtt_port
            ) as client:
                await client.subscribe("live/#")
                log.info("MQTT bridge connected to %s:%d, subscribed to live/#",
                         settings.mqtt_host, settings.mqtt_port)
                async for message in client.messages:
                    await _ingest(message.topic, message.payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("MQTT bridge disconnected (%s). Reconnecting in 5 s.", e)
            await asyncio.sleep(5)


async def _ingest(topic: Any, payload: bytes) -> None:
    try:
        msg = json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001
        log.warning("dropping unparseable message on %s", topic)
        return
    # Topic shape: live/<provider>/<id>
    parts = str(topic).split("/", 2)
    if len(parts) >= 3:
        msg.setdefault("provider", parts[1])
        msg.setdefault("id", parts[2])
    if "lat" not in msg or "lon" not in msg:
        return
    await cache.upsert(msg)
