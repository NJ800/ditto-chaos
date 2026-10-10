// Thin client for api.py. No business logic lives here -- every decision about what
// is safe to run is made by the backend (orchestrator.py / guard.py); this file only
// shapes HTTP calls and the TypeScript types that mirror the backend's real JSON.

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---------------------------------------------------------------- types
export interface HealthSnapshot {
  window_s: number;
  sample_count: number;
  error_rate: number | null;
  direct_error_rate: number | null;
  search_error_rate: number | null;
  direct_p95_ms: number | null;
  search_p95_ms: number | null;
  stale_p95_s: number | null;
  lost_acked: number | null;
  search_drift: number | null;
  latest_timestamp: number | null;
  fresh: boolean;
  status: "unknown" | "degraded" | "healthy";
  num_cars: number;
  ditto_url: string;
}

export interface MetricRow {
  ts: number;
  direct_ok: boolean;
  direct_ms: number;
  search_ok: boolean;
  search_ms: number;
  stale_p50_s: number | null;
  stale_p95_s: number | null;
  stale_max_s: number | null;
  lost_acked: number | null;
  search_drift: number | null;
  phase?: string;
}

export interface MetricsHistory {
  window_s: number;
  sample_count: number;
  rows: MetricRow[];
  stale_note: string | null;
}

export interface Baseline {
  recorded_at: number;
  duration_s: number;
  num_cars: number;
  samples: number;
  direct_error_rate: number | null;
  search_error_rate: number | null;
  direct_ms: { p50: number | null; p95: number | null; p99: number | null };
  search_ms: { p50: number | null; p95: number | null; p99: number | null };
  stale_p95_s: number | null;
  lost_acked_max: number | null;
  search_drift_max: number | null;
}

export interface Thresholds {
  max_error_rate: number;
  error_rate_window: number;
  max_latency_factor: number;
  min_latency_floor_ms: number;
  max_stale_s: number;
  breach_window: number;
  recovery_latency_factor: number;
  recovery_stale_s: number;
}

export interface BaselineResponse {
  present: boolean;
  baseline: Baseline | null;
}

export interface ServiceInfo {
  name: string;
  container: string;
  image: string;
  state: string;
  running: boolean;
  role: string;
  in_fault_allowlist: boolean;
}

export interface FaultTargetRecommendation {
  service: string;
  fan_in: number;
  fan_out: number;
  calculated_risk: "low" | "medium" | "high";
  allowlisted_for_faults: boolean;
  reasoning: string;
}

export interface DependencyMap {
  generated_at_utc: string;
  compose_project: string;
  docker_query_error: string | null;
  services: ServiceInfo[];
  edges: { from: string; to: string }[];
  fan_in: Record<string, number>;
  fan_out: Record<string, number>;
  fault_target_recommendations: FaultTargetRecommendation[];
}

export interface Proposal {
  proposal_id: string;
  schema_version: string;
  generated_at_utc: string;
  method: "gemini" | "deterministic_fallback";
  fault: "kill" | "delay";
  target: string;
  params: Record<string, number>;
  risk: "low" | "medium" | "high";
  hypothesis: string;
  expected_impact: string;
  reason: string;
  success_criteria: string;
  dependencies: string[];
  max_duration_s: number;
  recovery_timeout_s: number;
  pre_s: number;
}

export interface ExperimentRecord {
  schema_version: string;
  guard_version: string;
  id: string;
  state: string;
  fault: string;
  target: string;
  container: { name: string; id: string };
  params: Record<string, unknown>;
  dry_run: boolean;
  thresholds: Record<string, number>;
  baseline: Record<string, unknown> | null;
  baseline_source: string | null;
  started_at_utc: string;
  ended_at_utc: string | null;
  injected: boolean;
  injection_error: string | null;
  aborted: boolean;
  abort_reason: string | null;
  detected_s: number | null;
  reverted: boolean;
  rollback_verified: boolean;
  rollback_error: string | null;
  recovery_s: number | null;
  recovery_outcome: string | null;
  error: string | null;
  timeline: MetricRow[];
  during: Record<string, unknown> | null;
}

export interface Finding {
  title: string;
  severity: "critical" | "high" | "medium" | "low";
  evidence: string;
  impact: string;
  confidence: "supported_by_measurements" | "plausible_hypothesis" | "insufficient_evidence";
  recommended_fix: string;
}

export interface RootCauseHypothesis {
  hypothesis: string;
  supporting_evidence: string;
  contradicting_evidence: string;
  confidence: string;
  affected_services: string[];
}

