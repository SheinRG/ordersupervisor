"""OrderSupervisorWorkflow — one long-running workflow per order.

Design notes (also in docs/architecture.md):

* ONE workflow execution per order, with a deterministic id `order-{order_id}`,
  so starting it twice is rejected by the server and we get idempotency free.
* Events arrive as SIGNALS. The workflow wakes on every signal (cheap, local),
  but the MAIN AGENT only runs if the lightweight classifier says the event
  warrants it — otherwise the workflow goes straight back to sleep for the
  remainder of its scheduled interval.
* Sleeping is a single `wait_condition(..., timeout=...)`. That is a durable
  timer: the workflow holds no thread, no memory and no connection while it
  waits, and the Temporal server wakes it.
* ALL I/O — Postgres writes, LLM calls, business actions — happens in
  activities. Workflow code must stay deterministic so it can be replayed.
* COMPLETION IS WORKFLOW-OWNED. The agent may set `recommends_completion`, but
  only a terminal event, a manual terminate, or the configured max age
  actually ends the run (spec v2, "Workflow Completion").
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import (
        agent_classify,
        agent_decide,
        agent_finalize,
        execute_action,
        persist_activity,
        persist_final_output,
        persist_run_state,
    )
    from .models import (
        AgentInput,
        FinalOutput,
        OrderContext,
        OrderEvent,
        RunState,
        SupervisorConfig,
    )

# Continue-as-new once history gets long, so a very long-lived order doesn't
# accumulate an unbounded event history (a listed good-to-have).
HISTORY_LENGTH_THRESHOLD = 2_000

# Keep the in-workflow timeline bounded; older entries live in Postgres.
TIMELINE_TAIL = 40

_SHORT = timedelta(seconds=20)
_LLM = timedelta(seconds=120)

# LLM calls are the flaky part (free-tier rate limits). Temporal retries the
# ACTIVITY with backoff while the workflow simply stays parked — no bespoke
# retry code anywhere in the business logic.
_LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=6,
)


@dataclass
class RunParams:
    """Workflow input. Also the carrier across continue_as_new."""

    run_id: str
    order: OrderContext
    config: SupervisorConfig
    # --- carried state (empty on a fresh run) ---
    memory_summary: str = ""
    timeline: list[str] = field(default_factory=list)
    extra_instructions: list[str] = field(default_factory=list)
    business_age_offset: int = 0
    next_wake_business_seconds: int = 0
    wake_count: int = 0
    inference_count: int = 0
    escalated: bool = False
    needs_review: bool = False
    is_continuation: bool = False


@workflow.defn
class OrderSupervisorWorkflow:
    def __init__(self) -> None:
        self._pending: list[OrderEvent] = []
        self._instructions: list[str] = []
        self._paused = False
        self._interrupt = False
        self._terminate = False
        self._terminate_reason = ""
        self._terminal_event: str | None = None

        self._memory = ""
        self._timeline: list[str] = []
        self._next_wake_at: datetime | None = None
        self._next_wake_business = 0
        self._wake_count = 0
        self._inference_count = 0
        self._recommends_completion = False
        self._escalated = False
        self._needs_review = False

        self._params: RunParams | None = None
        self._started_at: datetime | None = None
        self._age_offset = 0

    # ------------------------------------------------------------------
    # Signals — the only way in. Handlers stay trivial: mutate state and
    # return. The run loop notices and does the work.
    # ------------------------------------------------------------------

    @workflow.signal
    async def deliver_event(self, event: OrderEvent) -> None:
        self._pending.append(event)

    @workflow.signal
    async def add_instruction(self, instruction: str) -> None:
        self._instructions.append(instruction)
        self._timeline.append(f"instruction added: {instruction}")

    @workflow.signal
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._paused = False

    @workflow.signal
    async def interrupt(self) -> None:
        """Force an inference right now, cancelling the current sleep."""
        self._interrupt = True

    @workflow.signal
    async def terminate_run(self, reason: str) -> None:
        self._terminate = True
        self._terminate_reason = reason or "manually terminated from the UI"

    # ------------------------------------------------------------------
    # Query — live state, straight out of the running workflow. No DB.
    # ------------------------------------------------------------------

    @workflow.query
    def state(self) -> RunState:
        params = self._params
        return RunState(
            run_id=params.run_id if params else "",
            order_id=params.order.order_id if params else "",
            status="paused" if self._paused else "running",
            asleep=not self._pending and not self._interrupt,
            next_wake_at=self._next_wake_at.isoformat() if self._next_wake_at else "",
            next_wake_business_seconds=self._next_wake_business,
            memory_summary=self._memory,
            timeline_length=len(self._timeline),
            pending_events=len(self._pending),
            extra_instructions=list(self._instructions),
            wake_count=self._wake_count,
            inference_count=self._inference_count,
            agent_recommends_completion=self._recommends_completion,
            escalated=self._escalated,
            needs_review=self._needs_review,
            business_age_seconds=self._business_age(),
        )

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, params: RunParams) -> FinalOutput:
        self._params = params
        self._started_at = workflow.now()
        self._age_offset = params.business_age_offset
        self._memory = params.memory_summary
        self._timeline = list(params.timeline)
        self._instructions = list(params.extra_instructions)
        self._wake_count = params.wake_count
        self._inference_count = params.inference_count
        self._escalated = params.escalated
        self._needs_review = params.needs_review

        cfg = params.config

        if params.is_continuation:
            # Resume the sleep that was in flight when we continued-as-new.
            self._schedule(params.next_wake_business_seconds)
        else:
            await self._log("run started", f"supervising order {params.order.order_id}")
            await self._infer("workflow_start", [])

        while True:
            reason = self._completion_reason(cfg)
            if reason:
                return await self._finish(reason)

            trigger = await self._sleep_until_something_happens()
            self._wake_count += 1

            if self._terminate:
                return await self._finish(self._terminate_reason)

            if self._paused:
                await self._log("paused", "inference suppressed; events still queue")
                await workflow.wait_condition(
                    lambda: not self._paused or self._terminate
                )
                if self._terminate:
                    return await self._finish(self._terminate_reason)
                await self._log("resumed", "draining queued events")
                trigger = "signal"

            batch, self._pending = self._pending, []
            forced = self._interrupt
            self._interrupt = False

            for event in batch:
                await self._log(f"event: {event.kind}", str(event.payload))
                if event.is_terminal:
                    self._terminal_event = event.kind

            should_infer = (
                trigger == "scheduled_wakeup"
                or forced
                or self._terminal_event is not None
            )

            # The lightweight classifier gate: does this batch justify waking
            # the MAIN agent right now, or can it wait for the next review?
            if not should_infer and batch:
                should_infer = await self._classify(batch, cfg)

            if should_infer:
                await self._infer(
                    "scheduled_wakeup" if trigger == "scheduled_wakeup" else "signal",
                    batch,
                )
            elif batch:
                await self._log(
                    "stayed asleep",
                    f"{len(batch)} event(s) recorded; deferred to next review",
                )

            if self._should_continue_as_new():
                await self._log(
                    "continue-as-new",
                    "history threshold reached; carrying memory into a fresh run",
                )
                workflow.continue_as_new(self._carry())

    # ------------------------------------------------------------------
    # Sleep
    # ------------------------------------------------------------------

    async def _sleep_until_something_happens(self) -> str:
        """Park until a signal lands or the scheduled wake-up is due."""
        now = workflow.now()
        if self._next_wake_at is None:
            self._schedule(self._params.config.default_wake_seconds)

        remaining = (self._next_wake_at - now).total_seconds()
        if remaining <= 0:
            return "scheduled_wakeup"

        try:
            # THE durable timer. Wakes early on a signal, otherwise fires.
            await workflow.wait_condition(
                lambda: bool(self._pending) or self._interrupt or self._terminate,
                timeout=timedelta(seconds=remaining),
            )
            return "signal"
        except asyncio.TimeoutError:
            return "scheduled_wakeup"

    def _schedule(self, business_seconds: int) -> None:
        """Convert business time to real time via the demo time_scale."""
        scale = max(self._params.config.time_scale, 1e-9)
        self._next_wake_business = business_seconds
        self._next_wake_at = workflow.now() + timedelta(
            seconds=business_seconds / scale
        )

    def _business_age(self) -> int:
        if not self._started_at or not self._params:
            return self._age_offset
        elapsed = (workflow.now() - self._started_at).total_seconds()
        return int(self._age_offset + elapsed * self._params.config.time_scale)

    # ------------------------------------------------------------------
    # Agent calls (all via activities)
    # ------------------------------------------------------------------

    async def _classify(self, batch: list[OrderEvent], cfg: SupervisorConfig) -> bool:
        for event in batch:
            decision = await workflow.execute_activity(
                agent_classify,
                args=[event, cfg.wake_aggressiveness, self._memory],
                start_to_close_timeout=_LLM,
                retry_policy=_LLM_RETRY,
            )
            await self._log(
                "wake decision",
                f"{event.kind}: {'WAKE' if decision.should_wake else 'stay asleep'}"
                f" — {decision.reason}",
            )
            if decision.should_wake:
                return True
        return False

    async def _infer(self, trigger: str, batch: list[OrderEvent]) -> None:
        payload = self._agent_input(trigger, batch)

        decision = await workflow.execute_activity(
            agent_decide,
            payload,
            start_to_close_timeout=_LLM,
            retry_policy=_LLM_RETRY,
        )

        self._inference_count += 1
        self._memory = decision.memory_update or self._memory
        self._escalated = self._escalated or decision.escalated
        self._needs_review = self._needs_review or decision.needs_review
        self._recommends_completion = decision.recommends_completion

        await self._log("agent reasoning", decision.reasoning)

        # Execute each business action as its own activity, so every action is
        # independently retried and independently visible in the history.
        for action in decision.actions:
            result = await workflow.execute_activity(
                execute_action,
                args=[self._params.run_id, action],
                start_to_close_timeout=_SHORT,
            )
            # execute_action already wrote the activity record; only mirror it
            # into the in-workflow timeline (used for memory + final output).
            self._timeline.append(f"action: {action.name} {result}")

        if decision.recommends_completion:
            # Recorded, deliberately NOT obeyed. Completion is workflow-owned.
            await self._log(
                "agent recommends completion",
                "noted; workflow continues until a lifecycle rule fires",
            )

        self._schedule(decision.next_wake_seconds or self._params.config.default_wake_seconds)
        await self._log(
            "sleeping",
            f"next review in {decision.next_wake_seconds // 3600}h business time",
        )
        await self._save()

    def _agent_input(self, trigger: str, batch: list[OrderEvent]) -> AgentInput:
        cfg = self._params.config
        return AgentInput(
            trigger=trigger,
            order=self._params.order,
            base_instruction=cfg.base_instruction,
            extra_instructions=list(self._instructions),
            memory_summary=self._memory,
            recent_timeline=self._timeline[-TIMELINE_TAIL:],
            new_events=batch,
            enabled_actions=list(cfg.enabled_actions),
            wake_aggressiveness=cfg.wake_aggressiveness,
            business_age_seconds=self._business_age(),
        )

    # ------------------------------------------------------------------
    # Completion — workflow-owned rules only
    # ------------------------------------------------------------------

    def _completion_reason(self, cfg: SupervisorConfig) -> str | None:
        if self._terminate:
            return self._terminate_reason
        if self._terminal_event:
            return f"terminal event '{self._terminal_event}' received"
        if self._business_age() >= cfg.max_age_seconds:
            return (
                f"max workflow age reached "
                f"({cfg.max_age_seconds // 86400}d of business time)"
            )
        return None

    async def _finish(self, reason: str) -> FinalOutput:
        await self._log("completing", reason)
        final = await workflow.execute_activity(
            agent_finalize,
            args=[self._agent_input("completion", []), reason],
            start_to_close_timeout=_LLM,
            retry_policy=_LLM_RETRY,
        )
        await workflow.execute_activity(
            persist_final_output,
            args=[self._params.run_id, final, reason],
            start_to_close_timeout=_SHORT,
        )
        return final

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _log(self, kind: str, detail: str) -> None:
        self._timeline.append(f"{kind}: {detail}")
        await workflow.execute_activity(
            persist_activity,
            args=[self._params.run_id, kind, detail],
            start_to_close_timeout=_SHORT,
        )

    async def _save(self) -> None:
        await workflow.execute_activity(
            persist_run_state,
            args=[self._params.run_id, self.state()],
            start_to_close_timeout=_SHORT,
        )

    # ------------------------------------------------------------------
    # continue_as_new
    # ------------------------------------------------------------------

    def _should_continue_as_new(self) -> bool:
        return workflow.info().get_current_history_length() >= HISTORY_LENGTH_THRESHOLD

    def _carry(self) -> RunParams:
        remaining = 0
        if self._next_wake_at:
            scale = self._params.config.time_scale
            secs = (self._next_wake_at - workflow.now()).total_seconds()
            remaining = max(int(secs * scale), 0)
        return RunParams(
            run_id=self._params.run_id,
            order=self._params.order,
            config=self._params.config,
            memory_summary=self._memory,
            timeline=self._timeline[-TIMELINE_TAIL:],
            extra_instructions=list(self._instructions),
            business_age_offset=self._business_age(),
            next_wake_business_seconds=remaining,
            wake_count=self._wake_count,
            inference_count=self._inference_count,
            escalated=self._escalated,
            needs_review=self._needs_review,
            is_continuation=True,
        )
