import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import type { Candidate, CandidatesView, Followup, FollowupComparison, Proposal, Recommendation, RerunPlan } from "../api/types";
import { CandidateList, ComparisonView, Followups, RerunPanel } from "./followup";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, selectCandidate: vi.fn(), rerunPlan: vi.fn(), rerun: vi.fn() } };
});
const mocked = api as unknown as Record<"selectCandidate" | "rerunPlan" | "rerun", ReturnType<typeof vi.fn>>;
const wrap = (ui: React.ReactElement) => render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>{ui}</MemoryRouter>);

const cand = (over: Partial<Candidate>): Candidate => ({
  rank: 1,
  signature: "delay@mongodb",
  targets: ["mongodb"],
  faults: ["delay"],
  risk: "high",
  hypothesis: "No stored run of this fault/target exists.",
  reason: "0 stored run(s) of this exact scenario",
  expected_impact: "Unknown: there is no stored evidence for this scenario. A failure here can reach 3 dependent service(s).",
  score: 0.783,
  components: { novelty: 1, outcome_uncertainty: 1, usefulness: 0.667, relevance: 1, follow_up: 0, repetition_penalty: 0 },
  runs: 0,
  blocked: false,
  selectable: true,
  not_selectable_reason: null,
  evidence: { basis: "none", basis_runs: 0, agreement: null, confidence: "none", experiments: [] },
  ...over,
});
const VIEW: CandidatesView = {
  generated_at_utc: "2026-10-10T18:00:00+00:00",
  evidence_runs: 7,
  store_degraded: null,
  candidates: [
    cand({}),
    cand({ rank: 2, signature: "kill@things-search", targets: ["things-search"], faults: ["kill"], risk: "medium", hypothesis: "3 of 4 prior run(s) of the same scenario ended 'kill_switch'.", evidence: { basis: "direct", basis_runs: 4, agreement: "3 of 4", confidence: "high", experiments: ["exp-a", "exp-b"] } }),
    cand({ rank: 3, signature: "kill@gateway", targets: ["gateway"], faults: ["kill"], risk: "unknown", blocked: true, selectable: false, score: null, not_selectable_reason: "A previous run of this scenario ended with an unverified rollback" }),
  ],
};
const REC: Recommendation = {
  experiment_id: "exp-1",
  index: 0,
  signature: "kill@things-search",
  title: "Degrade search gracefully",
  severity: "high",
  status: "accepted",
  validated_by: null,
  validation_note: null,
  updated_at_utc: "",
  item: { id: "R-exp-1-1", title: "Degrade search gracefully", finding_title: "Search availability degraded", description: "", severity: "high", priority: 1, supporting_findings: ["F1"], evidence: "", proposed_change: "", analyst_suggestion: "", analyst_suggestion_source: "", expected_benefit: "contained outage", complexity: "medium", risks: "", verification_steps: [], recurrence: { prior_runs_with_same_finding: 0, prior_runs: 0 } },
};
const PLAN: RerunPlan = {
  ok: true,
  requires_revised_plan: false,
  reasons: [],
  scenario: { actions: [{ fault: "kill", target: "things-search", params: {}, start_offset_s: 0, duration_s: null }], max_duration_s: 20 },
  validation: null,
  original: { id: "exp-1", signature: "kill@things-search", state: "COMPLETED", outcome_class: "kill_switch", started_at_utc: null },
  recommendation: REC,
  baseline_note: "The follow-up ignores baseline.json and measures a fresh baseline over 30s immediately before injecting.",
  caveat: "Observed correlation, not proof of causation: this compares one run before the change with one run after it.",
};
const outcome = (id: string, cls: string) => ({ id, state: "COMPLETED", outcome_class: cls, aborted: cls === "kill_switch", abort_reason: cls === "kill_switch" ? "search endpoint failed on all of the last 3 ticks" : null, recovery_outcome: "recovered", fault_ticks: 8, baseline_source: "fresh", baseline_direct_p95_ms: 40 });
const COMPARISON: FollowupComparison = {
  original_id: "exp-1",
  followup_id: "exp-2",
  signature: "kill@things-search",
  comparable: true,
  not_comparable_reasons: [],
  parameter_differences: [],
  metrics: [
    { key: "search_error_rate", label: "Search error rate (during fault)", unit: "ratio", before: 0.6, after: 0, change: -0.6, change_pct: -100, verdict: "improved", reason: null },
    { key: "direct_latency_p95_ms", label: "Direct read latency p95 (during fault)", unit: "ms", before: 50, after: 90, change: 40, change_pct: 80, verdict: "regressed", reason: null },
    { key: "stale_p95_s", label: "Twin staleness p95 (during fault)", unit: "s", before: 2, after: 2.1, change: 0.1, change_pct: 5, verdict: "unchanged", reason: null },
    { key: "lost_acked_max", label: "Lost acknowledged updates (max)", unit: "count", before: null, after: 0, change: null, change_pct: null, verdict: "not_comparable", reason: "not measured in the original run" },
  ],
  summary: { improved: 1, regressed: 1, unchanged: 1, not_comparable: 1 },
  outcomes: { original: outcome("exp-1", "kill_switch"), followup: outcome("exp-2", "tolerated") },
  caveat: "Observed correlation, not proof of causation: this compares one run before the change with one run after it.",
  verification: { demonstrated: "observed", statement: "The finding 'Search availability degraded' did not recur in the follow-up run; 1 metric(s) improved, 1 regressed.", finding_title: "Search availability degraded", finding_recurred: false, expected_benefit: "contained outage", evidence_strength: "observed correlation from one follow-up run" },
  link: { experiment_id: "exp-1", recommendation_index: 0, recommendation_id: "R-exp-1-1", recommendation_title: "Degrade search gracefully", finding_title: "Search availability degraded", confirmed_applied_by: "alice", confirmation_note: "added a fallback", confirmed_at_utc: "" },
};

