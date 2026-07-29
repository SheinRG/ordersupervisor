"""GroqAgent — the real LLM implementation of the `Agent` protocol, on Groq.

Same shape as `grok.py` (xAI): an OpenAI-compatible chat-completions endpoint
with JSON-schema structured outputs, so the decision object is guaranteed to
match what the workflow expects and we never hand-parse loose JSON.

Two models, deliberately:
  * main agent  -> a capable Llama model (structured outputs)
  * classifier  -> a small/fast model; the spec calls for something
                   "lightweight" to gate whether the main agent wakes.

Set AGENT_MODE=groq and GROQ_API_KEY to enable. If a call fails, the
exception propagates out of the activity and Temporal retries it with
backoff — that is how free-tier rate limits are absorbed without any bespoke
retry code.
"""

from __future__ import annotations

import json
import os

import httpx

from .agent import ScriptedAgent, _compact
from .models import (
    ActionCall,
    AgentDecision,
    AgentInput,
    FinalOutput,
    OrderEvent,
    WakeDecision,
)

BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
# Not every Groq-hosted model supports `response_format: json_schema` (see
# https://console.groq.com/docs/structured-outputs#supported-models) — most
# Llama/Qwen chat models don't, but the openai/gpt-oss family does.
MAIN_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
CLASSIFIER_MODEL = os.environ.get("GROQ_CLASSIFIER_MODEL", "openai/gpt-oss-20b")
TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# --------------------------------------------------------------------------
# JSON schemas — identical contract to GrokAgent; both implement the same
# Agent protocol and must produce the same shape.
# --------------------------------------------------------------------------

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reasoning",
        "actions",
        "memory_update",
        "next_wake_seconds",
        "recommends_completion",
        "escalated",
        "needs_review",
    ],
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "One short paragraph: what you concluded and why.",
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "body", "reason"],
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [
                            "message_fulfillment_team",
                            "message_payments_team",
                            "message_logistics_team",
                            "message_customer",
                            "create_internal_note",
                        ],
                    },
                    "body": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "memory_update": {
            "type": "string",
            "description": "Replacement compact memory summary, <= 12 short lines.",
        },
        "next_wake_seconds": {
            "type": "integer",
            "description": "Business seconds until you want to review again.",
        },
        "recommends_completion": {"type": "boolean"},
        "escalated": {"type": "boolean"},
        "needs_review": {"type": "boolean"},
    },
}

WAKE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["should_wake", "reason"],
    "properties": {
        "should_wake": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

FINAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "important_actions", "learnings", "recommendations"],
    "properties": {
        "summary": {"type": "string"},
        "important_actions": {"type": "array", "items": {"type": "string"}},
        "learnings": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
}

SUPERVISOR_SYSTEM = """\
You are an autonomous supervisor watching a single e-commerce order from \
creation to completion.

You are woken in one of three ways: at workflow start, when an event arrives \
that a classifier judged important, or when a scheduled wake-up you yourself \
set comes due. You are NOT running continuously — between wake-ups you are \
asleep and see nothing.

Your job each time you wake:
  1. Read the new events, your memory, and any run-specific instructions.
  2. Decide whether anything actually needs doing. Doing nothing is often right.
  3. Choose business actions from the enabled list only.
  4. Rewrite your compact memory summary so a future you can pick up cold.
  5. Choose when to wake next, in BUSINESS seconds.

Rules:
  * Run-specific instructions override your defaults. Follow them literally.
  * You may set recommends_completion, but you do NOT end the run — lifecycle
    rules owned by the workflow decide that. Recommend, don't assume.
  * Keep memory to at most 12 short lines. Fold old detail into a digest line.
  * Prefer longer sleeps when the order is progressing normally; short sleeps
    when something is unresolved.
  * Never invent events that did not happen."""

CLASSIFIER_SYSTEM = """\
You are a cheap gate in front of an expensive AI supervisor.

Given ONE incoming order event, decide whether it justifies waking the main \
supervisor right now, or whether it can safely wait until the supervisor's \
next scheduled review.

Wake for: money problems, delivery failures or delays, refunds, direct \
customer contact, terminal lifecycle events, and anything you don't recognise.
Do not wake for: routine progress events that need no decision.

Be decisive and brief."""


