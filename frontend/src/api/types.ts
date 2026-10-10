// Types mirroring the backend's JSON (api.py, scenario.py, agents.py, learning.py).
// Anything the backend may omit or not know is typed nullable -- the UI must render
// "unknown", never a zero, for a missing measurement.

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
  stale_p95_s: number | null;
  lost_acked: number | null;
  search_drift: number | null;
  phase?: string;
  active_faults?: number;
  probe_timeout?: boolean;
}

export interface MonitoringStatus {
  state: "ok" | "stale" | "down";
  loop_running: boolean;
  loop_age_s: number | null;
  probe_fresh: boolean;
  probe_age_s: number | null;
  docker_available: boolean | null;
  docker_error: string | null;
  last_error: string | null;
  restarts: number;
}

export interface ServiceRow {
  service: string;
  container: string;
  state: string;
  status_text: string;
  allowlisted: boolean;
  cpu_pct: number | null;
  mem_pct: number | null;
  mem_usage: string | null;
}

export interface ServicesView {
  available: boolean | null;
  error: string | null;
  services: ServiceRow[] | null;
  ts?: number;
}

export interface Percentiles {
  p50: number | null;
  p95: number | null;
  p99: number | null;
}

export interface Baseline {
  recorded_at: number | null;
  direct_ms: Percentiles;
  search_ms: Percentiles;
  stale_p95_s: number | null;
  direct_error_rate: number | null;
  search_error_rate: number | null;
}

export interface Thresholds {
  max_error_rate: number;
  error_rate_window: number;
  max_latency_factor: number;
  min_latency_floor_ms: number;
  max_stale_s: number;
  breach_window: number;
}

// ---- faults / scenarios
export interface ParamSpec {
  name: string;
  minimum: number;
  maximum: number;
  default: number;
  unit: string;
  description: string;
}

export interface FaultSpec {
  name: string;
  title: string;
  description: string;
  category: string;
  params: ParamSpec[];
  expected_symptoms: string[];
  self_expiring: boolean;
  supported: boolean;
  unsupported_reason: string | null;
  live_verified: boolean;
  verification_note: string;
}

export interface FaultCatalog {
  faults: FaultSpec[];
  targets: string[];
  limits: Record<string, number>;
  environment: {
    docker_available: boolean;
    os: string | null;
    netshoot_image_present: boolean | null;
    error: string | null;
  } | null;
}

export interface FaultAction {
  fault: string;
  target: string;
  params: Record<string, number>;
  start_offset_s: number;
  duration_s: number | null;
}

export interface Scenario {
  name?: string;
  hypothesis?: string;
  actions: FaultAction[];
  max_duration_s: number;
  pre_s?: number;
  recovery_timeout_s?: number;
}

export interface PolicyIssue {
  code: string;
  message: string;
  action: number | null;
}

export interface PlannedAction {
  index: number;
  id: string;
  fault: string;
  target: string;
  params: Record<string, number>;
  start_offset_s: number;
  duration_s: number | null;
  end_s: number | null;
  intensity: number | null;
  category: string | null;
  commands: string[];
  live_verified: boolean | null;
}

export interface Prediction {
  signature: string;
  basis: "direct" | "analogy" | "none";
  basis_runs: number;
  basis_experiments: string[];
  outcome_class: string | null;
  symptoms: string[];
  agreement: string | null;
  confidence: "none" | "low" | "medium" | "high";
  statement: string;
  generic_symptoms: string[];
}

export interface ValidationReport {
  ok: boolean;
  errors: PolicyIssue[];
  warnings: PolicyIssue[];
  actions: PlannedAction[];
  affected_services: string[];
  cumulative_intensity: number;
  max_concurrent_faults: number;
  apply_order: string[];
  rollback_order: string[];
  limits: Record<string, number>;
  signature: string;
  prediction?: Prediction | null;
}

export interface PreflightResult {
  ok: boolean;
  error: string | null;
  validation: ValidationReport;
  results: { id: string; fault: string; target: string; ok: boolean; detail: string }[];
}

