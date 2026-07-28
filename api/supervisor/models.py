"""Domain types shared by the workflow, the activities, the agents and the API.

Pure dataclasses only — these cross the Temporal serialization boundary, so
they must stay JSON-friendly and free of side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

# The event vocabulary from the assignment. Anything outside this set is
# treated as an unknown event (see UNKNOWN_EVENT_POLICY in the agent).
KNOWN_EVENTS: tuple[str, ...] = (
    "order_created",
    "payment_confirmed",
    "payment_failed",
    "shipment_created",
    "shipment_delayed",
    "delivered",
    "refund_requested",
    "customer_message_received",
    "no_update_for_n_hours",
)

# Events that end the order's lifecycle. The WORKFLOW owns completion — the
# agent may only *recommend* it (see spec v2, "Workflow Completion").
TERMINAL_EVENTS: frozenset[str] = frozenset(
    {"delivered", "order_cancelled", "refund_completed"}
)


@dataclass
class OrderEvent:
    """An event signalled into a running workflow."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    # ISO-8601. Set by the API when the event is accepted, so the workflow
    # never has to read a clock (which would be non-deterministic).
    received_at: str = ""

    @property
    def is_known(self) -> bool:
        return self.kind in KNOWN_EVENTS

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_EVENTS


# --------------------------------------------------------------------------
# Actions — exactly the five the spec (v2) requires, no more.
#
# "escalate" and "mark for review" from the earlier spec draft are expressed
# as flags on the decision instead, so this list stays exactly the five names
# a reviewer will grep for.
# --------------------------------------------------------------------------

ActionName = Literal[
    "message_fulfillment_team",
    "message_payments_team",
    "message_logistics_team",
    "message_customer",
    "create_internal_note",
]

ALL_ACTIONS: tuple[str, ...] = (
    "message_fulfillment_team",
    "message_payments_team",
    "message_logistics_team",
    "message_customer",
    "create_internal_note",
)


@dataclass
class ActionCall:
    """One business action the agent wants executed."""

    name: str
    body: str
    reason: str = ""


# --------------------------------------------------------------------------
# Agent input / output
# --------------------------------------------------------------------------


@dataclass
class OrderContext:
    order_id: str
    customer_name: str = ""
    items: list[str] = field(default_factory=list)
    total: float = 0.0
    currency: str = "USD"


@dataclass
class AgentInput:
    """Everything the agent sees for one inference."""

    trigger: str  # "workflow_start" | "signal" | "scheduled_wakeup"
    order: OrderContext
    base_instruction: str
    extra_instructions: list[str]  # added per-run, after start
    memory_summary: str
    recent_timeline: list[str]  # compacted tail of the timeline
    new_events: list[OrderEvent]
    enabled_actions: list[str]
    wake_aggressiveness: str  # "low" | "normal" | "high"
    # Business seconds elapsed since the run started (already un-scaled, so
    # the agent always reasons in real business time).
    business_age_seconds: int


@dataclass
class AgentDecision:
    """The structured object every agent implementation must return.

    The workflow executes `actions` as Temporal activities, stores
    `memory_update`, and sets a durable timer from `next_wake_seconds`.
    """

    reasoning: str
    actions: list[ActionCall] = field(default_factory=list)
    memory_update: str = ""
    # Business seconds until the agent wants to be woken again.
    next_wake_seconds: int = 3600
    # The agent may RECOMMEND completion. The workflow decides. Logged either
    # way so the recommendation is visible in the UI.
    recommends_completion: bool = False
    escalated: bool = False
    needs_review: bool = False


@dataclass
class WakeDecision:
    """Output of the lightweight classifier for a single incoming event."""

    should_wake: bool
    reason: str


@dataclass
class FinalOutput:
    summary: str
    important_actions: list[str] = field(default_factory=list)
    learnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Supervisor configuration (the template a run is started from)
# --------------------------------------------------------------------------


@dataclass
class SupervisorConfig:
    name: str
    base_instruction: str
    enabled_actions: list[str] = field(default_factory=lambda: list(ALL_ACTIONS))
    # Default sleep when the agent doesn't specify one (business seconds).
    default_wake_seconds: int = 6 * 3600
    model: str = "grok-4.5"
    classifier_model: str = "grok-4.1-fast"
    wake_aggressiveness: str = "normal"
    # Demo compression. The agent reasons in real business durations; the
    # workflow divides every duration by this before setting a timer.
    # 3600 => one business hour elapses in one real second.
    time_scale: float = 3600.0
    # Workflow-owned completion rule: hard cap on business lifetime.
    max_age_seconds: int = 30 * 24 * 3600


# --------------------------------------------------------------------------
# Live state exposed via @workflow.query
# --------------------------------------------------------------------------


@dataclass
class RunState:
    run_id: str
    order_id: str
    status: str  # running | paused | completed
    asleep: bool
    next_wake_at: str  # ISO-8601, real (scaled) time
    next_wake_business_seconds: int
    memory_summary: str
    timeline_length: int
    pending_events: int
    extra_instructions: list[str]
    wake_count: int
    inference_count: int
    agent_recommends_completion: bool
    escalated: bool
    needs_review: bool
    business_age_seconds: int
