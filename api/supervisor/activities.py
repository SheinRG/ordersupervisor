"""Activities — every side effect in the system lives here.

Workflow code is replayed to rebuild state, so it must be deterministic.
Anything that talks to Postgres, the LLM, or the outside world is an activity,
and Temporal records the *result* in history rather than re-running it.
"""

from __future__ import annotations

import os
from dataclasses import asdict

from temporalio import activity

from . import db
from .agent import Agent, ScriptedAgent
from .models import (
    ActionCall,
    AgentDecision,
    AgentInput,
    FinalOutput,
    OrderEvent,
    RunState,
    WakeDecision,
)

_agent: Agent | None = None


def get_agent() -> Agent:
    """Resolve the agent implementation once, from AGENT_MODE."""
    global _agent
    if _agent is None:
        mode = os.environ.get("AGENT_MODE", "scripted").lower()
        if mode == "grok":
            from .grok import GrokAgent  # imported lazily; needs XAI_API_KEY

            _agent = GrokAgent()
        else:
            _agent = ScriptedAgent()
    return _agent


def agent_name() -> str:
    return getattr(get_agent(), "name", "unknown")


# --------------------------------------------------------------------------
# Agent activities
# --------------------------------------------------------------------------


@activity.defn
async def agent_classify(
    event: OrderEvent, aggressiveness: str, guidance: str
) -> WakeDecision:
    return await get_agent().classify(event, aggressiveness, guidance)


@activity.defn
async def agent_decide(payload: AgentInput) -> AgentDecision:
    return await get_agent().decide(payload)


@activity.defn
async def agent_finalize(payload: AgentInput, reason: str) -> FinalOutput:
    return await get_agent().finalize(payload, reason)


# --------------------------------------------------------------------------
# Business actions
#
# The spec is explicit: these need not send anything externally. Each one
# creates an activity record for the run, stored in Postgres and shown in the
# UI's action history.
# --------------------------------------------------------------------------

_RECIPIENTS = {
    "message_fulfillment_team": "fulfillment team",
    "message_payments_team": "payments team",
    "message_logistics_team": "logistics team",
    "message_customer": "customer",
    "create_internal_note": "internal note",
}


@activity.defn
async def execute_action(run_id: str, action: ActionCall) -> str:
    target = _RECIPIENTS.get(action.name)
    if target is None:
        raise ValueError(f"unknown action '{action.name}'")

    rendered = f"-> {target}: {action.body}"
    if action.reason:
        rendered += f"  (because: {action.reason})"

    await db.append_activity(run_id, f"action:{action.name}", rendered)
    activity.logger.info("executed %s for run %s", action.name, run_id)
    return rendered


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


@activity.defn
async def persist_activity(run_id: str, kind: str, detail: str) -> None:
    await db.append_activity(run_id, kind, detail)


@activity.defn
async def persist_run_state(run_id: str, state: RunState) -> None:
    await db.update_run_state(run_id, asdict(state))


@activity.defn
async def persist_final_output(
    run_id: str, final: FinalOutput, reason: str
) -> None:
    await db.complete_run(run_id, asdict(final), reason)
    await db.append_activity(run_id, "final output", final.summary)


ALL_ACTIVITIES = [
    agent_classify,
    agent_decide,
    agent_finalize,
    execute_action,
    persist_activity,
    persist_run_state,
    persist_final_output,
]
