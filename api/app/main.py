"""FastAPI surface for workflow and run management.

The API is deliberately thin: it starts workflows, pushes signals into them,
and reads state back. It contains no supervision logic — that all lives in the
workflow and the agent.

Reads are split on purpose:
  * history (timeline, actions)  -> Postgres
  * live state (memory, sleep, next wake) -> @workflow.query against the
    running workflow, so the UI shows what the workflow actually believes.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from temporalio.client import Client
from temporalio.service import RPCError

load_dotenv()

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervisor import TASK_QUEUE, workflow_id_for  # noqa: E402
from supervisor import db  # noqa: E402
from supervisor.models import (  # noqa: E402
    ALL_ACTIONS,
    KNOWN_EVENTS,
    OrderContext,
    OrderEvent,
    SupervisorConfig,
)
from supervisor.workflow import OrderSupervisorWorkflow, RunParams  # noqa: E402

_client: Client | None = None


async def temporal() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(
            os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
        )
    return _client


# --------------------------------------------------------------------------
# Hardcoded supervisor templates (explicitly allowed by the spec). Multiple
# templates is a listed good-to-have.
# --------------------------------------------------------------------------

SEED_TEMPLATES = [
    {
        "id": "default",
        "name": "Balanced Order Supervisor",
        "base_instruction": (
            "Watch this order from payment through delivery. Escalate payment "
            "failures and shipment delays quickly, keep the customer informed, "
            "and stay asleep through routine lifecycle events."
        ),
        "enabled_actions": list(ALL_ACTIONS),
        "default_wake_seconds": 6 * 3600,
        "model": "grok-4.5",
        "classifier_model": "grok-4.1-fast",
        "wake_aggressiveness": "normal",
        "time_scale": 3600.0,
        "max_age_seconds": 30 * 24 * 3600,
    },
    {
        "id": "high-touch",
        "name": "High-Touch VIP Supervisor",
        "base_instruction": (
            "This is a VIP order. Review frequently, contact the customer "
            "proactively at every meaningful milestone, and escalate anything "
            "that could delay delivery."
        ),
        "enabled_actions": list(ALL_ACTIONS),
        "default_wake_seconds": 2 * 3600,
        "model": "grok-4.5",
        "classifier_model": "grok-4.1-fast",
        "wake_aggressiveness": "high",
        "time_scale": 3600.0,
        "max_age_seconds": 14 * 24 * 3600,
    },
    {
        "id": "hands-off",
        "name": "Hands-Off Supervisor (no customer contact)",
        "base_instruction": (
            "Supervise quietly. Never contact the customer directly - route "
            "everything through internal teams and notes for human review."
        ),
        "enabled_actions": [a for a in ALL_ACTIONS if a != "message_customer"],
        "default_wake_seconds": 12 * 3600,
        "model": "grok-4.5",
        "classifier_model": "grok-4.1-fast",
        "wake_aggressiveness": "low",
        "time_scale": 3600.0,
        "max_age_seconds": 30 * 24 * 3600,
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    for template in SEED_TEMPLATES:
        await db.upsert_supervisor(template)
    yield
    await db.close()


app = FastAPI(title="Order Supervisor API", version="0.1.0", lifespan=lifespan)
# Local dev only: allow any localhost port, so the UI can move off 3000 when
# something else already holds it. Not a production CORS policy.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class SupervisorBody(BaseModel):
    id: str | None = None
    name: str
    base_instruction: str
    enabled_actions: list[str] = Field(default_factory=lambda: list(ALL_ACTIONS))
    default_wake_seconds: int = 6 * 3600
    model: str = "grok-4.5"
    classifier_model: str = "grok-4.1-fast"
    wake_aggressiveness: str = "normal"
    time_scale: float = 3600.0
    max_age_seconds: int = 30 * 24 * 3600


class OrderBody(BaseModel):
    order_id: str | None = None
    customer_name: str = ""
    items: list[str] = Field(default_factory=list)
    total: float = 0.0
    currency: str = "USD"


class RunBody(BaseModel):
    supervisor_id: str = "default"
    order: OrderBody = Field(default_factory=OrderBody)


class EventBody(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class InstructionBody(BaseModel):
    instruction: str


class TerminateBody(BaseModel):
    reason: str = "manually terminated from the UI"


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    from supervisor.activities import agent_name

    temporal_ok = True
    try:
        await temporal()
    except Exception:
        temporal_ok = False
    return {
        "ok": True,
        "storage": db.storage_mode(),
        "agent": agent_name(),
        "temporal_connected": temporal_ok,
        "known_events": list(KNOWN_EVENTS),
        "actions": list(ALL_ACTIONS),
    }


# --------------------------------------------------------------------------
# Supervisors
# --------------------------------------------------------------------------


@app.get("/api/supervisors")
async def get_supervisors() -> list[dict[str, Any]]:
    return await db.list_supervisors()


@app.post("/api/supervisors")
async def create_supervisor(body: SupervisorBody) -> dict[str, Any]:
    record = body.model_dump()
    record["id"] = body.id or f"sup-{uuid.uuid4().hex[:8]}"
    await db.upsert_supervisor(record)
    return record


@app.get("/api/supervisors/{supervisor_id}")
async def read_supervisor(supervisor_id: str) -> dict[str, Any]:
    found = await db.get_supervisor(supervisor_id)
    if not found:
        raise HTTPException(404, "supervisor not found")
    return found


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


@app.post("/api/runs")
async def create_run(body: RunBody) -> dict[str, Any]:
    template = await db.get_supervisor(body.supervisor_id)
    if not template:
        raise HTTPException(404, f"supervisor '{body.supervisor_id}' not found")

    order_id = body.order.order_id or f"ORD-{uuid.uuid4().hex[:6].upper()}"
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    workflow_id = workflow_id_for(order_id)

    config = SupervisorConfig(
        name=template["name"],
        base_instruction=template["base_instruction"],
        enabled_actions=list(template["enabled_actions"]),
        default_wake_seconds=template["default_wake_seconds"],
        model=template["model"],
        classifier_model=template["classifier_model"],
        wake_aggressiveness=template["wake_aggressiveness"],
        time_scale=float(template["time_scale"]),
        max_age_seconds=template["max_age_seconds"],
    )
    order = OrderContext(
        order_id=order_id,
        customer_name=body.order.customer_name,
        items=list(body.order.items),
        total=body.order.total,
        currency=body.order.currency,
    )

    await db.create_run(
        {
            "id": run_id,
            "supervisor_id": body.supervisor_id,
            "order_id": order_id,
            "workflow_id": workflow_id,
            "order_context": asdict(order),
        }
    )

    client = await temporal()
    try:
        await client.start_workflow(
            OrderSupervisorWorkflow.run,
            RunParams(run_id=run_id, order=order, config=config),
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    except RPCError as exc:
        # Deterministic workflow id => starting twice for one order is rejected
        # by the server. Surface that as a clean 409 rather than a 500.
        raise HTTPException(
            409, f"a supervisor is already running for order {order_id}"
        ) from exc

    return {"run_id": run_id, "order_id": order_id, "workflow_id": workflow_id}


@app.get("/api/runs")
async def get_runs() -> list[dict[str, Any]]:
    return await db.list_runs()


@app.get("/api/runs/{run_id}")
async def read_run(run_id: str) -> dict[str, Any]:
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")

    activities = await db.list_activities(run_id)

    # Live state comes from the workflow itself, not the database.
    live: dict[str, Any] | None = None
    live_error: str | None = None
    try:
        client = await temporal()
        handle = client.get_workflow_handle(run["workflow_id"])
        live = asdict(await handle.query(OrderSupervisorWorkflow.state))
    except Exception as exc:  # completed, terminated, or worker down
        live_error = str(exc)

    return {"run": run, "activities": activities, "live": live, "live_error": live_error}


# --------------------------------------------------------------------------
# Signals into a running workflow
# --------------------------------------------------------------------------


async def _handle(run_id: str):
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    client = await temporal()
    return client.get_workflow_handle(run["workflow_id"])


@app.post("/api/runs/{run_id}/events")
async def inject_event(run_id: str, body: EventBody) -> dict[str, Any]:
    handle = await _handle(run_id)
    event = OrderEvent(
        kind=body.kind,
        payload=body.payload,
        received_at=datetime.now(timezone.utc).isoformat(),
    )
    await handle.signal(OrderSupervisorWorkflow.deliver_event, event)
    return {"delivered": body.kind}


@app.post("/api/runs/{run_id}/instructions")
async def add_instruction(run_id: str, body: InstructionBody) -> dict[str, Any]:
    handle = await _handle(run_id)
    await handle.signal(OrderSupervisorWorkflow.add_instruction, body.instruction)
    return {"added": body.instruction}


@app.post("/api/runs/{run_id}/interrupt")
async def interrupt(run_id: str) -> dict[str, Any]:
    handle = await _handle(run_id)
    await handle.signal(OrderSupervisorWorkflow.interrupt)
    return {"interrupted": True}


@app.post("/api/runs/{run_id}/pause")
async def pause(run_id: str) -> dict[str, Any]:
    handle = await _handle(run_id)
    await handle.signal(OrderSupervisorWorkflow.pause)
    return {"paused": True}


@app.post("/api/runs/{run_id}/resume")
async def resume(run_id: str) -> dict[str, Any]:
    handle = await _handle(run_id)
    await handle.signal(OrderSupervisorWorkflow.resume)
    return {"resumed": True}


@app.post("/api/runs/{run_id}/terminate")
async def terminate(run_id: str, body: TerminateBody) -> dict[str, Any]:
    handle = await _handle(run_id)
    # Graceful: a signal, so the workflow still produces its final summary.
    # (A hard handle.terminate() would skip the end-of-run output entirely.)
    await handle.signal(OrderSupervisorWorkflow.terminate_run, body.reason)
    return {"terminating": body.reason}