beforeEach(() => {
  mocked.selectCandidate.mockReset();
  mocked.rerunPlan.mockReset();
  mocked.rerun.mockReset();
});

describe("CandidateList", () => {
  it("shows rank, target, fault, risk, hypothesis and historical evidence for every candidate", () => {
    wrap(<CandidateList view={VIEW} selected={null} onSelected={() => {}} disabledReason={null} />);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText("mongodb")).toBeInTheDocument();
    expect(within(rows[0]).getByText("delay")).toBeInTheDocument();
    expect(within(rows[0]).getByText("high")).toBeInTheDocument();
    expect(within(rows[0]).getByText("no stored run yet")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/3 of 4 runs of this scenario \(high confidence\)/)).toBeInTheDocument();
    expect(within(rows[1]).getByRole("link", { name: "exp-a" })).toHaveAttribute("href", "/history/exp-a");
    expect(within(rows[2]).getByText("unknown")).toBeInTheDocument(); // risk is shown as unknown, not guessed
    expect(screen.getByText(/Ranked from 7 evidence run\(s\)/)).toBeInTheDocument();
  });

  it("reveals the reason and expected impact on request", async () => {
    wrap(<CandidateList view={VIEW} selected={null} onSelected={() => {}} disabledReason={null} />);
    expect(screen.queryByText(/can reach 3 dependent service/)).not.toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Reason and expected impact" })[0]);
    expect(screen.getByText(/can reach 3 dependent service/)).toBeInTheDocument();
    expect(screen.getByText(/score 0\.783/)).toBeInTheDocument();
  });

  it("selecting a candidate requests a proposal and hands it back, without running anything", async () => {
    const onSelected = vi.fn();
    const proposal = { proposal_id: "p1", validation: { signature: "kill@things-search" } } as unknown as Proposal;
    mocked.selectCandidate.mockResolvedValue(proposal);
    wrap(<CandidateList view={VIEW} selected={null} onSelected={onSelected} disabledReason={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Select candidate 2: kill@things-search" }));
    expect(mocked.selectCandidate).toHaveBeenCalledWith("kill@things-search");
    await waitFor(() => expect(onSelected).toHaveBeenCalledWith(proposal));
    expect(mocked.rerun).not.toHaveBeenCalled();
  });

  it("does not offer a blocked candidate and says why", () => {
    wrap(<CandidateList view={VIEW} selected="delay@mongodb" onSelected={() => {}} disabledReason={null} />);
    expect(screen.getByText(/not selectable: A previous run of this scenario ended with an unverified rollback/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Select candidate 3/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select candidate 1: delay@mongodb" })).toHaveTextContent("Selected");
  });

  it("surfaces a refused selection", async () => {
    mocked.selectCandidate.mockRejectedValue(new ApiError(400, "'kill@nginx' is not in the current ranked candidate list; rank again"));
    wrap(<CandidateList view={VIEW} selected={null} onSelected={() => {}} disabledReason={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Select candidate 1: delay@mongodb" }));
    expect(await screen.findByText(/rank again/)).toBeInTheDocument();
  });

  it("shows an empty state", () => {
    wrap(<CandidateList view={{ ...VIEW, candidates: [] }} selected={null} onSelected={() => {}} disabledReason={null} />);
    expect(screen.getByText("No candidate is eligible under the current policy.")).toBeInTheDocument();
  });
});

describe("RerunPanel", () => {
  it("needs both the applied-change confirmation and the authorization before it can start", async () => {
    const user = userEvent.setup();
    const onStarted = vi.fn();
    mocked.rerunPlan.mockResolvedValue(PLAN);
    mocked.rerun.mockResolvedValue({ status: "started", experiment_id: "exp-2", original_id: "exp-1", duplicate: false });
    wrap(<RerunPanel rec={REC} onStarted={onStarted} />);
    const start = await screen.findByRole("button", { name: "Start follow-up run" });
    expect(mocked.rerunPlan).toHaveBeenCalledWith("exp-1", 0, expect.anything());
    expect(screen.getByText("kill on things-search")).toBeInTheDocument();
    expect(screen.getByText(/measures a fresh baseline over 30s/)).toBeInTheDocument();
    expect(start).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /I confirm the change was actually applied/ }));
    expect(start).toBeDisabled(); // confirmed, but not authorized
    await user.click(screen.getByRole("checkbox", { name: /I authorize REAL fault injection/ }));
    expect(start).toBeEnabled();
    await user.type(screen.getByLabelText(/What was changed/), "added a fallback");
    await user.click(start);
    const [id, body] = mocked.rerun.mock.calls[0];
    expect(id).toBe("exp-1");
    expect(body).toMatchObject({ recommendation_index: 0, authorize: true, confirm_applied: true, note: "added a fallback" });
    expect(await screen.findByText(/Follow-up run/)).toHaveTextContent("exp-2 started");
    expect(onStarted).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "its page" })).toHaveAttribute("href", "/history/exp-2");
  });

  it("explains why the original cannot be repeated and offers no run button", async () => {
    mocked.rerunPlan.mockResolvedValue({ ...PLAN, ok: false, requires_revised_plan: true, scenario: null, reasons: ["the original was a dry run: nothing was injected, so there is no result to compare with"] });
    wrap(<RerunPanel rec={REC} onStarted={() => {}} />);
    expect(await screen.findByText(/A revised plan is required/)).toBeInTheDocument();
    expect(screen.getByText(/the original was a dry run/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start follow-up run" })).not.toBeInTheDocument();
  });

  it("shows a refusal from the backend and keeps the form", async () => {
    const user = userEvent.setup();
    mocked.rerunPlan.mockResolvedValue(PLAN);
    mocked.rerun.mockRejectedValue(new ApiError(423, "mutation is disabled: recovery is required", "recovery_required"));
    wrap(<RerunPanel rec={REC} onStarted={() => {}} />);
    await user.click(await screen.findByRole("checkbox", { name: /I confirm the change/ }));
    await user.click(screen.getByRole("checkbox", { name: /I authorize REAL/ }));
    await user.click(screen.getByRole("button", { name: "Start follow-up run" }));
    expect(await screen.findByText("mutation is disabled: recovery is required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start follow-up run" })).toBeInTheDocument();
  });

  it("reports a plan that could not be loaded", async () => {
    mocked.rerunPlan.mockRejectedValue(new ApiError(503, "knowledge store unavailable"));
    wrap(<RerunPanel rec={REC} onStarted={() => {}} />);
    expect(await screen.findByText("knowledge store unavailable")).toBeInTheDocument();
  });
});

