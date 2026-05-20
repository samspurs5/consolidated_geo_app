"""Provider plugin interface.

A provider is a long-running async task that pulls data from some
external source (REST API, file, hardware) and publishes JSON messages
to live/<provider-name>/<entity-id> on the local MQTT broker. From
there the live bridge picks them up uniformly.

External systems that already speak MQTT skip Python entirely — just
publish to the broker on the right topic.
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging

import aiomqtt

log = logging.getLogger(__name__)


class Provider(abc.ABC):
    name: str = "abstract"

    def __init__(self, mqtt_host: str, mqtt_port: int) -> None:
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port

    async def publish(self, client: aiomqtt.Client, feature: dict) -> None:
        topic = f"live/{self.name}/{feature['id']}"
        await client.publish(topic, json.dumps(feature).encode("utf-8"))

    async def run(self) -> None:
        """Connect to the broker and call loop(). Auto-reconnect on failure."""
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.mqtt_host, port=self.mqtt_port
                ) as client:
                    log.info("Provider %s connected to broker", self.name)
                    await self.loop(client)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "Provider %s disconnected (%s). Reconnecting in 5 s.",
                    self.name, e,
                )
                await asyncio.sleep(5)

    @abc.abstractmethod
    async def loop(self, client: aiomqtt.Client) -> None:
        """Run until the broker connection drops. Should publish features
        as they become available; sleep between batches as appropriate."""
