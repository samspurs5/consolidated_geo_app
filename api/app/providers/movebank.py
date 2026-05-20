"""Movebank provider: polls the Movebank REST API and publishes the
latest position per tracked individual to live/movebank/<id>.

Required env vars:
  MOVEBANK_USERNAME, MOVEBANK_PASSWORD
  MOVEBANK_STUDY_ID                 — numeric Movebank study id
Optional:
  MOVEBANK_POLL_SECONDS             — default 60

Docs: https://www.movebank.org/cms/movebank-content/movebank-rest-api
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging

import aiomqtt
import httpx

from .base import Provider

log = logging.getLogger(__name__)

_MOVEBANK_URL = "https://www.movebank.org/movebank/service/direct-read"


class Movebank(Provider):
    name = "movebank"

    def __init__(
        self,
        mqtt_host: str,
        mqtt_port: int,
        *,
        username: str,
        password: str,
        study_id: str,
        poll_seconds: int = 60,
    ) -> None:
        super().__init__(mqtt_host, mqtt_port)
        self.username = username
        self.password = password
        self.study_id = study_id
        self.poll_seconds = poll_seconds

    async def loop(self, client: aiomqtt.Client) -> None:
        async with httpx.AsyncClient(
            auth=(self.username, self.password), timeout=60
        ) as http:
            while True:
                await self._poll_once(client, http)
                await asyncio.sleep(self.poll_seconds)

    async def _poll_once(
        self, client: aiomqtt.Client, http: httpx.AsyncClient
    ) -> None:
        params = {
            "entity_type": "event",
            "study_id": self.study_id,
            "max_events_per_individual": "1",
        }
        try:
            r = await http.get(_MOVEBANK_URL, params=params)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.warning("Movebank poll failed (%s)", e)
            return

        latest: dict[str, dict] = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            individual = (
                row.get("individual-local-identifier")
                or row.get("individual_local_identifier")
                or row.get("individual_id")
            )
            try:
                lat = float(row["location-lat"])
                lon = float(row["location-long"])
            except (KeyError, ValueError):
                continue
            if not individual:
                continue
            latest[individual] = {
                "id": individual,
                "provider": self.name,
                "lat": lat,
                "lon": lon,
                "timestamp": row.get("timestamp", ""),
                "properties": {
                    k: v for k, v in row.items()
                    if v and k not in ("location-lat", "location-long")
                },
            }

        log.info("Movebank: publishing %d individuals", len(latest))
        for feature in latest.values():
            await self.publish(client, feature)
