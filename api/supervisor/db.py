"""Postgres access (Supabase), with a shared-file fallback.

Everything here is called from activities or the API, never from workflow code.

If DATABASE_URL is unset the module falls back to a local SQLite file so the
whole system still runs end-to-end for a reviewer who hasn't provisioned a
database. The fallback is loud, not silent — see `storage_mode()`.

The fallback is a FILE, not a dict, on purpose: the worker and the API are
separate processes, so an in-process store would leave the API unable to see
anything the worker's activities wrote.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None

FALLBACK_PATH = Path(__file__).resolve().parent.parent / ".local" / "fallback.db"

_FALLBACK_SCHEMA = """
create table if not exists supervisors (
    id text primary key, name text, base_instruction text,
    enabled_actions text, default_wake_seconds integer, model text,
    classifier_model text, wake_aggressiveness text, time_scale real,
    max_age_seconds integer,
    created_at text default current_timestamp
);
create table if not exists runs (
    id text primary key, supervisor_id text, order_id text, workflow_id text,
    status text default 'running', memory_summary text default '',
    next_wake_at text, next_wake_business_seconds integer default 0,
    business_age_seconds integer default 0, wake_count integer default 0,
    inference_count integer default 0, escalated integer default 0,
    needs_review integer default 0, recommends_completion integer default 0,
    completion_reason text, final_output text, order_context text default '{}',
    created_at text default current_timestamp
);
create table if not exists activities (
    id integer primary key autoincrement, run_id text, kind text,
    detail text default '', created_at text default current_timestamp
);
"""

_JSON_COLUMNS = {"enabled_actions", "final_output", "order_context"}


def _sqlite_sync(sql: str, args: tuple, fetch: str | None):
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(FALLBACK_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_FALLBACK_SCHEMA)
        cur = conn.execute(sql, args)
        if fetch == "all":
            return [dict(r) for r in cur.fetchall()]
        if fetch == "one":
            row = cur.fetchone()
            return dict(row) if row else None
        conn.commit()
        return None
    finally:
        conn.close()


async def _sq(sql: str, *args: Any, fetch: str | None = None):
    """Run a statement against the SQLite fallback, off the event loop."""
    return await asyncio.to_thread(_sqlite_sync, sql, args, fetch)


def database_url() -> str | None:
    url = os.environ.get("DATABASE_URL", "").strip()
    return url or None


def storage_mode() -> str:
    if database_url():
        return "postgres"
    return f"sqlite fallback at {FALLBACK_PATH} (DATABASE_URL unset)"


async def pool() -> asyncpg.Pool | None:
    global _pool
    if not database_url():
        return None
    if _pool is None:
        _pool = await asyncpg.create_pool(
            database_url(), min_size=1, max_size=5, statement_cache_size=0
        )
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# --------------------------------------------------------------------------
# Supervisors
# --------------------------------------------------------------------------


async def upsert_supervisor(sup: dict[str, Any]) -> None:
    values = (
        sup["id"],
        sup["name"],
        sup["base_instruction"],
        json.dumps(sup["enabled_actions"]),
        sup["default_wake_seconds"],
        sup["model"],
        sup["classifier_model"],
        sup["wake_aggressiveness"],
        sup["time_scale"],
        sup["max_age_seconds"],
    )
    p = await pool()
    if p is None:
        await _sq(
            """insert into supervisors (id, name, base_instruction, enabled_actions,
                   default_wake_seconds, model, classifier_model,
                   wake_aggressiveness, time_scale, max_age_seconds)
               values (?,?,?,?,?,?,?,?,?,?)
               on conflict (id) do update set
                   name=excluded.name,
                   base_instruction=excluded.base_instruction,
                   enabled_actions=excluded.enabled_actions,
                   default_wake_seconds=excluded.default_wake_seconds,
                   model=excluded.model,
                   classifier_model=excluded.classifier_model,
                   wake_aggressiveness=excluded.wake_aggressiveness,
                   time_scale=excluded.time_scale,
                   max_age_seconds=excluded.max_age_seconds""",
            *values,
        )
        return
    await p.execute(
        """
        insert into supervisors (id, name, base_instruction, enabled_actions,
            default_wake_seconds, model, classifier_model, wake_aggressiveness,
            time_scale, max_age_seconds)
        values ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10)
        on conflict (id) do update set
            name = excluded.name,
            base_instruction = excluded.base_instruction,
            enabled_actions = excluded.enabled_actions,
            default_wake_seconds = excluded.default_wake_seconds,
            model = excluded.model,
            classifier_model = excluded.classifier_model,
            wake_aggressiveness = excluded.wake_aggressiveness,
            time_scale = excluded.time_scale,
            max_age_seconds = excluded.max_age_seconds
        """,
        *values,
    )


async def list_supervisors() -> list[dict[str, Any]]:
    p = await pool()
    if p is None:
        rows = await _sq("select * from supervisors order by created_at", fetch="all")
        return [_decode(r) for r in rows]
    rows = await p.fetch("select * from supervisors order by created_at")
    return [_decode(dict(r)) for r in rows]


async def get_supervisor(sup_id: str) -> dict[str, Any] | None:
    p = await pool()
    if p is None:
        row = await _sq("select * from supervisors where id = ?", sup_id, fetch="one")
        return _decode(row) if row else None
    row = await p.fetchrow("select * from supervisors where id = $1", sup_id)
    return _decode(dict(row)) if row else None


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


async def create_run(run: dict[str, Any]) -> None:
    values = (
        run["id"],
        run["supervisor_id"],
        run["order_id"],
        run["workflow_id"],
        json.dumps(run.get("order_context", {})),
    )
    p = await pool()
    if p is None:
        await _sq(
            """insert or ignore into runs
               (id, supervisor_id, order_id, workflow_id, order_context)
               values (?,?,?,?,?)""",
            *values,
        )
        return
    await p.execute(
        """
        insert into runs (id, supervisor_id, order_id, workflow_id, order_context)
        values ($1,$2,$3,$4,$5::jsonb)
        on conflict (id) do nothing
        """,
        *values,
    )


async def update_run_state(run_id: str, state: dict[str, Any]) -> None:
    next_wake_raw = state.get("next_wake_at", "") or None
    values = (
        state.get("status", "running"),
        state.get("memory_summary", ""),
        next_wake_raw,
        state.get("next_wake_business_seconds", 0),
        state.get("business_age_seconds", 0),
        state.get("wake_count", 0),
        state.get("inference_count", 0),
        bool(state.get("escalated", False)),
        bool(state.get("needs_review", False)),
        bool(state.get("agent_recommends_completion", False)),
    )
    p = await pool()
    if p is None:
        await _sq(
            """update runs set status=?, memory_summary=?, next_wake_at=?,
                   next_wake_business_seconds=?, business_age_seconds=?,
                   wake_count=?, inference_count=?, escalated=?,
                   needs_review=?, recommends_completion=?
               where id=?""",
            *values,
            run_id,
        )
        return
    # asyncpg needs a real datetime for a timestamptz parameter — an ISO
    # string fails at bind time even with an explicit ::timestamptz cast,
    # because the cast only affects how Postgres types the placeholder, not
    # what asyncpg accepts as the Python value for it.
    next_wake_dt = datetime.fromisoformat(next_wake_raw) if next_wake_raw else None
    await p.execute(
        """
        update runs set
            status = $2, memory_summary = $3,
            next_wake_at = $4::timestamptz,
            next_wake_business_seconds = $5, business_age_seconds = $6,
            wake_count = $7, inference_count = $8, escalated = $9,
            needs_review = $10, recommends_completion = $11, updated_at = now()
        where id = $1
        """,
        run_id,
        values[0],
        values[1],
        next_wake_dt,
        *values[3:],
    )


async def complete_run(run_id: str, final: dict[str, Any], reason: str) -> None:
    p = await pool()
    if p is None:
        await _sq(
            """update runs set status='completed', final_output=?,
                   completion_reason=? where id=?""",
            json.dumps(final),
            reason,
            run_id,
        )
        return
    await p.execute(
        """
        update runs set status = 'completed', final_output = $2::jsonb,
            completion_reason = $3, updated_at = now()
        where id = $1
        """,
        run_id,
        json.dumps(final),
        reason,
    )


async def list_runs() -> list[dict[str, Any]]:
    p = await pool()
    if p is None:
        rows = await _sq("select * from runs order by created_at desc", fetch="all")
        return [_decode(r) for r in rows]
    rows = await p.fetch("select * from runs order by created_at desc")
    return [_decode(dict(r)) for r in rows]


async def get_run(run_id: str) -> dict[str, Any] | None:
    p = await pool()
    if p is None:
        row = await _sq("select * from runs where id = ?", run_id, fetch="one")
        return _decode(row) if row else None
    row = await p.fetchrow("select * from runs where id = $1", run_id)
    return _decode(dict(row)) if row else None


# --------------------------------------------------------------------------
# Activity log
# --------------------------------------------------------------------------


async def append_activity(run_id: str, kind: str, detail: str) -> None:
    p = await pool()
    if p is None:
        await _sq(
            "insert into activities (run_id, kind, detail) values (?,?,?)",
            run_id,
            kind,
            detail,
        )
        return
    await p.execute(
        "insert into activities (run_id, kind, detail) values ($1,$2,$3)",
        run_id,
        kind,
        detail,
    )


async def list_activities(run_id: str) -> list[dict[str, Any]]:
    p = await pool()
    if p is None:
        rows = await _sq(
            "select * from activities where run_id = ? order by id", run_id, fetch="all"
        )
        return [_decode(r) for r in rows]
    rows = await p.fetch(
        "select * from activities where run_id = $1 order by id", run_id
    )
    return [_decode(dict(r)) for r in rows]


# --------------------------------------------------------------------------


def _decode(row: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise a row from either backend into plain JSON-able values."""
    if not row:
        return {}
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif key in _JSON_COLUMNS and isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except (ValueError, TypeError):
                out[key] = value
        elif key in {"escalated", "needs_review", "recommends_completion"}:
            out[key] = bool(value)
        else:
            out[key] = value
    return out
