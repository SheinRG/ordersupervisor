# Architecture note

## The shape of the problem

An order supervisor is mostly *waiting*. It watches one order for days, does
almost nothing most of the time, and must react within minutes when something
goes wrong. It also has to survive process restarts without losing its place.

That combination — long-lived, mostly idle, event-driven, must not lose state —
is what Temporal exists for, and it drives every decision below.

```
   Next.js UI  ──HTTP──▶  FastAPI  ──signals──▶  ┌──────────────────────┐
       ▲                     │                   │  Temporal Server     │
       │                     │  queries          │  history + timers    │
       │                     └──────────────────▶└───────────┬──────────┘
       │                                                     │ dispatch
       │                                         ┌───────────▼──────────┐
       └──────── history ◀── Postgres ◀──────────│  Worker              │
                                    activities   │  workflow + activity │
                                                 │  code, agent runtime │
                                                 └──────────────────────┘
```

The server never runs your code — it stores history and fires timers. The
worker is the process that actually executes the workflow and activities.

---

## One workflow per order

Each run starts a workflow whose id is `order-{order_id}`. Temporal rejects a
second execution with the same id, so "exactly one supervisor per order" is an
invariant enforced by the platform rather than by application logic. The API
surfaces the rejection as a `409`.

## Sleeping and waking

The entire wake/sleep design is one construct:

```python
try:
    await workflow.wait_condition(
        lambda: bool(self._pending) or self._interrupt or self._terminate,
        timeout=timedelta(seconds=remaining),
    )
    trigger = "signal"            # woke early
except asyncio.TimeoutError:
    trigger = "scheduled_wakeup"  # timer fired
```

This is a **durable timer**. While it waits, the workflow holds no thread, no
memory and no database connection — it is evicted from the worker entirely and
the *server* wakes it. Sleeping for six hours costs exactly as much as sleeping
for six seconds, and a worker restart mid-sleep changes nothing.

It also covers all three required inference triggers with one code path:
workflow start (before the loop), an incoming signal (early wake), and the
scheduled wake-up (timeout).

### The classifier gate

The workflow wakes on **every** signal, because that's cheap and local. Whether
the **agent** runs is a separate decision, made by a lightweight classifier:

```
signal arrives → workflow wakes → record event → classifier
                                                   ├─ wake  → main agent runs
                                                   └─ defer → recompute
                                                              remaining sleep,
                                                              go back to sleep
```

Deferring re-sleeps for the *remaining* interval rather than restarting it, so
a stream of routine events can't indefinitely postpone a scheduled review.

The classifier is a cheaper model (`grok-4.1-fast`) than the main agent
(`grok-4.5`). Unknown event types always wake the agent — never silently
swallow something the system doesn't understand.

## Determinism: why all I/O is an activity

Workflow code is *replayed* to rebuild state after an eviction or restart. If
it called an LLM directly, replay would call the LLM again and diverge. So
every side effect — LLM calls, Postgres writes, business actions — is an
`@activity.defn`. Temporal records each activity's **result** in history and
reuses it on replay.

This pays off twice. Business actions are individually retried and individually
visible in the event history; and LLM rate limits become free to handle:

```python
_LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_attempts=6,
)
```

A `429` from a free-tier provider is retried with backoff by the platform while
the workflow simply stays parked. There is no retry code anywhere in the
business logic.

## Completion is workflow-owned

The agent returns `recommends_completion`, and the workflow **logs it and
carries on**. A run ends only when:

1. a terminal event arrives (`delivered`, `order_cancelled`, `refund_completed`),
2. an operator terminates it from the UI, or
3. the configured max business age is reached.

Termination from the UI is a *signal*, not `handle.terminate()`, so the
workflow still runs its final step and produces the end-of-run summary. A hard
terminate would discard exactly the output the assignment asks for.

## Memory and timeline

- **Timeline** — an append-only activity log in Postgres. One table with a
  `kind` discriminator covers events, wake decisions, sleep decisions, agent
  actions, instructions and final output.
