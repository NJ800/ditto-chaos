import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { api, request } from "../api/client";
import type { AgentRun, AgentStatus, Analysis, ExperimentEvent, ExperimentRecord, Thresholds } from "../api/types";
import { LiveProvider } from "../live/LiveContext";
import type { LiveClient, LiveState } from "../live/stream";
import { fakeClient, liveState, orchestrator, record } from "../test/fixtures";
import { agentChipState, closestCondition, stageStates, type RunView } from "./Pipeline";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, request: vi.fn(), api: Object.fromEntries(Object.keys(real.api).map((k) => [k, vi.fn()])) };
});
const mocked = api as unknown as Record<keyof typeof api, ReturnType<typeof vi.fn>>;
const mockedRequest = request as unknown as ReturnType<typeof vi.fn>;

// ---- builders ----
const TH: Thresholds = { max_error_rate: 0.05, error_rate_window: 10, max_latency_factor: 3, min_latency_floor_ms: 200, max_stale_s: 15, breach_window: 3 };
let seq = 0;
const ev = (state: string, kind: ExperimentEvent["kind"], t_s: number, detail: ExperimentEvent["detail"] = {}, message = ""): ExperimentEvent => ({ seq: ++seq, ts: "2026-10-11T01:56:36+00:00", t_s, state, kind, message, detail });
const health = (t: number, over: ExperimentEvent["detail"] = {}) => ev("MONITORING", "health", t, { direct_ok: true, direct_ms: 37, search_ok: true, search_ms: 41, stale_p95_s: 2, ...over });
const eventsReply = (events: ExperimentEvent[], terminal = true) => ({ experiment_id: "exp-1", state: null, running: !terminal, terminal, dry_run: false, last_seq: events.length ? events[events.length - 1].seq : 0, events });
const analysis = (over: Partial<Analysis> = {}): Analysis => ({ experiment_id: "exp-1", analysis_method: "deterministic_fallback", summary: "", severity: "high", findings: [{ title: "Search availability degraded", severity: "high", evidence: "", impact: "", confidence: "", recommended_fix: "" }], evidence: {}, recovery_verified: true, generated_at_utc: "", ...over });
const agentRun = (agent: string, over: Partial<AgentRun> = {}): AgentRun => ({ id: `run-${agent}`, agent, title: agent, version: "1", kind: "deterministic", experiment_id: "exp-1", status: "completed", attempts: 1, started_at_utc: "t0", finished_at_utc: "t1", duration_ms: 12, error: null, summary: `${agent} ok`, method: null, evidence_ids: [], ...over });
const agentStatus = (name: string, over: Partial<AgentStatus> = {}): AgentStatus => ({ name, title: name, role: "", version: "1", kind: "deterministic", timeout_s: 30, retries: 0, state: "idle", task: null, since: null, last: null, last_success_utc: null, failures: 0, runs: 0, recent: [], provider: null, ...over });
const ALL_AGENTS = ["risk", "fault_injection", "recovery", "validation", "insights", "learning", "remediation"];
const done = (over: Partial<ExperimentRecord> = {}) => record({ state: "COMPLETED", rollback_verified: true, recovery_outcome: "recovered", recovery_s: 2, actions: [{ ...record().actions![0], status: "rolled_back", rolled_back_at_s: 15.6, rollback_verified: true }], ...over });
const view = (over: Partial<RunView>): RunView => ({ status: "done", record: null, analysis: null, agentRuns: [], ...over });

/** A live client whose state can be replaced, so a real state change can be observed. */
function mutableClient(initial: LiveState) {
  let cur = initial;
  const listeners = new Set<() => void>();
  const client = { getState: () => cur, subscribe: (cb: () => void) => (listeners.add(cb), () => listeners.delete(cb)), start: () => {}, stop: () => {}, refresh: () => {}, reconnectNow: () => {}, setController: () => {} } as unknown as LiveClient;
  return { client, set: (next: LiveState) => act(() => { cur = next; listeners.forEach((l) => l()); }) };
}
/** Renders the page and lets the first activity-log request settle, so no update lands outside act(). */
async function show(state: LiveState, path = "/pipeline", client?: LiveClient) {
  render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <LiveProvider client={client ?? fakeClient(state).client}>
        <App />
      </LiveProvider>
    </MemoryRouter>,
  );
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}
const card = (title: RegExp) => screen.getByRole("heading", { level: 3, name: title }).closest("li")!;
const chip = (name: string) => document.querySelector(`[data-agent="${name}"]`) as HTMLElement;
const setReducedMotion = (reduce: boolean) => {
  window.matchMedia = ((q: string) => ({ matches: reduce && q.includes("reduce"), media: q, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, onchange: null, dispatchEvent: () => false })) as unknown as typeof window.matchMedia;
};

