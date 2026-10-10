// HTTP client for api.py. No decision about what is safe to run is made here or
// anywhere in the frontend -- the backend validates and authorizes every request.
import type {
  AgentStatus,
  CandidatesView,
  ControllerStatus,
  DependencyMap,
  ExperimentDetail,
  ExperimentEventsResponse,
  ExperimentSummary,
  FaultCatalog,
  LearningView,
  Lockout,
  Mode,
  OrchestratorStatus,
  PolicyIssue,
  PreflightResult,
  Proposal,
  Recommendation,
  RerunPlan,
  RiskView,
  SandboxChecks,
  Scenario,
  Snapshot,
  ValidationReport,
} from "./types";

// Development (vite on :5173) talks to the API on :8000. A production build is served by
// the API itself (`python main.py serve`), so it uses the page's own origin.
export const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.DEV ? "http://127.0.0.1:8000" : "");
const TOKEN_KEY = "ditto-chaos.api-token";
const DEFAULT_TIMEOUT_MS = 15000;

export class ApiError extends Error {
  status: number;
  code: string;
  errors: PolicyIssue[];
  constructor(status: number, message: string, code = "error", errors: PolicyIssue[] = []) {
    super(message);
    this.status = status;
    this.code = code;
    this.errors = errors;
  }
}

export function getToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the token simply is not remembered */
  }
}

/** A user-facing sentence for any thrown value. */
export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 401) return "Not authorized: enter the API token (Settings, top right).";
    if (e.errors.length) return `${e.message}: ${e.errors.map((x) => x.message).join("; ")}`;
    return e.message;
  }
  if (e instanceof DOMException && e.name === "AbortError") return "The request was cancelled.";
  if (e instanceof Error) return e.message;
  return "Unexpected error.";
}

interface Options {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export async function request<T>(path: string, opts: Options = {}): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, opts.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const onAbort = () => controller.abort();
  opts.signal?.addEventListener("abort", onAbort);
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["X-API-Token"] = token; // required on every /api route when the server has API_TOKEN set
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: controller.signal,
    });
  } catch (e) {
    if (timedOut) throw new ApiError(0, `The backend did not answer within ${(opts.timeoutMs ?? DEFAULT_TIMEOUT_MS) / 1000}s.`, "timeout");
    if (opts.signal?.aborted) throw e;
    throw new ApiError(0, `Cannot reach the backend at ${BASE_URL || window.location.origin}. Is \`python main.py serve\` running?`, "unreachable");
  } finally {
    clearTimeout(timer);
    opts.signal?.removeEventListener("abort", onAbort);
  }
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    let code = "error";
    let errors: PolicyIssue[] = [];
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg?: string; loc?: unknown[] }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
      if (typeof body.code === "string") code = body.code;
      if (Array.isArray(body.errors)) errors = body.errors;
    } catch {
      /* non-JSON error body: keep the status text */
    }
    throw new ApiError(res.status, detail, code, errors);
  }
  return (await res.json()) as T;
}

const post = <T>(path: string, body?: unknown, timeoutMs?: number) => request<T>(path, { method: "POST", body: body ?? {}, timeoutMs });