export interface Analysis {
  schema_version: string;
  generated_at_utc: string;
  experiment_id: string;
  analysis_method: "gemini" | "deterministic_fallback";
  evidence: Record<string, unknown>;
  summary: string;
  severity: "critical" | "high" | "medium" | "low";
  root_cause_hypotheses: RootCauseHypothesis[];
  findings: Finding[];
  recovery_verified: boolean;
  recommended_next_experiment: string;
}

export interface RemediationItem {
  finding_title: string;
  severity: string;
  confidence: string;
  recommended_fix: string;
  category: "A" | "B" | "C";
  status: string;
  auto_applied: boolean;
  operator_decision: "approved" | "rejected" | "not_applicable_already_automated" | null;
}

export interface RemediationVerification {
  experiment_id: string;
  checked_at_utc: string;
  current_health: HealthSnapshot;
  baseline_direct_p95_ms: number | null;
  verified_healthy_now: boolean;
  note: string;
}

export interface RemediationPlan {
  schema_version: string;
  experiment_id: string;
  generated_at_utc: string;
  analysis_method: string;
  items: RemediationItem[];
  none_auto_applied: boolean;
  policy_note: string;
  verification?: RemediationVerification | null;
}

export interface ExperimentSummary {
  id: string;
  started_at_utc: string;
  ended_at_utc: string | null;
  fault: string;
  target: string;
  state: string;
  aborted: boolean;
  injected: boolean | null;
  detected_s: number | null;
  reverted: boolean;
  rollback_verified: boolean;
  recovery_s: number | null;
  recovery_outcome: string | null;
  error: string | null;
  abort_reason: string | null;
  analyzed: boolean;
}

export interface ExperimentDetail {
  record: ExperimentRecord;
  analysis: Analysis | null;
  remediation_plan: RemediationPlan | null;
}

export interface OrchestratorStatus {
  status: "idle" | "planned" | "running" | "analyzing" | "done" | "error";
  proposal: Proposal | null;
  proposal_id: string | null;
  experiment_id: string | null;
  record: ExperimentRecord | null;
  analysis: Analysis | null;
  remediation_plan: RemediationPlan | null;
  error: string | null;
  started_at: number | null;
}

// ---------------------------------------------------------------- calls
export const api = {
  health: (windowS = 10) => request<HealthSnapshot>(`/api/health?window_s=${windowS}`),
  metricsHistory: (windowS = 600) => request<MetricsHistory>(`/api/metrics/history?window_s=${windowS}`),
  baseline: () => request<BaselineResponse>("/api/baseline"),
  thresholds: () => request<Thresholds>("/api/config/thresholds"),
  dependencies: () => request<DependencyMap>("/api/dependencies"),
  refreshDependencies: () => request<DependencyMap>("/api/dependencies/refresh", { method: "POST" }),

  orchestratorStatus: () => request<OrchestratorStatus>("/api/orchestrator/status"),
  plan: () => request<Proposal>("/api/agents/plan", { method: "POST" }),
  resetOrchestrator: () => request<OrchestratorStatus>("/api/orchestrator/reset", { method: "POST" }),
  runExperiment: (proposalId: string, authorize: boolean, dryRun: boolean) =>
    request<{ status: string }>("/api/experiments/run", {
      method: "POST",
      body: JSON.stringify({ proposal_id: proposalId, authorize, dry_run: dryRun }),
    }),
  stopExperiment: (experimentId: string) =>
    request<{ status: string }>(`/api/experiments/${experimentId}/stop`, { method: "POST" }),

  listExperiments: () => request<ExperimentSummary[]>("/api/experiments"),
  getExperiment: (id: string) => request<ExperimentDetail>(`/api/experiments/${id}`),

  analyzeExperiment: (id: string) =>
    request<{ analysis: Analysis; remediation_plan: RemediationPlan }>(
      `/api/insights/analyze?experiment_id=${encodeURIComponent(id)}`,
      { method: "POST" },
    ),
  latestInsights: () =>
    request<{ analysis: Analysis; remediation_plan: RemediationPlan | null }>("/api/insights/latest"),

  approveRemediation: (experimentId: string, itemIndex: number) =>
    request<RemediationPlan>(`/api/remediations/${experimentId}/${itemIndex}/approve`, { method: "POST" }),
  rejectRemediation: (experimentId: string, itemIndex: number) =>
    request<RemediationPlan>(`/api/remediations/${experimentId}/${itemIndex}/reject`, { method: "POST" }),
  verifyRemediation: (experimentId: string) =>
    request<RemediationVerification>(`/api/remediations/${experimentId}/verify`, { method: "POST" }),
};