beforeEach(() => {
  seq = 0;
  mocked.events.mockResolvedValue(eventsReply([]));
  setReducedMotion(false);
});
afterEach(() => vi.clearAllMocks());

// ================================================================ stage states
describe("stageStates", () => {
  it("is all pending without a run", async () => {
    expect(stageStates(view({ status: "idle" }))).toEqual(Array(8).fill("pending"));
  });

  it("marks earlier stages done and the controller's current state active", async () => {
    expect(stageStates(view({ status: "running", record: record({ state: "MONITORING" }) }))).toEqual(["done", "done", "done", "done", "done", "active", "pending", "pending"]);
    for (const state of ["REVERTING", "VERIFYING_RECOVERY"]) expect(stageStates(view({ status: "running", record: record({ state }) }))[6]).toBe("active");
  });

  it("kill-switch stop: Monitor is stopped, the rollback that followed is done", async () => {
    const s = stageStates(view({ record: done({ abort_reason: "search endpoint failed on all of the last 3 ticks" }), analysis: analysis() }));
    expect(s).toEqual(["done", "done", "done", "done", "done", "stopped", "done", "done"]);
  });

  it("operator emergency stop is shown as stopped, not failed", async () => {
    expect(stageStates(view({ record: done({ abort_reason: "emergency stop (requested by operator)" }) }))[5]).toBe("stopped");
  });

  it("rollback failure is failed and is never shown as done", async () => {
    const bad = record({ state: "RECOVERY_REQUIRED", rollback_verified: false, rollback_error: "tc still reports netem" });
    expect(stageStates(view({ status: "error", record: bad }))[6]).toBe("failed");
    expect(stageStates(view({ record: done({ state: "RECOVERY_TIMEOUT", recovery_outcome: "timeout" }) }))[6]).toBe("failed");
  });

  it("dry run: the fault stages are previewed, analysis is skipped", async () => {
    const dry = record({ state: "COMPLETED", dry_run: true, injected: false });
    expect(stageStates(view({ record: dry }))).toEqual(["done", "done", "done", "done", "previewed", "done", "previewed", "skipped"]);
    expect(stageStates(view({ status: "running", record: record({ state: "MONITORING", dry_run: true, injected: false }) }))[4]).toBe("previewed");
  });

  it("a run refused before injection stops at the stage it reached and skips the rest", async () => {
    const refused = record({ state: "ABORTED", injected: false, events: [ev("PRECHECK", "info", 0), ev("BASELINE", "decision", 1)] });
    expect(stageStates(view({ status: "error", record: refused }))).toEqual(["done", "done", "done", "stopped", "skipped", "skipped", "skipped", "skipped"]);
  });

  it("analysis is active while agents run, then done only for this run's analysis", async () => {
    expect(stageStates(view({ status: "analyzing", record: done() }))[7]).toBe("active");
    expect(stageStates(view({ record: done(), analysis: analysis() }))[7]).toBe("done");
    expect(stageStates(view({ record: done(), analysis: analysis({ experiment_id: "another" }) }))[7]).toBe("pending");
    expect(stageStates(view({ status: "saved", record: done() }))[7]).toBe("skipped");
  });
});

describe("closestCondition", () => {
  const c = (search_ok: boolean, direct_ms = 30) => ({ direct_ok: true, direct_ms, search_ok, search_ms: 40, stale: 2 });
  it("counts the newest consecutive breaches of the closest stop condition", async () => {
    expect(closestCondition([c(true), c(false), c(false)], 200, TH)).toEqual({ label: "search failures", count: 2, required: 3 });
    expect(closestCondition([c(false), c(true)], 200, TH).count).toBe(0);
    expect(closestCondition([c(true, 300), c(true, 400), c(true, 500), c(true, 600)], 200, TH)).toEqual({ label: "latency above the limit", count: 3, required: 3 });
  });
});

