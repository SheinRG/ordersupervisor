"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  api,
  humanDuration,
  type Health,
  type Run,
  type Supervisor,
} from "@/lib/api";

export default function RunsPage() {
  const router = useRouter();
  const [runs, setRuns] = useState<Run[]>([]);
  const [supervisors, setSupervisors] = useState<Supervisor[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const [supervisorId, setSupervisorId] = useState("default");
  const [customer, setCustomer] = useState("Priya Raman");
  const [items, setItems] = useState("Mechanical keyboard, USB-C hub");

  const refresh = useCallback(async () => {
    try {
      setRuns(await api.runs());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch((e) => setError(String(e)));
    api.supervisors().then(setSupervisors).catch(() => {});
    refresh();
    const t = setInterval(refresh, 2500);
    return () => clearInterval(t);
  }, [refresh]);

  async function start() {
    setStarting(true);
    try {
      const res = await api.createRun({
        supervisor_id: supervisorId,
        order: {
          customer_name: customer,
          items: items
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        },
      });
      router.push(`/runs/${res.run_id}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setStarting(false);
    }
  }

  const active = runs.filter((r) => r.status !== "completed");
  const done = runs.filter((r) => r.status === "completed");

  return (
    <div className="space-y-5">
      {health && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded-lg border border-zinc-200 bg-white px-4 py-2.5 text-xs">
          <Stat label="Agent" value={health.agent} />
          <Stat
            label="Temporal"
            value={health.temporal_connected ? "connected" : "unreachable"}
            bad={!health.temporal_connected}
          />
          <Stat
            label="Storage"
            value={
              health.storage.startsWith("postgres")
                ? "postgres"
                : "sqlite fallback"
            }
            bad={!health.storage.startsWith("postgres")}
          />
        </div>
      )}

      {error && (
        <p className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error} &mdash; is the API running on :8000?
        </p>
      )}

      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Start a run for an order
        </h2>
        <div className="grid gap-3 sm:grid-cols-[200px_1fr_1fr_auto]">
          <select
            value={supervisorId}
            onChange={(e) => setSupervisorId(e.target.value)}
            className="rounded border border-zinc-300 bg-white px-2 py-1.5 text-xs"
          >
            {supervisors.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
          <input
            value={customer}
            onChange={(e) => setCustomer(e.target.value)}
            placeholder="Customer name"
            className="rounded border border-zinc-300 px-2 py-1.5 text-xs"
          />
          <input
            value={items}
            onChange={(e) => setItems(e.target.value)}
            placeholder="Items (comma separated)"
            className="rounded border border-zinc-300 px-2 py-1.5 text-xs"
          />
          <button
            onClick={start}
            disabled={starting}
            className="rounded bg-zinc-900 px-4 py-1.5 text-xs font-medium text-white transition hover:bg-zinc-700 disabled:opacity-50"
          >
            {starting ? "Starting…" : "Start supervisor"}
          </button>
        </div>
      </section>

      <RunTable title="Active runs" runs={active} empty="No active runs." />
      <RunTable
        title="Completed runs"
        runs={done}
        empty="Nothing completed yet."
      />
    </div>
  );
}

function RunTable({
  title,
  runs,
  empty,
}: {
  title: string;
  runs: Run[];
  empty: string;
}) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white">
      <div className="border-b border-zinc-100 px-4 py-2.5">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
          {title} <span className="text-zinc-400">({runs.length})</span>
        </h2>
      </div>
      {runs.length === 0 ? (
        <p className="px-4 py-5 text-xs text-zinc-400">{empty}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-[11px] uppercase tracking-wide text-zinc-400">
              <tr>
                <th className="px-4 py-2 font-medium">Order</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Business age</th>
                <th className="px-4 py-2 font-medium">Inferences</th>
                <th className="px-4 py-2 font-medium">Flags</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.id}
                  className="border-t border-zinc-100 hover:bg-zinc-50"
                >
                  <td className="px-4 py-2 font-medium text-zinc-800">
                    {r.order_id}
                  </td>
                  <td className="px-4 py-2 text-zinc-600">{r.status}</td>
                  <td className="px-4 py-2 text-zinc-600">
                    {humanDuration(r.business_age_seconds)}
                  </td>
                  <td className="px-4 py-2 text-zinc-600">
                    {r.inference_count}
                  </td>
                  <td className="px-4 py-2">
                    <span className="flex gap-1">
                      {r.escalated && (
                        <span className="rounded bg-rose-100 px-1.5 py-0.5 text-[10px] text-rose-700">
                          escalated
                        </span>
                      )}
                      {r.needs_review && (
                        <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">
                          review
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Link
                      href={`/runs/${r.id}`}
                      className="font-medium text-zinc-900 hover:underline"
                    >
                      open &#8594;
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Stat({
  label,
  value,
  bad,
}: {
  label: string;
  value: string;
  bad?: boolean;
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="text-zinc-400">{label}</span>
      <span
        className={bad ? "font-medium text-amber-700" : "font-medium text-zinc-800"}
      >
        {value}
      </span>
    </span>
  );
}
