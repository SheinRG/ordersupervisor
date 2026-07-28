"""
TRACER BULLET — throwaway code, not part of the real system.

This is the smallest Temporal program that exercises every concept the real
Order Supervisor needs. Read this file before reading the real workflow; it is
the same shape, minus the business logic.

    Terminal 1:  uv run python tracer.py worker
    Terminal 2:  uv run python tracer.py demo

Then open http://localhost:8233 and look at the workflow's Event History.
"""

import asyncio
import sys
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

TASK_QUEUE = "tracer-task-queue"


# ---------------------------------------------------------------------------
# ACTIVITY — the ONLY place I/O is allowed.
#
# Workflow code gets replayed from history to rebuild its state, so it must be
# deterministic. Anything that talks to the outside world (Postgres, the LLM,
# an HTTP call, the clock, random) lives in an activity instead. Temporal
# records each activity's RESULT in history, so on replay it reuses the
# recorded value rather than running the side effect again.
# ---------------------------------------------------------------------------
@activity.defn
async def record(line: str) -> str:
    print(f"      [activity] {line}")
    return line.upper()


# ---------------------------------------------------------------------------
# WORKFLOW — deterministic orchestration. No I/O, ever.
# ---------------------------------------------------------------------------
@workflow.defn
class TracerWorkflow:
    def __init__(self) -> None:
        self.inbox: list[str] = []   # events pushed in by signals
        self.stop = False
        self.wake_count = 0

    # --- SIGNAL: how the outside world pushes data INTO a running workflow.
    # This is what the assignment means by "events should be sent into the
    # workflow as signals". Signal handlers must not block or do I/O — they
    # just mutate state, and the run loop notices.
    @workflow.signal
    async def deliver(self, event: str) -> None:
        self.inbox.append(event)

    @workflow.signal
    async def shutdown(self) -> None:
        self.stop = True

    # --- QUERY: read live state out of a RUNNING workflow, with no database
    # involved. The UI uses this for "current memory / next wake time".
    # Queries must be side-effect free.
    @workflow.query
    def status(self) -> dict:
        return {"pending": len(self.inbox), "wakes": self.wake_count}

    @workflow.run
    async def run(self) -> list[str]:
        log: list[str] = []

        # execute_activity is how the workflow asks a worker to do real work.
        log.append(
            await workflow.execute_activity(
                record,
                "workflow started",
                start_to_close_timeout=timedelta(seconds=10),
            )
        )

        while not self.stop:
            # ***** THE ONE LINE THAT MATTERS *****
            # Sleep until EITHER something lands in the inbox OR 15s elapse.
            #
            # This is a DURABLE TIMER. The workflow is evicted from worker
            # memory entirely while it waits — it consumes no thread, no RAM,
            # no connection. The Temporal *server* wakes it when the timer
            # fires. That is why "sleep 6 hours" is free here and painful with
            # cron + a database, and it is the whole reason this assignment
            # specifies Temporal.
            try:
                await workflow.wait_condition(
                    lambda: bool(self.inbox) or self.stop,
                    timeout=timedelta(seconds=15),
                )
                trigger = "signal arrived"
            except asyncio.TimeoutError:
                trigger = "scheduled wake-up"
            # *************************************

            if self.stop:
                break

            self.wake_count += 1
            batch, self.inbox = self.inbox, []
            log.append(
                await workflow.execute_activity(
                    record,
                    f"woke on {trigger}; drained {len(batch)} event(s): {batch}",
                    start_to_close_timeout=timedelta(seconds=10),
                )
            )

        log.append(
            await workflow.execute_activity(
                record,
                "workflow completing",
                start_to_close_timeout=timedelta(seconds=10),
            )
        )
        return log


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------
async def run_worker() -> None:
    client = await Client.connect("localhost:7233")
    print(f"worker polling '{TASK_QUEUE}' — Ctrl+C to stop")
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[TracerWorkflow],
        activities=[record],
    ):
        await asyncio.Future()  # run forever


async def run_demo() -> None:
    client = await Client.connect("localhost:7233")

    # Deterministic workflow ID. Starting it twice with the same ID is
    # rejected — which is exactly how we get "one workflow per order" for free.
    handle = await client.start_workflow(
        TracerWorkflow.run,
        id="tracer-demo",
        task_queue=TASK_QUEUE,
    )
    print(f"started workflow id={handle.id}")

    print("\n-- waiting 3s, then sending a signal (should wake it EARLY) --")
    await asyncio.sleep(3)
    await handle.signal(TracerWorkflow.deliver, "payment_failed")
    await asyncio.sleep(1)
    print(f"   query -> {await handle.query(TracerWorkflow.status)}")

    print("\n-- now staying quiet, so the 15s timer fires on its own --")
    await asyncio.sleep(17)
    print(f"   query -> {await handle.query(TracerWorkflow.status)}")

    print("\n-- shutting down --")
    await handle.signal(TracerWorkflow.shutdown)
    result = await handle.result()
    print("\nresult:")
    for line in result:
        print(f"   {line}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "worker"
    asyncio.run(run_worker() if mode == "worker" else run_demo())