// ================================================================ agent states
describe("agentChipState", () => {
  const rec = done();
  it("covers running, queued, completed, failed and skipped for the post-run agents", async () => {
    const analysing = view({ status: "analyzing", record: rec, agentRuns: [] });
    expect(agentChipState("recovery", analysing, agentStatus("recovery", { state: "running", task: "exp-1" })).state).toBe("running");
    expect(agentChipState("validation", analysing, agentStatus("validation")).state).toBe("queued");
    expect(agentChipState("recovery", analysing, agentStatus("recovery", { last: { status: "completed", experiment_id: "exp-1", summary: "RECOVERED" } })).state).toBe("completed");
    const finished = view({ record: rec, agentRuns: [agentRun("insights", { status: "failed", error: "boom" }), agentRun("learning", { status: "skipped" }), agentRun("remediation", { status: "timeout" }), agentRun("validation", { status: "fallback" })] });
    expect(agentChipState("insights", finished, agentStatus("insights")).state).toBe("failed");
    expect(agentChipState("remediation", finished, agentStatus("remediation")).state).toBe("failed");
    expect(agentChipState("learning", finished, agentStatus("learning")).state).toBe("skipped");
    expect(agentChipState("validation", finished, agentStatus("validation")).state).toBe("completed");
  });

  it("does not borrow another experiment's result", async () => {
    const other = agentStatus("recovery", { last: { status: "completed", experiment_id: "exp-other", summary: "old" } });
    expect(agentChipState("recovery", view({ status: "analyzing", record: rec }), other).state).toBe("queued");
  });

  it("planning agents are idle for an operator-written scenario and report their run for a proposal", async () => {
    const last = { status: "completed", summary: "pause@mongodb (rank #1)" };
    expect(agentChipState("risk", view({ record: record({ source: "operator" }) }), agentStatus("risk", { last }))).toMatchObject({ state: "idle", note: "not used: the operator wrote this scenario" });
    expect(agentChipState("risk", view({ record: record({ source: "proposal" }) }), agentStatus("risk", { last })).state).toBe("completed");
  });

  it("post-run agents are skipped for a dry run", async () => {
    expect(agentChipState("insights", view({ record: record({ state: "COMPLETED", dry_run: true, injected: false }) }), agentStatus("insights"))).toMatchObject({ state: "skipped", note: "a dry run has nothing to analyse" });
  });
});