export const api = {
  snapshot: (signal?: AbortSignal) => request<Snapshot>("/api/snapshot?window_s=900", { signal }),
  status: (signal?: AbortSignal) => request<OrchestratorStatus>("/api/orchestrator/status", { signal }),
  agents: (signal?: AbortSignal) => request<{ agents: AgentStatus[]; pipelines: { plan: string[]; post_run: string[] } }>("/api/agents", { signal }),
  controller: (signal?: AbortSignal) => request<ControllerStatus>("/api/controller", { signal }),

  faults: (signal?: AbortSignal) => request<FaultCatalog>("/api/faults", { signal }),
  validate: (scenario: Scenario) => post<ValidationReport>("/api/scenarios/validate", scenario),
  preflight: (scenario: Scenario) => post<PreflightResult>("/api/scenarios/preflight", scenario, 90000),
  plan: () => post<Proposal>("/api/plan", undefined, 90000),
  candidates: (signal?: AbortSignal) => request<CandidatesView>("/api/plan/candidates", { signal, timeoutMs: 30000 }),
  selectCandidate: (signature: string) => post<Proposal>("/api/plan/select", { signature }),
  rerunPlan: (experimentId: string, index: number, signal?: AbortSignal) =>
    request<RerunPlan>(`/api/experiments/${encodeURIComponent(experimentId)}/rerun-plan?recommendation_index=${index}`, { signal }),
  rerun: (experimentId: string, body: { recommendation_index: number; authorize: boolean; confirm_applied: boolean; note: string; request_id: string }) =>
    post<{ status: string; experiment_id: string; original_id: string; duplicate: boolean }>(`/api/experiments/${encodeURIComponent(experimentId)}/rerun`, body, 60000),
  runScenario: (body: { authorize: boolean; dry_run?: boolean; scenario?: Scenario; proposal_id?: string; request_id: string }) =>
    post<{ status: string; experiment_id: string; duplicate: boolean }>("/api/scenarios/run", body, 60000),
  events: (id: string, afterSeq: number, signal?: AbortSignal) =>
    request<ExperimentEventsResponse>(`/api/experiments/${encodeURIComponent(id)}/events?after_seq=${afterSeq}`, { signal }),
  stop: () => post<{ status: string }>("/api/stop"),
  reset: () => post<OrchestratorStatus>("/api/orchestrator/reset"),

  setMode: (mode: Mode) => post<ControllerStatus>("/api/controller/mode", { mode }),
  sandbox: (signal?: AbortSignal) => request<SandboxChecks>("/api/controller/sandbox", { signal, timeoutMs: 60000 }),
  startAutonomous: () => post<ControllerStatus>("/api/controller/autonomous/start", undefined, 60000),
  stopAutonomous: (stopExperiment: boolean) => post<ControllerStatus>("/api/controller/autonomous/stop", { stop_experiment: stopExperiment }),

  recovery: (signal?: AbortSignal) => request<{ lockout: Lockout | null; recovery_state: string }>("/api/recovery", { signal }),
  resolveRecovery: (force: boolean, note: string) =>
    post<{ lockout: Lockout | null; recovered: unknown[]; failed: unknown[] }>("/api/recovery/resolve", { force, note }, 120000),

  experiments: (signal?: AbortSignal) => request<ExperimentSummary[]>("/api/experiments", { signal }),
  experiment: (id: string, signal?: AbortSignal) => request<ExperimentDetail>(`/api/experiments/${encodeURIComponent(id)}`, { signal }),

  learning: (signal?: AbortSignal) => request<LearningView>("/api/learning", { signal }),
  recommendations: (signal?: AbortSignal) => request<Recommendation[]>("/api/recommendations", { signal }),
  setRecommendation: (experimentId: string, index: number, status: string, note = "") =>
    post<Recommendation>(`/api/recommendations/${encodeURIComponent(experimentId)}/${index}/status`, { status, note }),

  dependencies: (signal?: AbortSignal) => request<DependencyMap>("/api/dependencies", { signal }),
  refreshDependencies: () => post<DependencyMap>("/api/dependencies/refresh", undefined, 60000),
  risk: (signal?: AbortSignal) => request<RiskView>("/api/risk", { signal }),
};

export function streamUrl(lastSeq: number, boot: string, ticket?: string | null): string {
  const t = ticket ? `&ticket=${encodeURIComponent(ticket)}` : "";
  return `${BASE_URL}/api/stream?last_seq=${lastSeq}&boot=${encodeURIComponent(boot)}${t}`;
}

/** EventSource cannot send headers, so with token auth the stream is opened with a
 *  single-use ticket obtained here. Returns null when no token is configured. */
export async function streamTicket(signal?: AbortSignal): Promise<string | null> {
  if (!getToken()) return null;
  const res = await request<{ ticket: string }>("/api/stream/ticket", { method: "POST", body: {}, signal });
  return res.ticket;
}
