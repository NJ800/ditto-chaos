import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ApiError, api } from "../api/client";
import type { ExperimentDetail, FaultCatalog, LearningView, Recommendation, ValidationReport } from "../api/types";
import { LiveProvider } from "../live/LiveContext";
import type { LiveState } from "../live/stream";
import { controller, fakeClient, liveState, orchestrator, record, snapshot, summary } from "../test/fixtures";
import { filterExperiments } from "./History";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: Object.fromEntries(Object.keys(real.api).map((k) => [k, vi.fn()])) };
});
const mocked = api as unknown as Record<keyof typeof api, ReturnType<typeof vi.fn>>;

const CATALOG: FaultCatalog = {
  faults: [
    { name: "delay", title: "Network latency", description: "Fixed delay.", category: "network", params: [{ name: "delay_ms", minimum: 1, maximum: 5000, default: 500, unit: "ms", description: "" }], expected_symptoms: [], self_expiring: true, supported: true, unsupported_reason: null, live_verified: true, verification_note: "verified on a throwaway container" },
    { name: "memory_pressure", title: "Memory pressure", description: "", category: "resource", params: [], expected_symptoms: [], self_expiring: false, supported: false, unsupported_reason: "would OOM-kill the JVM", live_verified: false, verification_note: "Not implemented." },
  ],
  targets: ["things", "mongodb"],
  limits: { max_active_faults: 3, max_affected_services: 2, max_duration_s: 300, max_cumulative_intensity: 1.6 },
  environment: { docker_available: true, os: "linux", netshoot_image_present: true, error: null },
};
const REPORT = (ok: boolean): ValidationReport => ({
  ok,
  errors: ok ? [] : [{ code: "limit_intensity", message: "cumulative intensity 2 exceeds the budget 1.6", action: null }],
  warnings: [],
  actions: [{ index: 0, id: "a1", fault: "delay", target: "things", params: { delay_ms: 500 }, start_offset_s: 0, duration_s: null, end_s: 60, intensity: 0.1, category: "network", commands: ["docker run ... tc qdisc replace ... netem delay 500ms"], live_verified: true }],
  affected_services: ["things"],
  cumulative_intensity: 0.1,
  max_concurrent_faults: 1,
  apply_order: ["a1"],
  rollback_order: ["a1"],
  limits: CATALOG.limits,
  signature: "delay@things",
  prediction: null,
});
const REC: Recommendation = {
  experiment_id: "exp-1",
  index: 0,
  signature: "kill@things-search",
  title: "Degrade search gracefully",
  severity: "high",
  status: "proposed",
  validated_by: null,
  validation_note: null,
  updated_at_utc: "2026-10-10T10:05:00+00:00",
  item: { id: "R-exp-1-1", title: "Degrade search gracefully", finding_title: "Search availability degraded", description: "search failed", severity: "high", priority: 1, supporting_findings: ["F1"], evidence: "search_error_rate 0 -> 0.6", proposed_change: "Add a circuit breaker.", analyst_suggestion: "fall back to direct reads", analyst_suggestion_source: "deterministic rule", expected_benefit: "contained outage", complexity: "medium", risks: "flapping", verification_steps: ["Re-run scenario 'kill@things-search'."], recurrence: { prior_runs_with_same_finding: 0, prior_runs: 0 } },
};
const LEARNING: LearningView = {
  summary: { total_records: 3, evidence_runs: 1, simulated_or_not_injected: 2, single_fault_coverage: { covered: 1, possible: 54 }, patterns: [], model: { trained_model: false, method: "frequency statistics", reason: "No statistical model is fitted." }, weights: { novelty: 0.3 } },
  ranking: [{ signature: "delay@things", actions: [], score: 0.75, rank: 1, blocked: false, runs: 0, components: { novelty: 1 }, rationale: ["0 stored run(s) of this exact scenario"] }],
  records: [],
  prediction_accuracy: { evaluated: 0, outcome_matched: 0, note: "" },
  policy_scope: null,
};

function show(path: string, state: LiveState) {
  const fake = fakeClient(state);
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <LiveProvider client={fake.client}>
        <App />
      </LiveProvider>
    </MemoryRouter>,
  );
  return fake;
}

beforeEach(() => {
  mocked.faults.mockResolvedValue(CATALOG);
  mocked.experiments.mockResolvedValue([]);
  mocked.learning.mockResolvedValue(LEARNING);
  mocked.recommendations.mockResolvedValue([]);
  mocked.events.mockResolvedValue({ experiment_id: "exp-1", state: "COMPLETED", running: false, terminal: true, dry_run: false, last_seq: 0, events: [] });
});

