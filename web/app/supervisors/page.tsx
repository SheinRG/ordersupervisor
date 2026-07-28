"use client";

import { useCallback, useEffect, useState } from "react";
import { api, humanDuration, type Supervisor } from "@/lib/api";

const ALL_ACTIONS = [
  "message_fulfillment_team",
  "message_payments_team",
  "message_logistics_team",
  "message_customer",
  "create_internal_note",
];

export default function SupervisorsPage() {
  const [list, setList] = useState<Supervisor[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [name, setName] = useState("My Supervisor");
  const [instruction, setInstruction] = useState(
    "Watch this order end to end. Escalate payment problems quickly and keep the customer informed.",
  );
  const [actions, setActions] = useState<string[]>([...ALL_ACTIONS]);
  const [aggressiveness, setAggressiveness] = useState("normal");
  const [model, setModel] = useState("grok-4.5");
  const [classifierModel, setClassifierModel] = useState("grok-4.1-fast");
  const [wakeHours, setWakeHours] = useState(6);
  const [timeScale, setTimeScale] = useState(3600);

  const refresh = useCallback(async () => {
    try {
      setList(await api.supervisors());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function save() {
    setSaving(true);
    try {
      await api.createSupervisor({
        name,
        base_instruction: instruction,
        enabled_actions: actions,
        wake_aggressiveness: aggressiveness,
        model,
        classifier_model: classifierModel,
        default_wake_seconds: Math.round(wakeHours * 3600),
        time_scale: timeScale,
      });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  function toggle(a: string) {
    setActions((prev) =>
      prev.includes(a) ? prev.filter((x) => x !== a) : [...prev, a],
    );
  }

  return (
    <div className="space-y-5">
      {error && (
        <p className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </p>
      )}

      <section className="rounded-lg border border-zinc-200 bg-white">
        <div className="border-b border-zinc-100 px-4 py-2.5">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Define a supervisor
          </h2>
        </div>
        <div className="grid gap-4 p-4 md:grid-cols-2">
          <Field label="Name">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded border border-zinc-300 px-2 py-1.5 text-xs"
            />
          </Field>

          <Field label="Wake aggressiveness">
            <select
              value={aggressiveness}
              onChange={(e) => setAggressiveness(e.target.value)}
              className="w-full rounded border border-zinc-300 bg-white px-2 py-1.5 text-xs"
            >
              <option value="low">low — only urgent events</option>
              <option value="normal">normal</option>
              <option value="high">high — wake on everything</option>
            </select>
          </Field>

          <Field label="Base instruction" className="md:col-span-2">
            <textarea
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              rows={3}
              className="w-full rounded border border-zinc-300 px-2 py-1.5 text-xs"
            />
          </Field>

          <Field label="Available actions" className="md:col-span-2">
            <div className="flex flex-wrap gap-2">
              {ALL_ACTIONS.map((a) => (
                <label
                  key={a}
                  className={`cursor-pointer rounded border px-2 py-1 text-[11px] transition ${
                    actions.includes(a)
                      ? "border-zinc-800 bg-zinc-900 text-white"
                      : "border-zinc-300 bg-white text-zinc-600"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={actions.includes(a)}
                    onChange={() => toggle(a)}
                    className="hidden"
                  />
                  {a}
                </label>
              ))}
            </div>
          </Field>

          <Field label="Model (main agent)">
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full rounded border border-zinc-300 px-2 py-1.5 text-xs"
            />
          </Field>

          <Field label="Model (wake-up classifier)">
            <input
              value={classifierModel}
              onChange={(e) => setClassifierModel(e.target.value)}
              className="w-full rounded border border-zinc-300 px-2 py-1.5 text-xs"
            />
          </Field>

          <Field label="Default wake-up (business hours)">
            <input
              type="number"
              min={1}
              value={wakeHours}
              onChange={(e) => setWakeHours(Number(e.target.value))}
              className="w-full rounded border border-zinc-300 px-2 py-1.5 text-xs"
            />
          </Field>

          <Field label="Demo time scale (business secs per real sec)">
            <input
              type="number"
              min={1}
              value={timeScale}
              onChange={(e) => setTimeScale(Number(e.target.value))}
              className="w-full rounded border border-zinc-300 px-2 py-1.5 text-xs"
            />
            <p className="mt-1 text-[11px] text-zinc-500">
              At {timeScale}&times;, a {wakeHours}h sleep takes{" "}
              {Math.round((wakeHours * 3600) / timeScale)}s in real time.
            </p>
          </Field>

          <div className="md:col-span-2">
            <button
              onClick={save}
              disabled={saving || !actions.length}
              className="rounded bg-zinc-900 px-4 py-1.5 text-xs font-medium text-white transition hover:bg-zinc-700 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save supervisor"}
            </button>
          </div>
        </div>
      </section>

      <section className="rounded-lg border border-zinc-200 bg-white">
        <div className="border-b border-zinc-100 px-4 py-2.5">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Templates <span className="text-zinc-400">({list.length})</span>
          </h2>
        </div>
        <div className="divide-y divide-zinc-100">
          {list.map((s) => (
            <div key={s.id} className="px-4 py-3">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="text-sm font-medium text-zinc-900">
                  {s.name}
                </span>
                <span className="text-[11px] text-zinc-400">{s.id}</span>
                <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] text-zinc-600">
                  wake: {s.wake_aggressiveness}
                </span>
                <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] text-zinc-600">
                  every {humanDuration(s.default_wake_seconds)}
                </span>
                <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] text-zinc-600">
                  {s.time_scale}&times; time
                </span>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-zinc-600">
                {s.base_instruction}
              </p>
              <p className="mt-1 text-[11px] text-zinc-400">
                {s.enabled_actions.join(" · ")}
              </p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function Field({
  label,
  children,
  className = "",
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <label className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-400">
        {label}
      </label>
      {children}
    </div>
  );
}
