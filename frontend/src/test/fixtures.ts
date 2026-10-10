import type { ControllerStatus, ExperimentRecord, ExperimentSummary, MetricRow, OrchestratorStatus, Snapshot } from "../api/types";
import { applySnapshot, initialState, type LiveClient, type LiveState } from "../live/stream";

export const row = (ts: number, over: Partial<MetricRow> = {}): MetricRow => ({
  ts,
  direct_ok: true,
  direct_ms: 20,
  search_ok: true,
  search_ms: 25,
  stale_p95_s: 1,
  lost_acked: 0,
  search_drift: 0,
  ...over,
});

export const controller = (over: Partial<ControllerStatus> = {}): ControllerStatus => ({
  mode: "manual",
  modes: ["observe", "recommend", "manual", "autonomous_sandbox"],
  sandbox_enabled_in_config: false,
  policy: { allowed_faults: ["kill", "delay"], allowed_targets: ["things"], max_actions: 2, max_duration_s: 90, max_experiments: 3, cooldown_s: 60 },
  limits: { max_active_faults: 3, max_affected_services: 2, max_duration_s: 300, max_cumulative_intensity: 1.6 },
  loop: { running: false, started_at_utc: null, cycles: 0, executed: 0, last_decision: null, last_experiment_id: null, stopped_reason: null, next_cycle_at: null, phase: "idle" },
  lockout: null,
  auth_required: false,
  ...over,
});

export const orchestrator = (over: Partial<OrchestratorStatus> = {}): OrchestratorStatus => ({
  status: "idle",
  kind: "single",
  experiment_id: null,
  record: null,
  analysis: null,
  learning: null,
  agent_runs: null,
  scenario_proposal: null,
  error: null,
  started_at: null,
  stop_requested: false,
  recovery_state: "RECOVERY_NOT_REQUIRED",
  ...over,
});

export const record = (over: Partial<ExperimentRecord> = {}): ExperimentRecord => ({
  schema_version: "2.0",
  id: "exp-1",
  kind: "scenario",
  state: "MONITORING",
  fault: "pause",
  target: "things",
  signature: "pause@things",
  actions: [
    { id: "a1", fault: "pause", target: "things", params: {}, start_offset_s: 0, duration_s: null, status: "applied", applied_at_s: 0, rolled_back_at_s: null, rollback_verified: false, error: null, rollback_error: null, observed: null },
  ],
  authorization: { kind: "operator", actor: "alice", granted_at_utc: "2026-10-10T10:00:00+00:00" },
  dry_run: false,
  baseline: null,
  during: null,
  started_at_utc: "2026-10-10T10:00:00+00:00",
  ended_at_utc: null,
  injected: true,
  aborted: false,
  abort_reason: null,
  detected_s: null,
  reverted: false,
  rollback_verified: false,
  rollback_error: null,
  recovery_s: null,
  recovery_outcome: null,
  error: null,
  timeline: [],
  ...over,
});

export const snapshot = (nowS: number, over: Partial<Snapshot> = {}): Snapshot => ({
  seq: 10,
  boot: "boot-a",
  server_time: nowS,
  health: {
    window_s: 10,
    sample_count: 5,
    error_rate: 0,
    direct_error_rate: 0,
    search_error_rate: 0,
    direct_p95_ms: 42,
    search_p95_ms: 55,
    stale_p95_s: 1.2,
    lost_acked: 0,
    search_drift: 0,
    latest_timestamp: nowS - 1,
    fresh: true,
    status: "healthy",
    num_cars: 100,
    ditto_url: "http://localhost:8080",
  },
  monitoring: { state: "ok", loop_running: true, loop_age_s: 1, probe_fresh: true, probe_age_s: 1, docker_available: true, docker_error: null, last_error: null, restarts: 0 },
  services: { available: true, error: null, services: [{ service: "things", container: "docker-things-1", state: "running", status_text: "Up 2 hours", allowlisted: true, cpu_pct: 3.2, mem_pct: 20, mem_usage: "400MiB / 2GiB" }] },
  rows: [row(nowS - 4), row(nowS - 2)],
  baseline: { recorded_at: nowS - 600, direct_ms: { p50: 20, p95: 40, p99: 60 }, search_ms: { p50: 20, p95: 50, p99: 70 }, stale_p95_s: 1, direct_error_rate: 0, search_error_rate: 0 },
  thresholds: { max_error_rate: 0.3, error_rate_window: 10, max_latency_factor: 3, min_latency_floor_ms: 200, max_stale_s: 15, breach_window: 3 },
  orchestrator: orchestrator(),
  controller: controller(),
  agents: [],
  ...over,
});

export function liveState(over: Partial<LiveState> = {}, snap?: Snapshot): LiveState {
  const now = Date.now();
  return { ...applySnapshot(initialState, snap ?? snapshot(now / 1000), now), conn: "live", ...over };
}

/** A LiveClient stand-in holding a fixed state, for rendering pages. */
export function fakeClient(state: LiveState) {
  const calls = { refresh: 0, reconnect: 0, controller: [] as ControllerStatus[] };
  const client = {
    getState: () => state,
    subscribe: () => () => {},
    start: () => {},
    stop: () => {},
    refresh: () => void calls.refresh++,
    reconnectNow: () => void calls.reconnect++,
    setController: (c: ControllerStatus) => void calls.controller.push(c),
  } as unknown as LiveClient;
  return { client, calls };
}

export const summary = (over: Partial<ExperimentSummary> = {}): ExperimentSummary => ({
  id: "exp-1",
  started_at_utc: "2026-10-10T10:00:00+00:00",
  fault: "kill",
  target: "things-search",
  state: "COMPLETED",
  aborted: false,
  injected: true,
  rollback_verified: true,
  recovery_s: 4.2,
  recovery_outcome: "recovered",
  abort_reason: null,
  error: null,
  kind: "scenario",
  signature: "kill@things-search",
  faults: ["kill"],
  targets: ["things-search"],
  simulated: false,
  evidence: true,
  outcome_class: "degraded",
  symptoms: ["search_errors"],
  mode: "manual",
  source: "operator",
  validation_verdict: "conclusive",
  recovery_state: "RECOVERED",
  ...over,
});
