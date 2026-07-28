"use client";

import { use, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  humanDuration,
  kindStyle,
  type Health,
  type RunDetail,
} from "@/lib/api";

const POLL_MS = 2000;

export default function RunDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);

  const [data, setData] = useState<RunDetail | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [eventKind, setEventKind] = useState("payment_failed");
  const [customerMsg, setCustomerMsg] = useState("Where is my order?");
  const logRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await api.run(id));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [id]);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  // Keep the timeline pinned to the newest entry.
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [data?.activities.length]);

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  }

  if (!data) {
    return (
      <div className="text-sm text-zinc-500">
        {error ? (
          <span className="text-rose-600">{error}</span>
        ) : (
          "Loading run…"
        )}
      </div>
    );
  }

  const { run, activities, live, live_error } = data;
  const completed = run.status === "completed";
  const paused = live?.status === "paused";

  return (
    <div className="space-y-5">
      {/* ---------------------------------------------------------------- */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link href="/" className="text-xs text-zinc-500 hover:text-zinc-900">
            &#8592; all runs
          </Link>
          <h1 className="mt-1 text-xl font-semibold tracking-tight">
            {run.order_id}
          </h1>
          <p className="text-xs text-zinc-500">
            run {run.id} &middot; workflow {run.workflow_id}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={completed ? "rose" : paused ? "amber" : "emerald"}>
            {completed ? "completed" : paused ? "paused" : "running"}
          </Badge>
          {live && !completed && (
            <Badge tone={live.asleep ? "slate" : "sky"}>
              {live.asleep ? "asleep" : "awake"}
            </Badge>
          )}
          {live?.escalated && <Badge tone="rose">escalated</Badge>}
          {live?.needs_review && <Badge tone="amber">needs review</Badge>}
          {live?.agent_recommends_completion && (
            <Badge tone="violet">agent recommends completion</Badge>
          )}
        </div>
      </div>

      {error && (
        <p className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
        {/* -------------------------------------------------- timeline -- */}
        <Card
          title="Timeline & action history"
          right={
            <span className="text-xs text-zinc-500">
              {activities.length} entries
            </span>
          }
        >
          <div
            ref={logRef}
            className="max-h-[540px] space-y-1 overflow-y-auto pr-1"
          >
            {activities.length === 0 && (
              <p className="py-6 text-center text-xs text-zinc-400">
                No activity recorded yet.
              </p>
            )}
            {activities.map((a) => {
              const s = kindStyle(a.kind);
              return (
                <div
                  key={a.id}
                  className="flex gap-2.5 rounded px-2 py-1.5 hover:bg-zinc-50"
                >
                  <span
                    className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${s.dot}`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2">
                      <span className="text-[11px] font-medium uppercase tracking-wide text-zinc-400">
                        {s.label}
                      </span>
                      <span className="truncate text-xs font-medium text-zinc-700">
                        {a.kind}
                      </span>
                    </div>
                    <p className="break-words text-xs leading-relaxed text-zinc-600">
                      {a.detail}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        {/* ----------------------------------------------------- right -- */}
        <div className="space-y-5">
          <Card
            title="Live state"
            right={
              <span className="text-[11px] text-zinc-400">
                from workflow query
              </span>
            }
          >
            {live_error && !completed && (
              <p className="mb-2 rounded bg-amber-50 px-2 py-1.5 text-[11px] text-amber-700">
                Workflow not queryable (worker down, or run finished).
              </p>
            )}
            {live ? (
              <dl className="space-y-1.5 text-xs">
                <Row k="Business age" v={humanDuration(live.business_age_seconds)} />
                <Row
                  k="Next review in"
                  v={humanDuration(live.next_wake_business_seconds)}
                />
                <Row
                  k="Wakes / inferences"
                  v={`${live.wake_count} / ${live.inference_count}`}
                />
                <Row k="Queued events" v={String(live.pending_events)} />
              </dl>
            ) : (
              <p className="text-xs text-zinc-500">
                {completed
                  ? "Run finished — see final output below."
                  : "Unavailable."}
              </p>
            )}

            <div className="mt-3">
              <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
                Memory summary
              </p>
              <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap rounded bg-zinc-50 p-2 text-[11px] leading-relaxed text-zinc-700">
                {live?.memory_summary || run.memory_summary || "(empty)"}
              </pre>
            </div>

            {!!live?.extra_instructions.length && (
              <div className="mt-3">
                <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
                  Run instructions
                </p>
                <ul className="space-y-1">
                  {live.extra_instructions.map((x, i) => (
                    <li
                      key={i}
                      className="rounded bg-violet-50 px-2 py-1 text-[11px] text-violet-800"
                    >
                      {x}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </Card>

          {!completed && (
            <>
              <Card title="Inject an event">
                <select
                  value={eventKind}
                  onChange={(e) => setEventKind(e.target.value)}
                  className="w-full rounded border border-zinc-300 bg-white px-2 py-1.5 text-xs"
                >
                  {(health?.known_events ?? []).map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                  <option value="warehouse_fire">
                    warehouse_fire (unknown event)
                  </option>
                </select>
                {eventKind === "customer_message_received" && (
                  <input
                    value={customerMsg}
                    onChange={(e) => setCustomerMsg(e.target.value)}
                    className="mt-2 w-full rounded border border-zinc-300 px-2 py-1.5 text-xs"
                    placeholder="Customer message"
                  />
                )}
                <Button
                  className="mt-2 w-full"
                  busy={busy === "event"}
                  onClick={() =>
                    act("event", () =>
                      api.injectEvent(
                        id,
                        eventKind,
                        eventKind === "customer_message_received"
                          ? { message: customerMsg }
                          : {},
                      ),
                    )
                  }
                >
                  Send event
                </Button>
              </Card>

              <Card title="Add a run instruction">
                <textarea
                  value={instruction}
                  onChange={(e) => setInstruction(e.target.value)}
                  rows={2}
                  placeholder="e.g. If shipment is delayed, escalate immediately."
                  className="w-full rounded border border-zinc-300 px-2 py-1.5 text-xs"
                />
                <Button
                  className="mt-2 w-full"
                  busy={busy === "instruction"}
                  disabled={!instruction.trim()}
                  onClick={() =>
                    act("instruction", async () => {
                      await api.addInstruction(id, instruction.trim());
                      setInstruction("");
                    })
                  }
                >
                  Add instruction
                </Button>
              </Card>

              <Card title="Controls">
                <div className="grid grid-cols-2 gap-2">
                  <Button
                    busy={busy === "interrupt"}
                    onClick={() => act("interrupt", () => api.interrupt(id))}
                  >
                    Interrupt
                  </Button>
                  {paused ? (
                    <Button
                      busy={busy === "resume"}
                      onClick={() => act("resume", () => api.resume(id))}
                    >
                      Resume
                    </Button>
                  ) : (
                    <Button
                      busy={busy === "pause"}
                      onClick={() => act("pause", () => api.pause(id))}
                    >
                      Pause
                    </Button>
                  )}
                  <Button
                    tone="danger"
                    className="col-span-2"
                    busy={busy === "terminate"}
                    onClick={() =>
                      act("terminate", () =>
                        api.terminate(id, "manually terminated from the UI"),
                      )
                    }
                  >
                    Terminate
                  </Button>
                </div>
                <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">
                  Terminate is a signal, not a kill — the workflow still
                  produces its final summary.
                </p>
              </Card>
            </>
          )}
        </div>
      </div>

      {/* ---------------------------------------------------- final ----- */}
      {run.final_output && (
        <Card
          title="Final output"
          right={
            <span className="text-xs text-zinc-500">
              ended: {run.completion_reason}
            </span>
          }
        >
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-zinc-800">
            {run.final_output.summary}
          </p>
          <div className="mt-4 grid gap-4 md:grid-cols-3">
            <Listing title="Important actions" items={run.final_output.important_actions} />
            <Listing title="Key learnings" items={run.final_output.learnings} />
            <Listing title="Recommendations" items={run.final_output.recommendations} />
          </div>
        </Card>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ ui -- */

function Card({
  title,
  right,
  children,
}: {
  title: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white">
      <div className="flex items-center justify-between border-b border-zinc-100 px-4 py-2.5">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
          {title}
        </h2>
        {right}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-zinc-500">{k}</dt>
      <dd className="font-medium text-zinc-800">{v}</dd>
    </div>
  );
}

function Listing({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
        {title}
      </p>
      <ul className="space-y-1">
        {items.map((x, i) => (
          <li key={i} className="text-xs leading-relaxed text-zinc-700">
            &bull; {x}
          </li>
        ))}
        {items.length === 0 && <li className="text-xs text-zinc-400">-</li>}
      </ul>
    </div>
  );
}

const TONES: Record<string, string> = {
  emerald: "bg-emerald-100 text-emerald-800",
  amber: "bg-amber-100 text-amber-800",
  rose: "bg-rose-100 text-rose-800",
  sky: "bg-sky-100 text-sky-800",
  slate: "bg-slate-100 text-slate-700",
  violet: "bg-violet-100 text-violet-800",
};

function Badge({
  tone = "slate",
  children,
}: {
  tone?: keyof typeof TONES;
  children: React.ReactNode;
}) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

function Button({
  children,
  onClick,
  busy,
  disabled,
  tone = "default",
  className = "",
}: {
  children: React.ReactNode;
  onClick: () => void;
  busy?: boolean;
  disabled?: boolean;
  tone?: "default" | "danger";
  className?: string;
}) {
  const base =
    tone === "danger"
      ? "border-rose-300 bg-rose-50 text-rose-700 hover:bg-rose-100"
      : "border-zinc-300 bg-white text-zinc-800 hover:bg-zinc-50";
  return (
    <button
      onClick={onClick}
      disabled={busy || disabled}
      className={`rounded border px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${base} ${className}`}
    >
      {busy ? "…" : children}
    </button>
  );
}