describe("ComparisonView", () => {
  it("shows before, after, change and result per metric, with missing values marked as not measured", () => {
    wrap(<ComparisonView result={COMPARISON} />);
    const row = (name: RegExp) => screen.getByRole("row", { name });
    expect(within(row(/Search error rate/)).getByText("60.0%")).toBeInTheDocument();
    expect(within(row(/Search error rate/)).getByText("0.0%")).toBeInTheDocument();
    expect(within(row(/Search error rate/)).getByText("-60.0 pts")).toBeInTheDocument();
    expect(within(row(/Search error rate/)).getByText("improved")).toBeInTheDocument();
    expect(within(row(/Direct read latency/)).getByText("regressed")).toBeInTheDocument();
    expect(within(row(/Direct read latency/)).getByText("(+80%)")).toBeInTheDocument();
    expect(within(row(/Twin staleness/)).getByText("unchanged")).toBeInTheDocument();
    const lost = row(/Lost acknowledged updates/);
    expect(within(lost).getByText("not measured")).toBeInTheDocument(); // never rendered as 0
    expect(within(lost).getByText("not comparable")).toBeInTheDocument();
    expect(within(lost).getByText("not measured in the original run")).toBeInTheDocument();
  });

  it("states the verification result, who confirmed the change, and the correlation caveat", () => {
    wrap(<ComparisonView result={COMPARISON} />);
    expect(screen.getByText("benefit observed")).toBeInTheDocument();
    expect(screen.getByText(/did not recur in the follow-up run/)).toBeInTheDocument();
    expect(screen.getByText(/alice: "added a fallback"/)).toBeInTheDocument();
    expect(screen.getByText(/not proof of causation/)).toBeInTheDocument();
    expect(screen.getByText(/1 improved · 1 regressed · 1 unchanged · 1 not comparable/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "exp-1" })).toHaveAttribute("href", "/history/exp-1");
    expect(screen.getByText(/outcome kill switch/)).toBeInTheDocument();
    expect(screen.getByText(/outcome tolerated/)).toBeInTheDocument();
  });

  it("labels a failed and an inconclusive verification, and incomparable runs", () => {
    const failed = { ...COMPARISON, verification: { ...COMPARISON.verification!, demonstrated: "not_observed" as const, statement: "The finding recurred." } };
    const view = wrap(<ComparisonView result={failed} />);
    expect(screen.getByText("benefit not observed")).toBeInTheDocument();
    view.unmount();
    const incomparable: FollowupComparison = { ...COMPARISON, comparable: false, not_comparable_reasons: ["no fault was injected in the follow-up run"], metrics: COMPARISON.metrics.map((m) => ({ ...m, verdict: "not_comparable", change: null, change_pct: null, reason: "the two runs are not comparable" })), verification: { ...COMPARISON.verification!, demonstrated: "inconclusive", statement: "The two runs cannot be compared." } };
    wrap(<ComparisonView result={incomparable} />);
    expect(screen.getByText("inconclusive")).toBeInTheDocument();
    expect(screen.getByText("no fault was injected in the follow-up run")).toBeInTheDocument();
    expect(screen.queryByText("improved")).not.toBeInTheDocument();
  });

  it("shows an error result instead of an empty table", () => {
    wrap(<ComparisonView result={{ ...COMPARISON, error: "comparison could not be produced: original record missing" }} />);
    expect(screen.getByText(/comparison could not be produced/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("Followups", () => {
  const link: Followup = { followup_id: "exp-2", original_id: "exp-1", rec_index: 0, recommendation_id: "R", created_at_utc: "2026-10-10T18:00:00+00:00", confirmed_by: "alice", confirmation_note: "", status: "observed", completed_at_utc: "", result: COMPARISON };

  it("links to the other run from either side and handles a follow-up still in progress", () => {
    const a = wrap(<Followups items={[link]} experimentId="exp-1" />);
    expect(screen.getAllByText("Follow-up run").length).toBeGreaterThan(0);
    expect(screen.queryByText("This run is a follow-up of")).not.toBeInTheDocument();
    a.unmount();
    const b = wrap(<Followups items={[link]} experimentId="exp-2" />);
    expect(screen.getByText("This run is a follow-up of")).toBeInTheDocument();
    b.unmount();
    wrap(<Followups items={[{ ...link, status: "running", result: null }]} experimentId="exp-1" />);
    expect(screen.getByText(/has not finished yet/)).toBeInTheDocument();
  });
});
