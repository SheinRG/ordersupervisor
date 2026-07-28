"""Temporal worker: hosts the workflow and activity code.

    uv run python worker.py

The worker is the process that actually executes your code. The Temporal
server only stores history and fires timers — it never runs your logic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

# Windows consoles default to cp1252, which mangles non-ASCII in agent output.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
from temporalio.client import Client
from temporalio.worker import Worker

load_dotenv()

from supervisor import TASK_QUEUE  # noqa: E402
from supervisor import db  # noqa: E402
from supervisor.activities import ALL_ACTIVITIES, agent_name  # noqa: E402
from supervisor.workflow import OrderSupervisorWorkflow  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")


async def main() -> None:
    target = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(target)

    log.info("temporal:  %s", target)
    log.info("agent:     %s", agent_name())
    log.info("storage:   %s", db.storage_mode())
    log.info("polling task queue '%s' — Ctrl+C to stop", TASK_QUEUE)

    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[OrderSupervisorWorkflow],
        activities=ALL_ACTIVITIES,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
