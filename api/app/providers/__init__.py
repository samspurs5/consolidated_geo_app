"""Provider registry. Each provider is opted in via the PROVIDERS env
var (comma-separated). Provider-specific config is read from its own
env vars.

    PROVIDERS=movebank,random_walker

To add a new provider:
  1. Subclass Provider in api/app/providers/<name>.py
  2. Add a branch to _build_enabled() below mapping a string name to
     your provider's constructor (read config from env).
  3. Document the env vars in .env.example.

External systems that already speak MQTT don't need a Python adapter
at all — they just publish to live/<name>/<id> on the broker."""
from __future__ import annotations

import asyncio
import logging
import os

from ..config import settings
from .base import Provider
from .movebank import Movebank
from .random_walker import from_env as random_walker_from_env

log = logging.getLogger(__name__)


def _build_enabled() -> list[Provider]:
    names = [s.strip() for s in (os.getenv("PROVIDERS") or "").split(",") if s.strip()]
    out: list[Provider] = []
    for name in names:
        if name == "random_walker":
            out.append(random_walker_from_env(settings.mqtt_host, settings.mqtt_port))
        elif name == "movebank":
            user = os.getenv("MOVEBANK_USERNAME") or ""
            pwd = os.getenv("MOVEBANK_PASSWORD") or ""
            study = os.getenv("MOVEBANK_STUDY_ID") or ""
            if not (user and pwd and study):
                log.warning(
                    "movebank requested but MOVEBANK_USERNAME/PASSWORD/STUDY_ID not set; skipping"
                )
                continue
            out.append(Movebank(
                settings.mqtt_host, settings.mqtt_port,
                username=user, password=pwd, study_id=study,
                poll_seconds=int(os.getenv("MOVEBANK_POLL_SECONDS") or 60),
            ))
        else:
            log.warning("Unknown provider %r requested via PROVIDERS — skipping", name)
    return out


async def start_all() -> list[asyncio.Task]:
    """Spawn one background task per enabled provider. Returns the tasks so
    the lifespan can cancel them on shutdown."""
    providers = _build_enabled()
    if not providers:
        log.info("No providers enabled (PROVIDERS env empty). External "
                 "publishers can still push to live/<name>/<id> directly.")
    tasks = [
        asyncio.create_task(p.run(), name=f"provider-{p.name}")
        for p in providers
    ]
    return tasks