- **Memory** — a compact rolling summary the agent rewrites on each inference.
  Once it exceeds twelve lines, the oldest entries fold into a single
  `[compacted N earlier entries]` digest.

The workflow also keeps a bounded in-memory timeline tail (40 entries) for the
agent's context. Full history lives in Postgres; the workflow never needs it.

## Reads are split on purpose

| Data | Source | Why |
|---|---|---|
| Timeline, action history | Postgres | unbounded, historical, survives completion |
| Memory, sleep state, next wake, counters | `@workflow.query` | the workflow's own live belief, no write-lag |

Querying the workflow keeps the UI honest: it shows what the supervisor
actually thinks right now, not a possibly-stale projection.

## The agent seam

Everything that reasons sits behind one protocol:

```python
class Agent(Protocol):
    async def classify(self, event, aggressiveness, guidance) -> WakeDecision
    async def decide(self, payload: AgentInput) -> AgentDecision
    async def finalize(self, payload: AgentInput, reason) -> FinalOutput
```

Three implementations: `ScriptedAgent` (deterministic rules, no network),
`GrokAgent` (xAI) and `GroqAgent` (Groq — groq.com, a different company from
xAI despite the name), both using JSON-schema structured outputs. `AGENT_MODE`
selects one at worker startup.

This is not just testing hygiene. It means the orchestration can be developed
and demonstrated without spending a token, the system stays demonstrable if a
provider is rate-limited, and a reviewer with no API key can still run it.

`GrokAgent`/`GroqAgent` use strict JSON-schema structured outputs, so the
decision object is guaranteed to match the shape the workflow executes — no
defensive JSON repair. The workflow additionally clamps `next_wake_seconds` to
[5 min, 7 days] and filters actions against the template's enabled list,
because a schema guarantees *shape*, not *sense*.

Structured-output generation can still fail validation occasionally (observed
on Groq's smaller models under strict mode) — a real LLM is not a type
checker. `activities.py` catches this: on the *last* retry attempt for an
agent call, instead of failing the whole workflow it falls back to
`ScriptedAgent`, so a run always produces a valid decision and, critically, a
final summary — never a `WorkflowExecutionFailed` with no end-of-run output.

## Demo time compression

The agent reasons in business time; the workflow converts to real time:

```python
self._next_wake_at = workflow.now() + timedelta(
    seconds=business_seconds / config.time_scale
)
```

The alternative — having the agent pick artificially short sleeps — would make
its reasoning incoherent on camera ("I'll check back in 8 seconds"). This way
the agent's output stays realistic and only the clock is scaled.

## `continue_as_new`

An order supervised for months would accumulate an unbounded event history.
Past a history-length threshold the workflow calls `continue_as_new`, carrying
memory, timeline tail, instructions, business-age offset and the *remaining*
sleep into a fresh execution. Same workflow id, same logical run, bounded
history.

---

## Trade-offs taken

| Decision | Alternative | Why this one |
|---|---|---|
| Wake workflow on every signal, gate the *agent* | Classify inside the signal handler | Signal handlers should not block or do I/O; gating in the run loop keeps handlers trivial |
| Classifier per event | Classify the batch | Simpler to read and to explain; slightly more calls |
| Single `activities` table | Separate events / messages / actions tables | The spec explicitly allows it, and a single ordered log is what the UI actually renders |
| SQLite fallback | Require Postgres | Keeps the system runnable with zero credentials — the fallback is a *file*, not a dict, so worker and API share it |
| Terminate via signal | `handle.terminate()` | Preserves the final summary |
| No migration framework | Alembic | One schema version, two days |

## What I'd do next

- Token-based memory compaction instead of line-count.
- Agent-authored wake guidance fed back into the classifier (started: the
  classifier already receives the memory summary as `guidance`).
- Batch classification to cut call volume.
- Per-run metrics: time asleep vs awake, cost per run, actions per event type.

## Productionising

The only structural change is configuration: point the client at Temporal Cloud
(namespace + mTLS), run the worker as a container behind an autoscaler, and put
the API and UI on any host. Nothing in the workflow, activity or agent code
changes — the dev server and Temporal Cloud speak the same API. Postgres is
already remote.
