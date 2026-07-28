const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type Supervisor = {
  id: string;
  name: string;
  base_instruction: string;
  enabled_actions: string[];
  default_wake_seconds: number;
  model: string;
  classifier_model: string;
  wake_aggressiveness: string;
  time_scale: number;
  max_age_seconds: number;
};

export type Run = {
  id: string;
  supervisor_id: string;
  order_id: string;
  workflow_id: string;
  status: string;
  memory_summary: string;
  next_wake_at: string | null;
  business_age_seconds: number;
  wake_count: number;
  inference_count: number;
  escalated: boolean;
  needs_review: boolean;
  recommends_completion: boolean;
  completion_reason: string | null;
  final_output: FinalOutput | null;
  order_context: Record<string, unknown>;
  created_at: string;
};

export type FinalOutput = {
  summary: string;
  important_actions: string[];
  learnings: string[];
  recommendations: string[];
};

export type Activity = {
  id: number;
  run_id: string;
  kind: string;
  detail: string;
  created_at: string;
};

export type LiveState = {
  run_id: string;
  order_id: string;
  status: string;
  asleep: boolean;
  next_wake_at: string;
  next_wake_business_seconds: number;
  memory_summary: string;
  timeline_length: number;
  pending_events: number;
  extra_instructions: string[];
  wake_count: number;
  inference_count: number;
  agent_recommends_completion: boolean;
  escalated: boolean;
  needs_review: boolean;
  business_age_seconds: number;
};

export type RunDetail = {
  run: Run;
  activities: Activity[];
  live: LiveState | null;
  live_error: string | null;
};

export type Health = {
  ok: boolean;
  storage: string;
  agent: string;
  temporal_connected: boolean;
  known_events: string[];
  actions: string[];
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${body}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => req<Health>("/api/health"),

  supervisors: () => req<Supervisor[]>("/api/supervisors"),
  createSupervisor: (body: Partial<Supervisor>) =>
    req<Supervisor>("/api/supervisors", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  runs: () => req<Run[]>("/api/runs"),
  run: (id: string) => req<RunDetail>(`/api/runs/${id}`),
  createRun: (body: {
    supervisor_id: string;
    order: {
      order_id?: string;
      customer_name?: string;
      items?: string[];
      total?: number;
    };
  }) =>
    req<{ run_id: string; order_id: string; workflow_id: string }>("/api/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  injectEvent: (id: string, kind: string, payload: Record<string, unknown> = {}) =>
    req(`/api/runs/${id}/events`, {
      method: "POST",
      body: JSON.stringify({ kind, payload }),
    }),
  addInstruction: (id: string, instruction: string) =>
    req(`/api/runs/${id}/instructions`, {
      method: "POST",
      body: JSON.stringify({ instruction }),
    }),
  interrupt: (id: string) => req(`/api/runs/${id}/interrupt`, { method: "POST" }),
  pause: (id: string) => req(`/api/runs/${id}/pause`, { method: "POST" }),
  resume: (id: string) => req(`/api/runs/${id}/resume`, { method: "POST" }),
  terminate: (id: string, reason: string) =>
    req(`/api/runs/${id}/terminate`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
};

/** Business seconds -> a human duration, e.g. 21600 -> "6h". */
export function humanDuration(seconds: number): string {
  if (!seconds || seconds < 0) return "-";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h`;
  if (m) return `${m}m`;
  return `${seconds}s`;
}

/** Colour-code an activity row by its `kind` discriminator. */
export function kindStyle(kind: string): { dot: string; label: string } {
  if (kind.startsWith("action:")) return { dot: "bg-emerald-500", label: "action" };
  if (kind.startsWith("event:")) return { dot: "bg-sky-500", label: "event" };
  if (kind === "wake decision") return { dot: "bg-amber-500", label: "classifier" };
  if (kind === "agent reasoning") return { dot: "bg-violet-500", label: "agent" };
  if (kind === "sleeping") return { dot: "bg-slate-400", label: "sleep" };
  if (kind === "stayed asleep") return { dot: "bg-slate-400", label: "sleep" };
  if (kind === "final output") return { dot: "bg-rose-500", label: "final" };
  if (kind === "completing") return { dot: "bg-rose-500", label: "final" };
  return { dot: "bg-zinc-400", label: "info" };
}
