# Order Supervisor

A long-running AI supervisor that watches a single order from creation to
completion. One Temporal workflow per order; events arrive as signals; the
agent reasons, acts through business actions, updates memory, sleeps, and
wakes again — on a schedule or when something important happens.

**Stack:** Next.js (App Router) + Tailwind · FastAPI · Temporal Python SDK ·
PostgreSQL (Supabase) · xAI Grok or Groq (pluggable, see `AGENT_MODE` below)

---

## Quick start

### Prerequisites

| Tool | Install |
|---|---|
| Temporal CLI | `winget install Temporal.TemporalCLI` (or `brew install temporal`) |
| uv | https://docs.astral.sh/uv/ |
| Node 20+ | https://nodejs.org |

### 1. Configure

```bash
cp api/.env.example api/.env
```

Nothing in `.env` is required to run the system. With an empty file you get the
**scripted agent** (deterministic, no network, no cost) and a **local SQLite
file** for storage. Fill it in to enable the real LLM and Postgres:

| Variable | Effect if unset |
|---|---|
| `AGENT_MODE` | defaults to `scripted`. Also accepts `grok` (xAI) or `groq` (groq.com — note these are different companies) |
| `XAI_API_KEY` | required only when `AGENT_MODE=grok` |
| `GROQ_API_KEY` | required only when `AGENT_MODE=groq`. On Groq, structured outputs (`response_format: json_schema`) only work on the `openai/gpt-oss-*` models today — see `api/.env.example` |
| `DATABASE_URL` | falls back to `api/.local/fallback.db` (SQLite) |

For Postgres, apply the schema once:

```bash
psql "$DATABASE_URL" -f api/schema.sql
# or paste api/schema.sql into the Supabase SQL editor
```

> **Supabase tip:** use the **Session pooler** connection URI, not the direct
> one. The direct host is IPv6-only on the free tier and frequently
> unreachable from home networks.

### 2. Install

```bash
cd api && uv sync
cd ../web && npm install
```

### 3. Run — four terminals

```bash
# 1. Temporal server (+ Web UI on :8233)
temporal server start-dev --db-filename .temporal/dev.db

# 2. Worker — hosts the workflow and activity code
cd api && uv run python worker.py

# 3. API
cd api && uv run uvicorn app.main:app --port 8000

# 4. UI
cd web && npm run dev
```

Then open **http://localhost:3000** (the app), and
**http://localhost:8233** (Temporal's own UI — worth having open, it shows the
durable timers firing).

> If port 3000 is taken: `npm run dev -- --port 3002`. The API accepts any
> localhost origin.

> **One worker at a time.** If you restart the worker, kill the old process
> first — two workers polling the same task queue will split work between them
> and make behaviour look non-deterministic.

### 4. Verify without the UI

```bash
cd api && uv run python smoke.py
```

Drives a full run end to end — start, routine event, urgent event, run
instruction, pause, resume, terminal event, final summary.

---

## What it does

- **One workflow per order.** Workflow id is `order-{order_id}`, so starting a
  second supervisor for the same order is rejected by the server.
- **Three inference triggers:** workflow start, an important signal, and a
  scheduled wake-up.
- **A lightweight classifier gates the main agent.** Every incoming event wakes
  the workflow cheaply; only events the classifier judges important cause a
  (more expensive) main-agent inference. Everything else is recorded and
  deferred to the next scheduled review.
- **Five business actions**, each producing an activity record:
  `message_fulfillment_team`, `message_payments_team`, `message_logistics_team`,
  `message_customer`, `create_internal_note`.
- **Memory + timeline** with simple rolling compaction.
- **Run-specific instructions** can be added after the workflow has started and
  become part of its context.
- **Completion is workflow-owned** — a terminal event, a manual terminate, or a
  max-age rule. The agent may *recommend* completion; it cannot cause it.

### Demo time compression

Real supervision sleeps for hours. Every supervisor template carries a
`time_scale`: the agent reasons in real business durations ("check back in 6
hours") and the workflow divides by `time_scale` before setting the timer. At
the default `3600`, six business hours elapse in six real seconds.

> At `time_scale=3600` the 30-day max-age rule fires after ~12 real minutes.
> That's handy for demonstrating the rule, but don't leave a run parked
> mid-recording and be surprised when it completes.

---

## Layout

```
api/
  supervisor/
    workflow.py     OrderSupervisorWorkflow — signals, sleep/wake, completion
    activities.py   every side effect: agent calls, actions, persistence
    agent.py        Agent protocol + ScriptedAgent (deterministic)
    grok.py         GrokAgent — xAI with JSON-schema structured outputs
    groq.py         GroqAgent — Groq (groq.com) with JSON-schema structured outputs
    models.py       shared dataclasses
    db.py           Postgres, with a SQLite fallback
  app/main.py       FastAPI
  worker.py         Temporal worker
  smoke.py          end-to-end test
  tracer.py         throwaway teaching example — read this first
  schema.sql
web/
  app/page.tsx                runs list
  app/runs/[id]/page.tsx      run detail — timeline, controls, event injector
  app/supervisors/page.tsx    supervisor templates
docs/architecture.md
```

**New to Temporal? Read `api/tracer.py` first.** It's ~60 lines of throwaway
code containing every concept the real workflow uses — activity, signal, query,
and the durable timer — with nothing else in the way.

---

## API

| Method | Path |
|---|---|
| `GET` | `/api/health` |
| `GET/POST` | `/api/supervisors` |
| `GET` | `/api/supervisors/{id}` |
| `GET/POST` | `/api/runs` |
| `GET` | `/api/runs/{run_id}` |
| `POST` | `/api/runs/{run_id}/events` |
| `POST` | `/api/runs/{run_id}/instructions` |
| `POST` | `/api/runs/{run_id}/interrupt` |
| `POST` | `/api/runs/{run_id}/pause` |
| `POST` | `/api/runs/{run_id}/resume` |
| `POST` | `/api/runs/{run_id}/terminate` |

`GET /api/runs/{run_id}` returns history from Postgres **and** live state
queried directly from the running workflow.

---

## Known limitations

- Single-node Temporal dev server; no auth; no multi-tenancy. All out of scope
  per the assignment.
- The classifier runs once per event rather than per batch — simpler to read,
  slightly more calls.
- Memory compaction is line-count based, not token-based.
