"""The agent seam.

Everything that reasons sits behind `Agent`. Two implementations exist:

  ScriptedAgent  — deterministic, rule-based, no network. Used for tests, for
                   developing the workflow without spending tokens, and as the
                   fallback if the LLM provider is rate-limited or down.
  GrokAgent      — xAI Grok with JSON-schema structured outputs (grok.py).

Selected at worker startup via AGENT_MODE=scripted|grok. Because the workflow
only ever sees this interface, swapping implementations changes nothing about
the orchestration.
"""

from __future__ import annotations

from typing import Protocol

from .models import (
    ActionCall,
    AgentDecision,
    AgentInput,
    FinalOutput,
    OrderEvent,
    WakeDecision,
)

# Business seconds.
MINUTE, HOUR, DAY = 60, 3600, 86400


class Agent(Protocol):
    """The contract the workflow depends on."""

    async def classify(
        self, event: OrderEvent, aggressiveness: str, guidance: str
    ) -> WakeDecision:
        """Cheap gate: is this event worth waking the main agent for, now?"""
        ...

    async def decide(self, payload: AgentInput) -> AgentDecision:
        """Full inference: reason, choose actions, update memory, set next wake."""
        ...

    async def finalize(self, payload: AgentInput, reason: str) -> FinalOutput:
        """End-of-run summary, learnings and recommendations."""
        ...


# --------------------------------------------------------------------------
# Wake policy — shared by both agents so behaviour is comparable.
#
# The main agent can also refine this at runtime (a listed good-to-have): its
# decision carries wake guidance that gets fed back into `classify`.
# --------------------------------------------------------------------------

# Events that always justify waking the supervisor immediately.
URGENT_EVENTS = frozenset(
    {
        "payment_failed",
        "shipment_delayed",
        "refund_requested",
        "customer_message_received",
        "delivered",
        "order_cancelled",
        "refund_completed",
    }
)

# Events that are informational: record them, keep sleeping.
ROUTINE_EVENTS = frozenset(
    {"order_created", "payment_confirmed", "shipment_created"}
)