export interface Proposal {
  proposal_id: string;
  method: string;
  fallback_note: string | null;
  objective: string;
  hypothesis: string;
  target_reason: string;
  expected_impact: string;
  expected_symptoms: string[];
  metrics_to_observe: string[];
  safety_risks: string[];
  stop_conditions: string[];
  preflight_requirements: string[];
  learning_value: string;
  dependencies: string[];
  scenario: Scenario;
  validation: ValidationReport;
  prediction: Prediction;
  confidence: { level: string; basis: string; basis_runs: number; agreement: string | null };
  related_experiments: string[];
  ranking: { rank: number; score: number; components: Record<string, number>; rationale: string[] };
  alternatives: { signature: string; score: number | null; rank: number; blocked: boolean; rationale: string[] }[];
  store_degraded: string | null;
  generated_at_utc: string;
}

// ---- experiment records
export interface ActionState {
  id: string;
  fault: string;
  target: string;
  params: Record<string, number>;
  start_offset_s: number;
  duration_s: number | null;
  status: string;
  applied_at_s: number | null;
  rolled_back_at_s: number | null;
  rollback_verified: boolean;
  error: string | null;
  rollback_error: string | null;
  observed: { active: boolean | null; detail: string } | null;
}

export interface SafetyEvent {
  t_s: number | null;
  ts: number;
  kind: string;
  detail: string;
}

export interface RecoveryInstruction {
  experiment_id: string;
  fault: string | null;
  target: string | null;
  error: string | null;
  manual: string[];
}

export interface ExperimentRecord {
  schema_version: string;
  id: string;
  kind?: string;
  state: string;
  fault: string;
  target: string;
  signature?: string;
  scenario?: Scenario;
  actions?: ActionState[];
  targets?: Record<string, { container: string; running: boolean | null; verified: boolean; detail: string }>;
  authorization?: { kind: string; actor: string; granted_at_utc: string };
  mode?: string | null;
  source?: string | null;
  hypothesis?: string;
  prediction?: Prediction | null;
  dry_run: boolean;
  simulated?: boolean;
  baseline: Record<string, unknown> | null;
  during: Record<string, unknown> | null;
  started_at_utc: string;
  ended_at_utc: string | null;
  started?: number | null;
  injected: boolean;
  aborted: boolean;
  abort_reason: string | null;
  detected_s: number | null;
  reverted: boolean;
  rollback_verified: boolean;
  rollback_error: string | null;
  recovery_s: number | null;
  recovery_outcome: string | null;
  recovery_state?: string;
  error: string | null;
  safety_events?: SafetyEvent[];
  timeline: MetricRow[];
  /** Activity log. Absent on records saved before schema 1.1 / 2.0-with-events. */
  events?: ExperimentEvent[];
  validation?: ValidationVerdict | null;
  recovery_report?: RecoveryReport | null;
  recovery_instructions?: RecoveryInstruction[];
}

export interface ValidationVerdict {
  verdict: "conclusive" | "inconclusive" | "invalid";
  counts_as_evidence: boolean;
  checks: { check: string; ok: boolean }[];
  failed_checks: string[];
  hypothesis_supported: boolean | null;
  returned_to_baseline: boolean | null;
  fault_ticks: number;
}

export interface RecoveryReport {
  recovery_state: string;
  faults_touched: number;
  faults_verified_removed: number;
  stop_reason: string | null;
  detected_s: number | null;
  recovery_s: number | null;
  mutation_blocked: boolean;
  instructions: RecoveryInstruction[];
}

export interface Finding {
  id?: string;
  title: string;
  severity: "critical" | "high" | "medium" | "low";
  evidence: string;
  impact: string;
  confidence: string;
  recommended_fix: string;
  basis?: "measured" | "hypothesis";
  affected_services?: string[];
}

export interface Comparison {
  evaluated: boolean;
  reason?: string;
  predicted_outcome?: string | null;
  observed_outcome?: string;
  outcome_match?: boolean;
  confirmed_symptoms?: string[];
  unexpected_symptoms?: string[];
  predicted_but_absent?: string[];
  basis_runs?: number;
}

export interface Analysis {
  experiment_id: string;
  analysis_method: string;
  summary: string;
  severity: "critical" | "high" | "medium" | "low";
  findings: Finding[];
  root_cause_hypotheses?: { hypothesis: string; supporting_evidence: string; confidence: string }[];
  evidence: Record<string, unknown>;
  windows?: Record<string, Record<string, unknown> | null>;
  observed?: { outcome_class: string; symptoms: string[] };
  hypothesis?: string | null;
  hypothesis_check?: Comparison;
  similar_experiments?: { id: string; signature: string; outcome_class: string; symptoms: string[]; same_scenario: boolean }[];
  limitations?: string[];
  recovery_verified: boolean;
  generated_at_utc: string;
}