// ================================================================ the page
describe("Pipeline page", () => {
  it("says so when there is no run, and links to experiment control", async () => {
    await show(liveState());
    expect(screen.getByRole("heading", { level: 1, name: "Pipeline" })).toBeInTheDocument();
    expect(screen.getByText(/No experiment has run since the API started/)).toBeInTheDocument();
    expect(within(card(/Monitor/)).getByText("pending")).toBeInTheDocument();
  });

  it("shows a live Monitor stage: checks, latency against the limit, the kill-switch counter and a sparkline", async () => {
    mocked.events.mockResolvedValue(eventsReply([ev("PRECHECK", "info", 0), ev("MONITORING", "info", 10), health(12), health(14, { search_ok: false, search_ms: null }), health(16, { search_ok: false, search_ms: null })], false));
    const rec = record({ state: "MONITORING", baseline: { direct_ms: { p95: 72 }, search_ms: { p95: 84 } } });
    await show(liveState({ thresholds: TH, orchestrator: orchestrator({ status: "running", record: rec }) }));
    const monitor = card(/Monitor/);
    expect(monitor).toHaveAttribute("aria-current", "step");
    expect(within(monitor).getByText("active")).toBeInTheDocument();
    expect(await within(monitor).findByText("3")).toBeInTheDocument();
    expect(within(monitor).getByText(/\/ limit 216 ms/)).toBeInTheDocument();
    expect(within(monitor).getByTestId("kill-switch-meter")).toHaveTextContent("search failures 2 of 3");
    expect(within(monitor).getByRole("img", { name: /Read latency of the last 3 checks, stop limit 216 ms dashed/ })).toBeInTheDocument();
    expect(within(monitor).getByText(/running for/)).toBeInTheDocument();
    expect(screen.getByText("Kill switch armed.")).toBeInTheDocument();
    expect(screen.getByTestId("stage-announcer")).toHaveTextContent("Stage 6, Monitor: active");
    expect(screen.getByTestId("stage-announcer")).toHaveAttribute("aria-live", "polite");
  });

  it("kill-switch stop: exact reason, the stage where it fired, verified rollback and recovery", async () => {
    mocked.events.mockResolvedValue(eventsReply([ev("MONITORING", "decision", 15.2, { kill_switch: true, reason: "x" }), ev("VERIFYING_RECOVERY", "decision", 20, { healthy_streak: 3, required: 3 })]));
    const rec = done({ abort_reason: "search endpoint failed on all of the last 3 ticks", detected_s: 15.2 });
    await show(liveState({ thresholds: TH, orchestrator: orchestrator({ status: "done", record: rec, analysis: analysis() }) }));
    expect(screen.getByText("last run, not live")).toBeInTheDocument();
    expect(within(card(/Monitor/)).getByText("stopped")).toBeInTheDocument();
    expect(await screen.findByText(/during Monitor at 15.2 s: search endpoint failed on all of the last 3 ticks/)).toBeInTheDocument();
    expect(screen.getByText("Kill switch fired")).toBeInTheDocument();
    const rb = card(/Roll back and verify/);
    expect(within(rb).getByText("done")).toBeInTheDocument();
    expect(within(rb).getByText("removed, verified")).toBeInTheDocument();
    expect(within(rb).getByText("recovered in 2.0 s")).toBeInTheDocument();
    const an = card(/Analyse/);
    expect(within(an).getByText("1 finding(s), top severity high")).toBeInTheDocument();
    expect(within(an).getByText("analyst: rule-based fallback")).toBeInTheDocument();
  });

  it("names the AI analyst when the analysis did not use the fallback", async () => {
    await show(liveState({ orchestrator: orchestrator({ status: "done", record: done(), analysis: analysis({ analysis_method: "gemini" }) }) }));
    expect(within(card(/Analyse/)).getByText("analyst: AI (gemini)")).toBeInTheDocument();
    expect(screen.getByText("Completed.")).toBeInTheDocument();
  });

  it("emergency stop is reported as stopped by the operator", async () => {
    await show(liveState({ orchestrator: orchestrator({ status: "done", record: done({ abort_reason: "emergency stop (requested by operator)", detected_s: 6 }) }) }));
    expect(screen.getByText("Stopped by the operator")).toBeInTheDocument();
    expect(within(card(/Monitor/)).getByText("stopped")).toBeInTheDocument();
  });

  it("rollback failure: red alert with the lock message, and the action says verify failed", async () => {
    const rec = record({ state: "RECOVERY_REQUIRED", rollback_verified: false, rollback_error: "tc still reports netem", actions: [{ ...record().actions![0], status: "rollback_failed", rolled_back_at_s: 9, rollback_verified: false, rollback_error: "tc still reports netem" }] });
    await show(liveState({ orchestrator: orchestrator({ status: "error", record: rec }) }));
    expect(screen.getByRole("alert")).toHaveTextContent("Rollback not verified. tc still reports netem Fault injection is locked until a person resolves it.");
    const rb = card(/Roll back and verify/);
    expect(within(rb).getByText("failed")).toBeInTheDocument();
    expect(within(rb).getByText("verify failed")).toBeInTheDocument();
  });

  it("dry run: every fault stage says previewed, not executed, and no kill-switch meter is shown", async () => {
    const rec = record({ state: "COMPLETED", dry_run: true, injected: false, authorization: undefined, actions: [{ ...record().actions![0], status: "previewed", applied_at_s: null }] });
    await show(liveState({ orchestrator: orchestrator({ status: "done", record: rec }) }));
    expect(screen.getByText("dry run: nothing is injected")).toBeInTheDocument();
    expect(within(card(/Inject/)).getByText("previewed")).toBeInTheDocument();
    expect(within(card(/Inject/)).getByText("pause on things: previewed, not executed")).toBeInTheDocument();
    expect(within(card(/Roll back and verify/)).getByText("previewed, not executed: nothing to remove")).toBeInTheDocument();
    expect(within(card(/Analyse/)).getByText("skipped")).toBeInTheDocument();
    expect(screen.queryByTestId("kill-switch-meter")).not.toBeInTheDocument();
    expect(screen.getByText(/Dry run complete: every fault stage was previewed, not executed/)).toBeInTheDocument();
  });

  it("multi-fault: one line per action, the rollback order and the recovery streak", async () => {
    mocked.events.mockResolvedValue(eventsReply([ev("PRECHECK", "decision", 0, { rollback_order: ["a2", "a1"] }), ev("VERIFYING_RECOVERY", "decision", 30, { healthy_streak: 2, required: 3 })], false));
    const a1 = { ...record().actions![0], id: "a1", fault: "delay", target: "mongodb", status: "rolled_back", rolled_back_at_s: 20, rollback_verified: true };
    const a2 = { ...record().actions![0], id: "a2", fault: "kill", target: "things-search", status: "rolling_back" };
    await show(liveState({ orchestrator: orchestrator({ status: "running", record: record({ state: "VERIFYING_RECOVERY", actions: [a1, a2] }) }) }));
    const rb = card(/Roll back and verify/);
    expect(await within(rb).findByText("order: things-search → mongodb")).toBeInTheDocument();
    expect(within(rb).getByText("removed, verified")).toBeInTheDocument();
    expect(within(rb).getByText("removing…")).toBeInTheDocument();
    expect(within(rb).getByText("2 of 3")).toBeInTheDocument();
    expect(within(card(/Policy check/)).getByText("2 fault(s) on 2 service(s)")).toBeInTheDocument();
  });

  it("replays a saved run from its record, labelled as a replay, with no animation", async () => {
    mocked.experiment.mockResolvedValue({ record: done({ abort_reason: "search endpoint failed on all of the last 3 ticks" }), analysis: analysis(), effects: {}, learning: null, recommendations: [], agent_runs: [agentRun("recovery", { summary: "RECOVERED: 1 of 1 fault(s) verified removed" })], timeline: [], followups: [] });
    await show(liveState(), "/pipeline/exp-1");
    expect(await screen.findByText("saved run: replay")).toBeInTheDocument();
    expect(within(card(/Monitor/)).getByText("stopped")).toBeInTheDocument();
    expect(chip("recovery")).toHaveTextContent("RECOVERED: 1 of 1 fault(s) verified removed");
    expect(document.querySelector("[data-anim]")).toBeNull();
    expect(document.querySelector(".pl-pulse")).toBeNull();
  });
});