class GroqAgent:
    """Calls Groq. Falls back to nothing — errors propagate so Temporal retries."""

    name = "groq"

    def __init__(self) -> None:
        self.api_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError(
                "AGENT_MODE=groq but GROQ_API_KEY is not set. "
                "Set it in api/.env, or use AGENT_MODE=scripted."
            )
        # Rules-based helper reused for deterministic bits (memory compaction).
        self._fallback = ScriptedAgent()

    # ------------------------------------------------------------------
    async def _complete(
        self, model: str, system: str, user: str, schema: dict, schema_name: str
    ) -> dict:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            res = await client.post(
                f"{BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            res.raise_for_status()
            body = res.json()
        return json.loads(body["choices"][0]["message"]["content"])

    # ------------------------------------------------------------------
    async def classify(
        self, event: OrderEvent, aggressiveness: str, guidance: str
    ) -> WakeDecision:
        user = (
            f"Event: {event.kind}\n"
            f"Payload: {json.dumps(event.payload)}\n"
            f"Known event type: {event.is_known}\n"
            f"Supervisor wake aggressiveness: {aggressiveness}\n"
            f"Supervisor's current memory:\n{guidance or '(empty)'}"
        )
        out = await self._complete(
            CLASSIFIER_MODEL, CLASSIFIER_SYSTEM, user, WAKE_SCHEMA, "wake_decision"
        )
        return WakeDecision(
            should_wake=bool(out["should_wake"]), reason=str(out["reason"])
        )

    # ------------------------------------------------------------------
    async def decide(self, payload: AgentInput) -> AgentDecision:
        events = "\n".join(
            f"  - {e.kind} {json.dumps(e.payload)}" for e in payload.new_events
        ) or "  (none)"
        instructions = "\n".join(f"  - {i}" for i in payload.extra_instructions) or (
            "  (none)"
        )
        timeline = "\n".join(f"  {t}" for t in payload.recent_timeline[-25:]) or "  (empty)"

        user = f"""\
Wake trigger: {payload.trigger}
Business time elapsed: {payload.business_age_seconds // 3600}h

ORDER
  id: {payload.order.order_id}
  customer: {payload.order.customer_name}
  items: {", ".join(payload.order.items) or "(none)"}
  total: {payload.order.total} {payload.order.currency}

BASE INSTRUCTION
  {payload.base_instruction}

RUN-SPECIFIC INSTRUCTIONS (override the base instruction)
{instructions}

YOUR MEMORY
{payload.memory_summary or "  (empty - this is your first wake)"}

RECENT TIMELINE
{timeline}

NEW EVENTS SINCE YOU LAST WOKE
{events}

ENABLED ACTIONS (you may use only these)
  {", ".join(payload.enabled_actions)}

Decide what to do."""

        out = await self._complete(
            MAIN_MODEL, SUPERVISOR_SYSTEM, user, DECISION_SCHEMA, "agent_decision"
        )

        allowed = set(payload.enabled_actions)
        actions = [
            ActionCall(
                name=a["name"], body=a["body"], reason=a.get("reason", "")
            )
            for a in out.get("actions", [])
            if a.get("name") in allowed
        ]

        memory = str(out.get("memory_update", "")).strip()
        if not memory:
            memory = payload.memory_summary
        else:
            # Enforce the line budget regardless of what the model returned.
            memory = _compact("", memory.splitlines())

        wake = int(out.get("next_wake_seconds", 6 * 3600))
        wake = max(300, min(wake, 7 * 24 * 3600))  # clamp: 5min .. 7 days

        return AgentDecision(
            reasoning=str(out.get("reasoning", "")),
            actions=actions,
            memory_update=memory,
            next_wake_seconds=wake,
            recommends_completion=bool(out.get("recommends_completion", False)),
            escalated=bool(out.get("escalated", False)),
            needs_review=bool(out.get("needs_review", False)),
        )

    # ------------------------------------------------------------------
    async def finalize(self, payload: AgentInput, reason: str) -> FinalOutput:
        user = f"""\
The supervision run for order {payload.order.order_id} has ended.
Reason the workflow ended: {reason}
Business time supervised: {payload.business_age_seconds // 3600}h

FINAL MEMORY
{payload.memory_summary or "(empty)"}

TIMELINE TAIL
{chr(10).join("  " + t for t in payload.recent_timeline[-30:]) or "  (empty)"}

Write the end-of-run report: a short summary, the actions that actually \
mattered, concrete learnings about how this order went, and recommendations \
for handling similar orders better."""

        out = await self._complete(
            MAIN_MODEL, SUPERVISOR_SYSTEM, user, FINAL_SCHEMA, "final_output"
        )
        return FinalOutput(
            summary=str(out.get("summary", "")),
            important_actions=[str(x) for x in out.get("important_actions", [])],
            learnings=[str(x) for x in out.get("learnings", [])],
            recommendations=[str(x) for x in out.get("recommendations", [])],
        )