export interface LearningRecord {
  experiment_id: string;
  created_at_utc: string;
  signature: string;
  is_evidence: boolean;
  new_evidence: string;
  prediction: Prediction;
  comparison: Comparison;
  related_experiments: string[];
  influence: {
    signature: string;
    score_before: number | null;
    score_after: number | null;
    rank_before: number | null;
    rank_after: number | null;
    top_candidate_before: string | null;
    top_candidate_after: string | null;
    top_candidate_changed: boolean;
  };
  validation_verdict?: string | null;
  evaluation_status?: string;
  next_candidate?: { signature: string; score: number | null; rationale: string[] } | null;
  recommendations_evaluated?: Recommendation[];
  dataset?: { evidence_runs: number; limitation: string };
}

export interface RecommendationItem {
  id: string;
  title: string;
  finding_title: string;
  description: string;
  severity: string;
  priority: number;
  supporting_findings: string[];
  evidence: string;
  evidence_basis?: string;
  proposed_change: string;
  analyst_suggestion: string;
  analyst_suggestion_source: string;
  expected_benefit: string;
  complexity: string;
  risks: string;
  verification_steps: string[];
  recurrence: { prior_runs_with_same_finding: number; prior_runs: number };
}

export interface Recommendation {
  experiment_id: string;
  index: number;
  signature: string;
  title: string;
  severity: string;
  status: "proposed" | "accepted" | "implemented" | "verified" | "rejected" | "not_effective";
  validated_by: string | null;
  validation_note: string | null;
  updated_at_utc: string;
  item: RecommendationItem;
}

export interface AgentRun {
  id: string;
  agent: string;
  title: string;
  version: string;
  kind: string;
  experiment_id: string | null;
  status: string;
  attempts: number;
  started_at_utc: string;
  finished_at_utc: string | null;
  duration_ms: number | null;
  error: string | null;
  summary: string | null;
  method: string | null;
  evidence_ids: string[];
}

export interface AgentStatus {
  name: string;
  title: string;
  role: string;
  version: string;
  kind: string;
  timeout_s: number;
  retries: number;
  state: string;
  task: string | null;
  since: string | null;
  last: Partial<AgentRun> | null;
  last_success_utc: string | null;
  failures: number;
  runs: number;
  recent: AgentRun[];
  provider: { provider: string; model: string; enabled: boolean } | null;
}

export interface TimelineEvent {
  id: number;
  ts: number;
  type: string;
  data: Record<string, unknown>;
}

export interface ExperimentSummary {
  id: string;
  started_at_utc: string | null;
  fault: string | null;
  target: string | null;
  state: string | null;
  aborted: boolean | null;
  injected: boolean | null;
  rollback_verified: boolean | null;
  recovery_s: number | null;
  recovery_outcome: string | null;
  abort_reason: string | null;
  error: string | null;
  kind: string;
  signature: string;
  faults: string[];
  targets: string[];
  simulated: boolean;
  evidence: boolean;
  outcome_class: string;
  symptoms: string[];
  mode: string | null;
  source: string | null;
  validation_verdict: string | null;
  recovery_state: string;
}

export interface ExperimentDetail {
  record: ExperimentRecord;
  analysis: Analysis | null;
  effects: Record<string, unknown>;
  learning: LearningRecord | null;
  recommendations: Recommendation[];
  agent_runs: AgentRun[];
  timeline: TimelineEvent[];
}

export interface OrchestratorStatus {
  status: "idle" | "planned" | "running" | "analyzing" | "done" | "error";
  kind: string;
  experiment_id: string | null;
  record: ExperimentRecord | null;
  analysis: Analysis | null;
  learning: LearningRecord | null;
  agent_runs: AgentRun[] | null;
  scenario_proposal: Proposal | null;
  error: string | null;
  started_at: number | null;
  stop_requested: boolean;
  recovery_state: string;
}

export interface Lockout {
  experiment_id: string | null;
  reason: string;
  created_at_utc: string | null;
  items: RecoveryInstruction[];
}

export type Mode = "observe" | "recommend" | "manual" | "autonomous_sandbox";

