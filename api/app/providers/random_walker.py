"""Tiny demo provider — emits 3 randomly-walking points around a centre
every 3 s. Useful for smoke-testing the live pipeline without an
external data source.

Enable with PROVIDERS=random_walker. Optional centre via
WALKER_LAT and WALKER_LON env vars (defaults near Monaco).
"""
from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime, timezone

import aiomqtt

from .base import Provider


class RandomWalker(Provider):
    name = "random_walker"

    def __init__(
        self,
        mqtt_host: str,
        mqtt_port: int,
        *,
        lat: float = 43.74,
        lon: float = 7.42,
        count: int = 3,
        step: float = 0.0005,
    ) -> None:
        super().__init__(mqtt_host, mqtt_port)
        self.lat = lat
        self.lon = lon
        self.count = count
        self.step = step

    async def loop(self, client: aiomqtt.Client) -> None:
        positions = {
            f"walker-{i}": [
                self.lat + random.uniform(-self.step * 5, self.step * 5),
                self.lon + random.uniform(-self.step * 5, self.step * 5),
            ]
            for i in range(self.count)
        }
        while True:
            for fid, pos in positions.items():
                pos[0] += random.uniform(-self.step, self.step)
                pos[1] += random.uniform(-self.step, self.step)
                await self.publish(client, {
                    "id": fid,
                    "provider": self.name,
                    "lat": pos[0],
                    "lon": pos[1],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "properties": {"kind": "demo"},
                })
            await asyncio.sleep(3)


def from_env(mqtt_host: str, mqtt_port: int) -> "RandomWalker":
    return RandomWalker(
        mqtt_host, mqtt_port,
        lat=float(os.getenv("WALKER_LAT") or 43.74),
        lon=float(os.getenv("WALKER_LON") or 7.42),
    )
