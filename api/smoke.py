"""End-to-end smoke test against a running worker + Temporal dev server.

    uv run python smoke.py

Exercises every acceptance criterion: start, signal, classifier gate, early
wake, scheduled wake, actions, run instructions, and workflow-owned completion.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import asdict

from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
from temporalio.client import Client

load_dotenv()

from supervisor import TASK_QUEUE, db, workflow_id_for  # noqa: E402
from supervisor.models import OrderContext, OrderEvent, SupervisorConfig  # noqa: E402
from supervisor.workflow import OrderSupervisorWorkflow, RunParams  # noqa: E402


def banner(text: str) -> None:
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


async def main() -> None:
    client = await Client.connect("localhost:7233")

    order_id = f"ORD-{uuid.uuid4().hex[:6].upper()}"
    run_id = f"run-{uuid.uuid4().hex[:8]}"

    config = SupervisorConfig(
        name="Default Order Supervisor",
        base_instruction=(
            "Watch this order end to end. Escalate payment problems fast, keep "
            "the customer informed, and don't wake up for routine events."
        ),
        wake_aggressiveness="normal",
        time_scale=3600.0,  # 1 business hour == 1 real second
        default_wake_seconds=6 * 3600,
    )

    order = OrderContext(
        order_id=order_id,
        customer_name="Priya Raman",
        items=["Mechanical keyboard", "USB-C hub"],
        total=184.50,
    )

    # Mirror what POST /api/runs does: a run row must exist before the
    # workflow starts, since activities (persist_activity, persist_run_state)
    # write against its foreign key. smoke.py talks to Temporal directly, so
    # it has to do this bookkeeping itself.
    supervisor_id = "smoke-test"
    await db.upsert_supervisor(
        {
            "id": supervisor_id,
            "name": config.name,
            "base_instruction": config.base_instruction,
            "enabled_actions": config.enabled_actions,
            "default_wake_seconds": config.default_wake_seconds,
            "model": config.model,
            "classifier_model": config.classifier_model,
            "wake_aggressiveness": config.wake_aggressiveness,
            "time_scale": config.time_scale,
            "max_age_seconds": config.max_age_seconds,
        }
    )
    await db.create_run(
        {
            "id": run_id,
            "supervisor_id": supervisor_id,
            "order_id": order_id,
            "workflow_id": workflow_id_for(order_id),
            "order_context": asdict(order),
        }
    )

    banner(f"starting supervisor for {order_id}")
    handle = await client.start_workflow(
        OrderSupervisorWorkflow.run,
        RunParams(run_id=run_id, order=order, config=config),
        id=workflow_id_for(order_id),
        task_queue=TASK_QUEUE,
    )
    print(f"workflow id: {handle.id}")

    await asyncio.sleep(2)
    print(f"state: {await handle.query(OrderSupervisorWorkflow.state)}")

    banner("1. routine event (payment_confirmed) -> classifier should NOT wake agent")
    await handle.signal(
        OrderSupervisorWorkflow.deliver_event, OrderEvent(kind="payment_confirmed")
    )
    await asyncio.sleep(2)
    s = await handle.query(OrderSupervisorWorkflow.state)
    print(f"inferences so far: {s.inference_count} (expect: still 1)")

    banner("2. run-specific instruction added mid-flight")
    await handle.signal(
        OrderSupervisorWorkflow.add_instruction,
        "If shipment is delayed, escalate immediately.",
    )

    banner("3. urgent event (shipment_delayed) -> should wake agent EARLY")
    await handle.signal(
        OrderSupervisorWorkflow.deliver_event,
        OrderEvent(kind="shipment_delayed", payload={"carrier": "BlueDart"}),
    )
    await asyncio.sleep(3)
    s = await handle.query(OrderSupervisorWorkflow.state)
    print(f"inferences: {s.inference_count}  escalated: {s.escalated}")
    print(f"memory:\n{s.memory_summary}")

    banner("4. pause -> events queue but no inference runs")
    await handle.signal(OrderSupervisorWorkflow.pause)
    await asyncio.sleep(1)
    await handle.signal(
        OrderSupervisorWorkflow.deliver_event,
        OrderEvent(kind="customer_message_received", payload={"message": "Where is it?"}),
    )
    await asyncio.sleep(2)
    s = await handle.query(OrderSupervisorWorkflow.state)
    print(f"status: {s.status}  pending: {s.pending_events}  inferences: {s.inference_count}")

    banner("5. resume -> queued events drain")
    await handle.signal(OrderSupervisorWorkflow.resume)
    await asyncio.sleep(3)
    s = await handle.query(OrderSupervisorWorkflow.state)
    print(f"status: {s.status}  inferences: {s.inference_count}")

    banner("6. terminal event (delivered) -> WORKFLOW-owned completion")
    await handle.signal(
        OrderSupervisorWorkflow.deliver_event, OrderEvent(kind="delivered")
    )

    final = await asyncio.wait_for(handle.result(), timeout=60)
    banner("final output")
    print(f"summary:\n  {final.summary}\n")
    print("learnings:")
    for item in final.learnings:
        print(f"  - {item}")
    print("recommendations:")
    for item in final.recommendations:
        print(f"  - {item}")


if __name__ == "__main__":
    asyncio.run(main())