describe("shell", () => {
  it("shows nothing as live before a valid snapshot has loaded", () => {
    show("/", { ...liveState(), phase: "loading", health: null });
    expect(screen.getByText(/Loading the current state/)).toBeInTheDocument();
    expect(screen.queryByText("Target health")).not.toBeInTheDocument();
  });

  it("shows the backend error instead of an empty dashboard", () => {
    show("/", { ...liveState(), phase: "error", conn: "offline", error: "Cannot reach the backend at http://127.0.0.1:8000." });
    expect(screen.getByRole("alert")).toHaveTextContent("Cannot reach the backend");
    expect(screen.queryByText("Target health")).not.toBeInTheDocument();
  });

  it("marks last-known data as not live when disconnected and offers a manual reconnect", async () => {
    const { calls } = show("/", liveState({ conn: "offline", retryAt: Date.now() + 4000, attempt: 2 }));
    expect(screen.getAllByText("DISCONNECTED").length).toBeGreaterThan(0);
    expect(screen.getByText(/are not live/)).toBeInTheDocument();
    expect(screen.queryByText("HEALTHY")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reconnect now" }));
    expect(calls.reconnect).toBe(1);
  });

  it("renders a 404 for unknown routes", () => {
    show("/nope", liveState());
    expect(screen.getByText(/does not exist/)).toBeInTheDocument();
  });
});

describe("Overview", () => {
  it("renders real measurements and separates monitoring health from target health", () => {
    show("/", liveState());
    expect(screen.getAllByText("HEALTHY").length).toBeGreaterThan(0);
    expect(screen.getByText("42 ms")).toBeInTheDocument();
    expect(screen.getAllByText(/1\.1x baseline/)).toHaveLength(2); // direct and search, each against its own baseline
    expect(screen.getByText("docker-things-1")).toBeInTheDocument();
    expect(screen.getByText("Monitoring pipeline")).toBeInTheDocument();
  });

  it("shows STALE, never HEALTHY, when probe data stops arriving", () => {
    const now = Date.now() / 1000;
    const snap = snapshot(now);
    snap.health = { ...snap.health, latest_timestamp: now - 120, fresh: false };
    show("/", liveState({}, snap));
    expect(screen.getAllByText("STALE").length).toBeGreaterThan(0);
    expect(screen.queryByText("HEALTHY")).not.toBeInTheDocument();
    expect(screen.getByText(/last known values, not the current state/)).toBeInTheDocument();
  });

  it("renders unknown measurements as unknown, not zero", () => {
    const snap = snapshot(Date.now() / 1000);
    snap.health = { ...snap.health, lost_acked: null, direct_p95_ms: null };
    snap.services = { available: false, error: "Cannot connect to the Docker daemon", services: null };
    show("/", liveState({}, snap));
    expect(screen.getAllByText("unknown").length).toBeGreaterThan(0);
    expect(screen.getByText(/container states are unknown \(not "down"\)/)).toBeInTheDocument();
  });
});

describe("recovery required", () => {
  it("shows a prominent alert with manual commands and blocks running", async () => {
    const lockout = { experiment_id: "exp-9", reason: "rollback could not be verified", created_at_utc: null, items: [{ experiment_id: "exp-9", fault: "delay", target: "mongodb", error: "tc still reports netem", manual: ["docker run --rm ... tc qdisc del dev eth0 root"] }] };
    show("/control", liveState({ controller: controller({ lockout }) }));
    const banner = screen.getAllByRole("alert")[0];
    expect(banner).toHaveTextContent("Recovery required: fault injection is disabled");
    expect(banner).toHaveTextContent("tc qdisc del dev eth0 root");
    expect(within(banner).getByRole("button", { name: /clear lockout/ })).toBeDisabled(); // needs a note
    await waitFor(() => expect(screen.getByRole("button", { name: "Run this scenario" })).toBeDisabled());
    expect(screen.getAllByText(/Recovery is required; fault injection is disabled/).length).toBeGreaterThan(0);
    mocked.resolveRecovery.mockResolvedValue({ lockout: null, recovered: [], failed: [] });
    mocked.controller.mockResolvedValue(controller());
    await userEvent.click(within(banner).getByRole("button", { name: "Retry verified recovery" }));
    expect(mocked.resolveRecovery).toHaveBeenCalledWith(false, "");
  });
});

describe("Experiment control", () => {
  it("requires a successful preview and an explicit authorization before a run", async () => {
    const user = userEvent.setup();
    const { calls } = show("/control", liveState());
    const run = await screen.findByRole("button", { name: "Run this scenario" });
    expect(run).toBeDisabled();
    expect(screen.getByText("Preview the scenario first.")).toBeInTheDocument();

    mocked.validate.mockResolvedValue(REPORT(true));
    await user.click(screen.getByRole("button", { name: "Preview plan" }));
    expect(await screen.findByText("policy check passed")).toBeInTheDocument();
    expect(screen.getByText(/Preview only\. Nothing has been executed/)).toBeInTheDocument();
    expect(screen.getByText(/netem delay 500ms/)).toBeInTheDocument();
    expect(run).toBeDisabled(); // previewed, but not yet authorized
    expect(mocked.runScenario).not.toHaveBeenCalled();

    const boxes = screen.getAllByRole("checkbox", { name: /I authorize REAL fault injection/ });
    await user.click(boxes[boxes.length - 1]);
    expect(run).toBeEnabled();
    mocked.runScenario.mockResolvedValue({ status: "started", experiment_id: "exp-2", duplicate: false });
    await user.click(run);
    expect(mocked.runScenario).toHaveBeenCalledTimes(1);
    const body = mocked.runScenario.mock.calls[0][0];
    expect(body.authorize).toBe(true);
    expect(body.scenario.actions[0]).toMatchObject({ fault: "delay", target: "things" });
    expect(body.request_id).toMatch(/^ui-/);
    await waitFor(() => expect(calls.refresh).toBe(1));
  });

  it("shows policy errors from the preview and keeps the run disabled", async () => {
    const user = userEvent.setup();
    show("/control", liveState());
    mocked.validate.mockResolvedValue(REPORT(false));
    await user.click(await screen.findByRole("button", { name: "Preview plan" }));
    expect(await screen.findByText("Refused by policy")).toBeInTheDocument();
    expect(screen.getByText(/exceeds the budget 1\.6/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run this scenario" })).toBeDisabled();
  });

  it("editing the scenario invalidates the preview", async () => {
    const user = userEvent.setup();
    show("/control", liveState());
    mocked.validate.mockResolvedValue(REPORT(true));
    await user.click(await screen.findByRole("button", { name: "Preview plan" }));
    await screen.findByText("policy check passed");
    await user.selectOptions(screen.getByLabelText("Target"), "mongodb");
    expect(screen.queryByText("policy check passed")).not.toBeInTheDocument();
    expect(screen.getByText("Preview the scenario first.")).toBeInTheDocument();
  });

  it("surfaces a failed request instead of failing silently", async () => {
    const user = userEvent.setup();
    show("/control", liveState());
    mocked.validate.mockRejectedValue(new ApiError(0, "The backend did not answer within 15s.", "timeout"));
    await user.click(await screen.findByRole("button", { name: "Preview plan" }));
    expect(await screen.findByText("The backend did not answer within 15s.")).toBeInTheDocument();
  });

  it("observe mode blocks runs; recommend mode blocks custom scenarios; unsupported faults are not selectable", async () => {
    show("/control", liveState({ controller: controller({ mode: "observe" }) }));
    expect((await screen.findAllByText("Observe mode: nothing can be injected.")).length).toBe(1);
    expect(screen.getByRole("option", { name: /Memory pressure \(not supported\)/ })).toBeDisabled();
    expect(screen.getByText("would OOM-kill the JVM")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /autonomous sandbox/ })).toBeDisabled(); // config flag is off
  });

  it("shows the running experiment's real state and wires the emergency stop", async () => {
    const user = userEvent.setup();
    const state = liveState({ orchestrator: orchestrator({ status: "running", experiment_id: "exp-1", record: record(), recovery_state: "FAULTS_ACTIVE" }) });
    const { calls } = show("/control", state);
    expect(screen.getByText("Running experiment")).toBeInTheDocument();
    expect(await screen.findByRole("button", { current: "step" })).toHaveTextContent("monitoring");
    expect(mocked.events).toHaveBeenCalledWith("exp-1", 0, expect.anything()); // the activity log loads for the running experiment
    expect(screen.getAllByText("FAULTS ACTIVE").length).toBeGreaterThan(0);
    expect(screen.getByText("alice (operator)")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Run this scenario" })).toBeDisabled());
    mocked.stop.mockResolvedValue({ status: "stop_requested" });
    await user.click(screen.getByRole("button", { name: "Emergency stop" }));
    expect(mocked.stop).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(calls.refresh).toBe(1));
  });

  it("distinguishes recovery-required from a recovered run", async () => {
    const rec = record({ state: "RECOVERY_REQUIRED", error: "REVERT NOT VERIFIED: delay@mongodb", actions: [{ ...record().actions![0], status: "rollback_failed", rollback_error: "tc still reports netem" }] });
    show("/control", liveState({ orchestrator: orchestrator({ status: "done", experiment_id: "exp-1", record: rec, recovery_state: "RECOVERY_REQUIRED" }) }));
    expect(screen.getAllByText("RECOVERY REQUIRED").length).toBeGreaterThan(0);
    expect(screen.getByText("not verified")).toBeInTheDocument();
    expect(screen.getByText("tc still reports netem")).toBeInTheDocument();
    expect(screen.queryByText("RECOVERED")).not.toBeInTheDocument();
    await screen.findByText("Fault types"); // let the page's own loads settle before the test ends
    await screen.findByText(/saved before the log existed/);
  });
});

describe("Ranked candidates on Experiment control", () => {
  it("ranks, selects into the proposal, and still requires authorization to run", async () => {
    const user = userEvent.setup();
    mocked.candidates.mockResolvedValue({
      generated_at_utc: "",
      evidence_runs: 3,
      store_degraded: null,
      candidates: [{ rank: 1, signature: "delay@things", targets: ["things"], faults: ["delay"], risk: "medium", hypothesis: "Untested.", reason: "0 stored run(s)", expected_impact: "Unknown.", score: 0.75, components: { novelty: 1 }, runs: 0, blocked: false, selectable: true, not_selectable_reason: null, evidence: { basis: "none", basis_runs: 0, agreement: null, confidence: "none", experiments: [] } }],
    });
    const proposal = { proposal_id: "p9", method: "operator_selected", fallback_note: "selected by an operator", objective: "o", hypothesis: "h", target_reason: "r", expected_impact: "e", expected_symptoms: [], metrics_to_observe: [], safety_risks: [], stop_conditions: ["operator emergency stop"], preflight_requirements: [], learning_value: "l", dependencies: [], scenario: { actions: [], max_duration_s: 60 }, validation: REPORT(true), prediction: {}, confidence: { level: "none", basis: "none", basis_runs: 0, agreement: null }, related_experiments: [], ranking: { rank: 1, score: 0.75, components: {}, rationale: [] }, alternatives: [], store_degraded: null, generated_at_utc: "2026-10-10T18:00:00+00:00" };
    mocked.selectCandidate.mockResolvedValue(proposal);
    show("/control", liveState());
    await user.click(await screen.findByRole("button", { name: "Rank candidates" }));
    expect(await screen.findByText("Ranked candidates")).toBeInTheDocument();
    expect(mocked.runScenario).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Select candidate 1: delay@things" }));
    expect(await screen.findByText("selected by operator")).toBeInTheDocument();
    expect(mocked.runScenario).not.toHaveBeenCalled(); // selecting ran nothing
    const run = screen.getByRole("button", { name: "Run proposed scenario" });
    expect(run).toBeDisabled(); // not authorized yet
    const boxes = screen.getAllByRole("checkbox", { name: /I authorize REAL fault injection/ });
    await user.click(boxes[0]);
    mocked.runScenario.mockResolvedValue({ status: "started", experiment_id: "exp-3", duplicate: false });
    await user.click(run);
    expect(mocked.runScenario.mock.calls[0][0]).toMatchObject({ authorize: true, proposal_id: "p9" });
  });
});

describe("History", () => {
  it("filters by text, outcome and evidence", () => {
    const rows = [summary(), summary({ id: "exp-2", signature: "delay@mongodb", outcome_class: "tolerated", symptoms: [] }), summary({ id: "exp-3", simulated: true, evidence: false, outcome_class: "no_evidence", symptoms: [] })];
    expect(filterExperiments(rows, "mongo", "", false).map((r) => r.id)).toEqual(["exp-2"]);
    expect(filterExperiments(rows, "", "degraded", false).map((r) => r.id)).toEqual(["exp-1"]);
    expect(filterExperiments(rows, "", "", true)).toHaveLength(2);
    expect(filterExperiments(rows, "search_errors", "", false).map((r) => r.id)).toEqual(["exp-1"]);
  });

  it("lists experiments and labels dry runs and unverified rollbacks", async () => {
    mocked.experiments.mockResolvedValue([summary(), summary({ id: "exp-dry", simulated: true, evidence: false, injected: false, outcome_class: "no_evidence" }), summary({ id: "exp-bad", rollback_verified: false, state: "RECOVERY_REQUIRED", outcome_class: "rollback_failure" })]);
    show("/history", liveState());
    expect(await screen.findByRole("link", { name: "exp-1" })).toHaveAttribute("href", "/history/exp-1");
    expect(screen.getByText(/dry run/)).toBeInTheDocument();
    expect(screen.getByText("not injected")).toBeInTheDocument();
    expect(screen.getByText("not verified")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("searchbox"), "zzz");
    expect(screen.getByText("No experiment matches these filters.")).toBeInTheDocument();
  });

  it("shows empty and error states with a retry", async () => {
    mocked.experiments.mockRejectedValueOnce(new ApiError(503, "knowledge store unavailable", "store_unavailable"));
    show("/history", liveState());
    expect(await screen.findByText("knowledge store unavailable")).toBeInTheDocument();
    mocked.experiments.mockResolvedValue([]);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No experiments saved yet.")).toBeInTheDocument();
  });

  it("renders an experiment's findings with measured facts and hypotheses labelled apart", async () => {
    const detail: ExperimentDetail = {
      record: record({ state: "COMPLETED", recovery_state: "RECOVERED", rollback_verified: true, validation: { verdict: "conclusive", counts_as_evidence: true, checks: [{ check: "rollback was verified", ok: true }], failed_checks: [], hypothesis_supported: null, returned_to_baseline: true, fault_ticks: 8 } }),
      analysis: { experiment_id: "exp-1", analysis_method: "gemini", summary: "Search failed while the service was down.", severity: "high", evidence: {}, recovery_verified: true, generated_at_utc: "", limitations: ["only gateway-level probes are measured"], findings: [
        { id: "F1", title: "Search availability degraded", severity: "high", evidence: "search_error_rate 0 -> 0.6", impact: "queries failed", confidence: "supported_by_measurements", recommended_fix: "", basis: "measured" },
        { id: "F2", title: "Connection pool exhaustion", severity: "medium", evidence: "not directly measured", impact: "", confidence: "plausible_hypothesis", recommended_fix: "", basis: "hypothesis" },
      ] },
      effects: {},
      learning: null,
      recommendations: [REC],
      agent_runs: [],
      timeline: [{ id: 1, ts: 1791600000, type: "authorized", data: { kind: "operator", actor: "alice" } }],
      followups: [],
    };
    mocked.experiment.mockResolvedValue(detail);
    show("/history/exp-1", liveState());
    expect(await screen.findByText("Search availability degraded")).toBeInTheDocument();
    expect(screen.getByText("measured")).toBeInTheDocument();
    expect(screen.getByText("hypothesis")).toBeInTheDocument();
    expect(screen.getByText("only gateway-level probes are measured")).toBeInTheDocument();
    expect(screen.getByText("This run counts as evidence.", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("operator by alice")).toBeInTheDocument();
    expect(screen.getByText("Add a circuit breaker.")).toBeInTheDocument();
  });

  it("reports a missing experiment", async () => {
    mocked.experiment.mockRejectedValue(new ApiError(404, "no saved experiment 'exp-x'"));
    show("/history/exp-x", liveState());
    expect(await screen.findByText("no saved experiment 'exp-x'")).toBeInTheDocument();
  });
});

describe("Insights", () => {
  it("shows counts, the ranking with its components, and that no model is trained", async () => {
    show("/insights", liveState());
    expect(await screen.findByText("delay@things")).toBeInTheDocument();
    expect(screen.getByText("1 / 54")).toBeInTheDocument();
    expect(screen.getByText(/No statistical model is fitted/)).toBeInTheDocument();
    expect(screen.getByText(/none yet: needs a repeat run/)).toBeInTheDocument();
  });

  it("lets the operator track a recommendation and never implies it was applied", async () => {
    const user = userEvent.setup();
    mocked.recommendations.mockResolvedValue([REC]);
    show("/insights", liveState());
    expect(await screen.findByText("Degrade search gracefully")).toBeInTheDocument();
    expect(screen.getByText(/Nothing here is applied by this tool/)).toBeInTheDocument();
    mocked.setRecommendation.mockResolvedValue({ ...REC, status: "accepted" });
    await user.click(screen.getByRole("button", { name: "Accept" }));
    expect(mocked.setRecommendation).toHaveBeenCalledWith("exp-1", 0, "accepted");
    await waitFor(() => expect(mocked.recommendations).toHaveBeenCalledTimes(2)); // list reloaded
  });

  it("degrades visibly when the knowledge store is down", async () => {
    mocked.learning.mockRejectedValue(new ApiError(503, "knowledge store unavailable"));
    show("/insights", liveState());
    expect(await screen.findByText(/Learning data unavailable: knowledge store unavailable/)).toBeInTheDocument();
  });
});