class ScriptedAgent:
    """Deterministic rule-based agent. No network, no clock, no randomness.

    Deliberately opinionated rather than clever: its purpose is to make the
    orchestration observable and testable, not to be smart.
    """

    name = "scripted"

    # -- classifier --------------------------------------------------------
    async def classify(
        self, event: OrderEvent, aggressiveness: str, guidance: str
    ) -> WakeDecision:
        if not event.is_known:
            # Unknown-event escalation (good-to-have): never swallow something
            # we don't understand — wake and let the main agent look at it.
            return WakeDecision(True, f"unknown event '{event.kind}' — escalating")

        if event.kind in URGENT_EVENTS:
            return WakeDecision(True, f"'{event.kind}' is urgent")

        if aggressiveness == "high":
            return WakeDecision(True, "aggressiveness=high wakes on every event")

        if aggressiveness == "low" and event.kind in ROUTINE_EVENTS:
            return WakeDecision(False, f"'{event.kind}' is routine; staying asleep")

        return WakeDecision(
            False, f"'{event.kind}' is routine; will be picked up at next wake-up"
        )

    # -- main inference ----------------------------------------------------
    async def decide(self, payload: AgentInput) -> AgentDecision:
        actions: list[ActionCall] = []
        notes: list[str] = []
        next_wake = 6 * HOUR
        escalated = False
        needs_review = False
        recommends_completion = False

        order = payload.order.order_id
        kinds = [e.kind for e in payload.new_events]

        if payload.trigger == "workflow_start":
            actions.append(
                ActionCall(
                    "create_internal_note",
                    f"Supervisor engaged for order {order}. Watching payment, "
                    f"fulfillment and delivery. Base instruction: "
                    f"{payload.base_instruction[:120]}",
                    "run started",
                )
            )
            notes.append("Run started; baseline established.")
            next_wake = 4 * HOUR

        for event in payload.new_events:
            kind = event.kind

            if kind == "payment_failed":
                actions.append(
                    ActionCall(
                        "message_payments_team",
                        f"Payment failed on order {order}. Please retry the "
                        f"charge and confirm the card status.",
                        "payment_failed",
                    )
                )
                actions.append(
                    ActionCall(
                        "message_customer",
                        "We hit a problem taking payment for your order. "
                        "We're on it and will confirm shortly.",
                        "keep customer informed",
                    )
                )
                notes.append("Payment failed — payments team engaged.")
                escalated = True
                next_wake = min(next_wake, 1 * HOUR)

            elif kind == "shipment_delayed":
                actions.append(
                    ActionCall(
                        "message_logistics_team",
                        f"Shipment for order {order} is delayed. Need a revised "
                        f"ETA and the reason for the delay.",
                        "shipment_delayed",
                    )
                )
                actions.append(
                    ActionCall(
                        "message_customer",
                        "Your delivery is running late. We're chasing the "
                        "carrier and will send you a revised ETA.",
                        "keep customer informed",
                    )
                )
                notes.append("Shipment delayed — logistics engaged.")
                needs_review = True
                next_wake = min(next_wake, 3 * HOUR)

            elif kind == "refund_requested":
                actions.append(
                    ActionCall(
                        "message_payments_team",
                        f"Refund requested on order {order}. Please review "
                        f"eligibility and process if valid.",
                        "refund_requested",
                    )
                )
                actions.append(
                    ActionCall(
                        "message_fulfillment_team",
                        f"Refund requested on order {order} — hold any pending "
                        f"fulfillment.",
                        "prevent shipping a refunded order",
                    )
                )
                notes.append("Refund requested — payments and fulfillment notified.")
                escalated = True
                next_wake = min(next_wake, 2 * HOUR)

            elif kind == "customer_message_received":
                message = event.payload.get("message", "(no content)")
                actions.append(
                    ActionCall(
                        "message_customer",
                        "Thanks for getting in touch — we've picked this up and "
                        "will come back to you shortly.",
                        f"customer said: {message[:80]}",
                    )
                )
                notes.append(f"Customer contacted us: {message[:80]}")
                next_wake = min(next_wake, 2 * HOUR)

            elif kind == "no_update_for_n_hours":
                actions.append(
                    ActionCall(
                        "message_fulfillment_team",
                        f"No movement on order {order} for an extended period. "
                        f"Please confirm status.",
                        "staleness check",
                    )
                )
                notes.append("Order went quiet — fulfillment chased.")
                needs_review = True
                next_wake = min(next_wake, 4 * HOUR)

            elif kind == "payment_confirmed":
                notes.append("Payment confirmed.")
                next_wake = min(next_wake, 8 * HOUR)

            elif kind == "shipment_created":
                notes.append("Shipment created; in transit.")
                next_wake = min(next_wake, 12 * HOUR)

            elif kind == "delivered":
                actions.append(
                    ActionCall(
                        "create_internal_note",
                        f"Order {order} delivered. No further supervision needed.",
                        "delivered",
                    )
                )
                notes.append("Order delivered.")
                recommends_completion = True

            elif kind == "order_created":
                notes.append("Order created.")

            else:
                # Unknown event: record it and flag for a human.
                actions.append(
                    ActionCall(
                        "create_internal_note",
                        f"Unrecognised event '{kind}' on order {order}: "
                        f"{event.payload}. Flagging for human review.",
                        "unknown event",
                    )
                )
                notes.append(f"Unknown event '{kind}' — escalated.")
                escalated = True
                next_wake = min(next_wake, 1 * HOUR)

        if payload.trigger == "scheduled_wakeup" and not payload.new_events:
            notes.append("Scheduled check: nothing new since last review.")
            # Nothing happening — back off, but never past a day.
            next_wake = min(12 * HOUR, DAY)

        # Honour run-specific instructions in a visible, checkable way.
        joined = " ".join(payload.extra_instructions).lower()
        if "escalate immediately" in joined and any(
            k in ("shipment_delayed", "payment_failed") for k in kinds
        ):
            escalated = True
            next_wake = min(next_wake, 30 * MINUTE)
            notes.append("Run instruction: escalating immediately.")
        if "do not contact the customer" in joined:
            dropped = [a for a in actions if a.name == "message_customer"]
            actions = [a for a in actions if a.name != "message_customer"]
            if dropped:
                actions.append(
                    ActionCall(
                        "create_internal_note",
                        "Customer contact suppressed by run instruction; "
                        "queued for human review instead.",
                        "run instruction",
                    )
                )
                needs_review = True
                notes.append("Run instruction: customer contact suppressed.")

        # Respect the supervisor template's enabled action list.
        actions = [a for a in actions if a.name in payload.enabled_actions]

        reasoning = (
            f"Trigger={payload.trigger}. "
            f"New events: {kinds or 'none'}. "
            f"Chose {len(actions)} action(s). "
            f"Next review in {next_wake // 3600}h."
        )

        return AgentDecision(
            reasoning=reasoning,
            actions=actions,
            memory_update=_compact(payload.memory_summary, notes),
            next_wake_seconds=next_wake,
            recommends_completion=recommends_completion,
            escalated=escalated,
            needs_review=needs_review,
        )

    # -- end of run --------------------------------------------------------
    async def finalize(self, payload: AgentInput, reason: str) -> FinalOutput:
        return FinalOutput(
            summary=(
                f"Supervised order {payload.order.order_id} for "
                f"{payload.business_age_seconds // 3600}h of business time. "
                f"Run ended because: {reason}. "
                f"Final memory: {payload.memory_summary or '(none)'}"
            ),
            important_actions=payload.recent_timeline[-10:],
            learnings=[
                "Payment failures and shipment delays were the events that "
                "actually warranted waking the supervisor.",
                "Routine lifecycle events (order_created, payment_confirmed) "
                "were safely deferred to the next scheduled review.",
            ],
            recommendations=[
                "Consider auto-retrying failed payments once before escalating.",
                "A revised-ETA feed from the carrier would remove most "
                "delay-related customer contact.",
            ],
        )


# --------------------------------------------------------------------------
# Memory compaction
#
# Deliberately simple, per the spec ("a sophisticated memory architecture is
# not required"): keep a rolling summary, append new notes, and compact the
# oldest lines into a single digest once it grows past a threshold.
# --------------------------------------------------------------------------

MEMORY_LINE_LIMIT = 12


def _compact(existing: str, new_notes: list[str]) -> str:
    lines = [ln for ln in existing.splitlines() if ln.strip()]
    lines.extend(f"- {n}" for n in new_notes)

    if len(lines) <= MEMORY_LINE_LIMIT:
        return "\n".join(lines)

    keep = lines[-(MEMORY_LINE_LIMIT - 1) :]
    folded = len(lines) - len(keep)
    digest = f"- [compacted {folded} earlier entries]"
    return "\n".join([digest, *keep])