// ================================================================ agents lane
describe("Agents lane", () => {
  const agents = ALL_AGENTS.map((n) => agentStatus(n, { title: n, kind: n === "insights" ? "hybrid" : "deterministic", provider: n === "insights" ? { provider: "gemini", model: "m", enabled: false } : null }));

  it("shows the running agent, the next one as queued, and each agent's type", async () => {
    const live = agents.map((a) => (a.name === "recovery" ? { ...a, last: { status: "completed", experiment_id: "exp-1", summary: "RECOVERED: 1 of 1 fault(s) verified removed", duration_ms: 31 } } : a.name === "validation" ? { ...a, state: "running", task: "exp-1" } : a));
    await show(liveState({ agents: live, orchestrator: orchestrator({ status: "analyzing", record: done() }) }));
    expect(chip("recovery")).toHaveAttribute("data-state", "completed");
    expect(chip("recovery")).toHaveTextContent("RECOVERED: 1 of 1 fault(s) verified removed");
    expect(chip("recovery")).toHaveTextContent("31 ms");
    expect(chip("validation")).toHaveAttribute("data-state", "running");
    expect(chip("insights")).toHaveTextContent("queued: next");
    expect(chip("learning")).toHaveAttribute("data-state", "queued");
    expect(chip("insights")).toHaveTextContent("LLM off: rules only");
    expect(chip("recovery")).toHaveTextContent("deterministic");
    expect(within(card(/Analyse/)).getByText("active")).toBeInTheDocument();
  });

  it("says LLM on when the provider is enabled, and shows a failed agent with its error", async () => {
    const on = agents.map((a) => (a.name === "insights" ? { ...a, provider: { provider: "gemini", model: "m", enabled: true } } : a));
    await show(liveState({ agents: on, orchestrator: orchestrator({ status: "done", record: done(), agent_runs: [agentRun("insights", { status: "failed", error: "ValueError: bad schema" })] }) }));
    expect(chip("insights")).toHaveTextContent("LLM on");
    expect(chip("insights")).toHaveAttribute("data-state", "failed");
    expect(chip("insights")).toHaveTextContent("ValueError: bad schema");
  });

  it("opens an agent's stored run, read-only, with its input reference and output document", async () => {
    mockedRequest.mockResolvedValue([{ ...agentRun("recovery", { summary: "RECOVERED" }), input_ref: "exp-1", output: { state: "RECOVERED", verified: 1 } }]);
    await show(liveState({ agents, orchestrator: orchestrator({ status: "done", record: done(), agent_runs: [agentRun("recovery")] }) }));
    await userEvent.click(chip("recovery"));
    const dialog = await screen.findByRole("dialog");
    expect(mockedRequest.mock.calls[0][0]).toBe("/api/agents/runs?agent=recovery&experiment_id=exp-1&limit=1");
    expect(await within(dialog).findByText("Output document (validated, read-only)")).toBeInTheDocument();
    expect(within(dialog).getByText(/"state": "RECOVERED"/)).toBeInTheDocument();
    expect(within(dialog).getByText("exp-1")).toBeInTheDocument();
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
  });
});

