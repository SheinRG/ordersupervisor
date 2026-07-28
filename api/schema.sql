-- Order Supervisor schema.
-- Applied by hand once (no migration framework for a POC):
--   psql "$DATABASE_URL" -f schema.sql
-- or paste into the Supabase SQL editor.

create table if not exists supervisors (
    id                   text primary key,
    name                 text        not null,
    base_instruction     text        not null,
    enabled_actions      jsonb       not null default '[]'::jsonb,
    default_wake_seconds integer     not null default 21600,
    model                text        not null default 'grok-4.5',
    classifier_model     text        not null default 'grok-4.1-fast',
    wake_aggressiveness  text        not null default 'normal',
    time_scale           double precision not null default 3600,
    max_age_seconds      integer     not null default 2592000,
    created_at           timestamptz not null default now()
);

create table if not exists runs (
    id                         text primary key,
    supervisor_id              text        not null references supervisors (id),
    order_id                   text        not null,
    workflow_id                text        not null,
    status                     text        not null default 'running',
    memory_summary             text        not null default '',
    next_wake_at               timestamptz,
    next_wake_business_seconds integer     not null default 0,
    business_age_seconds       integer     not null default 0,
    wake_count                 integer     not null default 0,
    inference_count            integer     not null default 0,
    escalated                  boolean     not null default false,
    needs_review               boolean     not null default false,
    recommends_completion      boolean     not null default false,
    completion_reason          text,
    final_output               jsonb,
    order_context              jsonb       not null default '{}'::jsonb,
    created_at                 timestamptz not null default now(),
    updated_at                 timestamptz not null default now()
);

-- Single append-only activity log. The spec explicitly allows one table for
-- incoming events, wake decisions, sleep decisions, agent actions, manual
-- instructions and final outputs — `kind` is the discriminator.
create table if not exists activities (
    id         bigserial primary key,
    run_id     text        not null references runs (id) on delete cascade,
    kind       text        not null,
    detail     text        not null default '',
    created_at timestamptz not null default now()
);

create index if not exists activities_run_id_idx on activities (run_id, id);
create index if not exists runs_status_idx on runs (status, created_at desc);