export interface ControllerStatus {
  mode: Mode;
  modes: Mode[];
  sandbox_enabled_in_config: boolean;
  policy: {
    allowed_faults: string[];
    allowed_targets: string[];
    max_actions: number;
    max_duration_s: number;
    max_experiments: number;
    cooldown_s: number;
  };
  limits: Record<string, number>;
  loop: {
    running: boolean;
    started_at_utc: string | null;
    cycles: number;
    executed: number;
    last_decision: { signature: string; method: string; rank: number; at_utc: string } | null;
    last_experiment_id: string | null;
    stopped_reason: string | null;
    next_cycle_at: number | null;
    phase: string;
  };
  lockout: Lockout | null;
  auth_required: boolean;
}

export interface SandboxChecks {
  ready: boolean;
  checks: { name: string; ok: boolean; detail: string }[];
}

export interface Snapshot {
  seq: number;
  boot: string;
  server_time: number;
  health: HealthSnapshot;
  monitoring: MonitoringStatus;
  services: ServicesView;
  rows: MetricRow[];
  baseline: Baseline | null;
  thresholds: Thresholds;
  orchestrator: OrchestratorStatus;
  controller: ControllerStatus;
  agents: AgentStatus[];
}

export interface StreamEvent {
  v: number;
  boot: string;
  seq: number;
  ts: number;
  type: string;
  data: Record<string, unknown>;
}

export interface RankedCandidate {
  signature: string;
  actions: { fault: string; target: string }[];
  score: number | null;
  rank: number;
  blocked: boolean;
  runs: number;
  components: Record<string, number>;
  rationale: string[];
}

export interface LearningView {
  summary: {
    total_records: number;
    evidence_runs: number;
    simulated_or_not_injected: number;
    single_fault_coverage: { covered: number; possible: number };
    patterns: {
      signature: string;
      runs: number;
      outcomes: Record<string, number>;
      symptoms: Record<string, number>;
      recurring_symptoms: Record<string, number>;
      median_recovery_s: number | null;
      last_run_utc: string | null;
    }[];
    model: { trained_model: boolean; method: string; reason: string };
    weights: Record<string, number>;
  };
  ranking: RankedCandidate[];
  records: LearningRecord[];
  prediction_accuracy: { evaluated: number; outcome_matched: number; note: string };
  policy_scope: Record<string, unknown> | null;
}

export interface DependencyMap {
  generated_at_utc: string;
  docker_query_error: string | null;
  services: { name: string; container: string; state: string; running: boolean; role: string; in_fault_allowlist: boolean }[];
  edges: { from: string; to: string; note?: string }[];
  fan_in: Record<string, number>;
  fan_out: Record<string, number>;
}

export interface RiskView {
  targets: Record<
    string,
    {
      fan_in: number | null;
      fan_out: number | null;
      blast_radius: string[];
      calculated_risk: string | null;
      evidence_runs: number;
      undertested: boolean;
    }
  >;
  undertested: string[];
  limitations: string[];
}

// ---- activity log (GET /api/experiments/{id}/events)
export type EventKind = "info" | "command" | "health" | "decision" | "warning" | "error";

/** Structured metadata of an event. Which fields are present depends on `kind`. */
export interface EventDetail {
  // command
  command?: string;
  executed?: boolean;
  read_only?: boolean;
  returncode?: number | null;
  stderr?: string;
  timed_out?: boolean;
  duration_ms?: number | null;
  // health (null = the measurement was unavailable)
  direct_ok?: boolean;
  direct_ms?: number | null;
  search_ok?: boolean;
  search_ms?: number | null;
  stale_p95_s?: number | null;
  lost_acked?: number | null;
  search_drift?: number | null;
  probe_timeout?: boolean;
  // decision / outcome
  reason?: string;
  kill_switch?: boolean;
  verified?: boolean;
  final?: boolean;
  [key: string]: unknown;
}

export interface ExperimentEvent {
  seq: number;
  ts: string;
  t_s: number;
  state: string | null;
  kind: EventKind;
  message: string;
  detail: EventDetail;
}

export interface ExperimentEventsResponse {
  experiment_id: string;
  state: string | null;
  running: boolean;
  terminal: boolean;
  dry_run: boolean;
  last_seq: number;
  events: ExperimentEvent[];
}