// ================================================================ transitions
describe("state-change motion", () => {
  const monitoring = () => liveState({ orchestrator: orchestrator({ status: "running", record: record({ state: "MONITORING" }) }) });
  const reverting = () => liveState({ orchestrator: orchestrator({ status: "running", record: record({ state: "REVERTING" }) }) });
  const failed = () => liveState({ orchestrator: orchestrator({ status: "error", record: record({ state: "RECOVERY_REQUIRED", rollback_verified: false, rollback_error: "x" }) }) });

  for (const reduce of [false, true]) {
    it(`active to done ends on the right label with motion ${reduce ? "off" : "on"}`, async () => {
      setReducedMotion(reduce);
      const c = mutableClient(monitoring());
      await show(monitoring(), "/pipeline", c.client);
      expect(within(card(/Monitor/)).getByText("active")).toBeInTheDocument();
      expect(card(/Monitor/)).not.toHaveAttribute("data-anim");
      c.set(reverting());
      expect(within(card(/Monitor/)).getByText("done")).toBeInTheDocument();
      expect(within(card(/Roll back and verify/)).getByText("active")).toBeInTheDocument();
      if (reduce) {
        expect(document.querySelector("[data-anim]")).toBeNull();
        expect(document.querySelector(".pl-pulse, .pl-fill, .pl-draw")).toBeNull();
      } else {
        expect(card(/Monitor/)).toHaveAttribute("data-anim", "pl-settle");
        expect(within(card(/Monitor/)).getByTestId("connector-fill")).toHaveClass("pl-fill");
      }
    });

    it(`active to failed ends on the right label with motion ${reduce ? "off" : "on"}`, async () => {
      setReducedMotion(reduce);
      const c = mutableClient(reverting());
      await show(reverting(), "/pipeline", c.client);
      c.set(failed());
      const rb = card(/Roll back and verify/);
      expect(within(rb).getByText("failed")).toBeInTheDocument();
      if (reduce) expect(rb).not.toHaveAttribute("data-anim");
      else expect(rb).toHaveAttribute("data-anim", "pl-shake");
    });
  }

  it("a catch-up of several stages at once applies the final state without animating", async () => {
    const start = liveState({ orchestrator: orchestrator({ status: "running", record: record({ state: "PRECHECK" }) }) });
    const c = mutableClient(start);
    await show(start, "/pipeline", c.client);
    c.set(liveState({ orchestrator: orchestrator({ status: "done", record: done(), analysis: analysis() }) }));
    expect(within(card(/Analyse/)).getByText("done")).toBeInTheDocument();
    expect(document.querySelector("[data-anim]")).toBeNull();
    expect(document.querySelector(".pl-sweep")).toBeNull();
  });

  it("does not animate the first render of a finished run", async () => {
    await show(liveState({ orchestrator: orchestrator({ status: "done", record: done(), analysis: analysis() }) }));
    expect(document.querySelector("[data-anim]")).toBeNull();
    expect(document.querySelector(".pl-slide, .pl-sweep, .pl-fill")).toBeNull();
  });

  it("run completion sweeps the cards once and slides the banner in", async () => {
    const analysing = liveState({ orchestrator: orchestrator({ status: "analyzing", record: done() }) });
    const c = mutableClient(analysing);
    await show(analysing, "/pipeline", c.client);
    c.set(liveState({ orchestrator: orchestrator({ status: "done", record: done(), analysis: analysis() }) }));
    expect(within(card(/Analyse/)).getByText("done")).toBeInTheDocument();
    expect(document.querySelectorAll(".pl-sweep")).toHaveLength(8);
    expect(document.querySelector(".pl-slide")).toHaveTextContent("Completed.");
  });
});
