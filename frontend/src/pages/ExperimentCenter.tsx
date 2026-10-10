import { useState } from "react";
import { api, ApiError, type Proposal } from "../api";
import { usePolling } from "../hooks";
import { Panel, ErrorState, EmptyState } from "../components/Panel";
import { StatusBadge, orchestratorTone, severityTone } from "../components/StatusBadge";

const LIFECYCLE_STATES = [
  "PRECHECK", "BASELINE", "INJECTING", "MONITORING", "REVERTING", "VERIFYING_RECOVERY", "COMPLETED",
];

function LifecycleStepper({ current }: { current: string | undefined }) {
  const terminalUnhappy = current && !LIFECYCLE_STATES.includes(current);
  const idx = current ? LIFECYCLE_STATES.indexOf(current) : -1;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {LIFECYCLE_STATES.map((s, i) => (
        <span
          key={s}
          className={`rounded-md px-2 py-1 text-[11px] font-medium ${
            i === idx
              ? "bg-accent/20 text-accent"
              : i < idx
                ? "bg-good/10 text-good"
                : "bg-white/5 text-muted"
          }`}
        >
          {s}
        </span>
      ))}
      {terminalUnhappy && <StatusBadge label={current!} tone="bad" />}
    </div>
  );
}

export function ExperimentCenter() {
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [authorize, setAuthorize] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [busy, setBusy] = useState(false);

  const { data: orch, refresh: refreshStatus } = usePolling(() => api.orchestratorStatus(), 3000);
  const isRunning = orch?.status === "running" || orch?.status === "analyzing";

  async function generatePlan() {
    setPlanError(null);
    setBusy(true);
    try {
      const p = await api.plan();
      setProposal(p);
      setAuthorize(false);
    } catch (e) {
      setPlanError(e instanceof ApiError ? e.message : "Failed to reach the planner agent.");
    } finally {
      setBusy(false);
    }
  }

  async function runProposal() {
    if (!proposal) return;
    setActionError(null);
    setBusy(true);
    try {
      await api.runExperiment(proposal.proposal_id, authorize || dryRun, dryRun);
      await refreshStatus();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : "Failed to start the experiment.");
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    if (!orch?.experiment_id) return;
    setBusy(true);
    try {
      await api.stopExperiment(orch.experiment_id);
      await refreshStatus();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : "Failed to stop the experiment.");
    } finally {
      setBusy(false);
    }
  }

  const canAuthorizeReal = !!proposal && authorize && !dryRun;
  const canRun = !!proposal && !isRunning && (dryRun || authorize);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">AI Experiment Center</h1>
        <p className="text-sm text-muted">
          The planner proposes; it never executes. A real fault only runs after a human explicitly authorizes it below.
        </p>
      </div>

      {planError && <ErrorState message={planError} />}
      {actionError && <ErrorState message={actionError} />}

      <Panel
        title="Proposed experiment"
        subtitle={proposal ? `method: ${proposal.method}` : "No proposal generated yet"}
        actions={
          <>
            <button
              onClick={generatePlan}
              disabled={busy}
              className="rounded-md bg-accent/15 px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent/25 disabled:opacity-50"
            >
              {proposal ? "Refresh proposal" : "Generate plan"}
            </button>
          </>
        }
      >
        {!proposal ? (
          <EmptyState>Click "Generate plan" to ask the planner agent for a candidate experiment.</EmptyState>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge label={`fault: ${proposal.fault}`} tone="accent" />
              <StatusBadge label={`target: ${proposal.target}`} tone="accent" />
              <StatusBadge label={`risk: ${proposal.risk}`} tone={severityTone(proposal.risk === "high" ? "high" : proposal.risk === "medium" ? "medium" : "low")} />
              {Object.keys(proposal.params).length > 0 && (
                <StatusBadge label={`params: ${JSON.stringify(proposal.params)}`} tone="muted" />
              )}
            </div>

            <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs uppercase text-muted">Hypothesis</dt>
                <dd className="text-slate-200">{proposal.hypothesis}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase text-muted">Expected impact</dt>
                <dd className="text-slate-200">{proposal.expected_impact}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase text-muted">Reason for selection</dt>
                <dd className="text-slate-200">{proposal.reason}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase text-muted">Success criteria</dt>
                <dd className="text-slate-200">{proposal.success_criteria}</dd>
              </div>
              <div>
                <dt className="text-xs uppercase text-muted">Relevant dependencies</dt>
                <dd className="text-slate-200">
                  {proposal.dependencies.length ? proposal.dependencies.join(", ") : "none listed"}
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase text-muted">Duration / recovery timeout</dt>
                <dd className="text-slate-200">
                  {proposal.max_duration_s}s / {proposal.recovery_timeout_s}s
                </dd>
              </div>
            </dl>

            <div className="rounded-lg border border-border bg-panel2 p-4">
              <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted">Run this proposal</div>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex flex-col gap-2">
                  <label className="flex items-center gap-2 text-sm text-slate-200">
                    <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
                    Preview only (dry-run — no real fault, zero Docker commands)
                  </label>
                  <label className="flex items-center gap-2 text-sm text-slate-200">
                    <input
                      type="checkbox"
                      checked={authorize}
                      onChange={(e) => setAuthorize(e.target.checked)}
                      disabled={dryRun}
                    />
                    I am an operator and I authorize a REAL fault injection against{" "}
                    <span className="font-mono text-accent">{proposal.target}</span>
                  </label>
                </div>
                <button
                  onClick={runProposal}
                  disabled={!canRun || busy}
                  className={`rounded-md px-4 py-2 text-sm font-medium disabled:opacity-40 ${
                    canAuthorizeReal ? "bg-bad text-white hover:bg-bad/90" : "bg-accent text-white hover:bg-accent/90"
                  }`}
                >
                  {dryRun ? "Preview experiment" : "Authorize & run REAL experiment"}
                </button>
              </div>
              {!dryRun && !authorize && (
                <p className="mt-2 text-xs text-warn">
                  An AI proposal can never authorize itself — check the box above to enable a real run.
                </p>
              )}
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="Experiment lifecycle"
        subtitle={orch?.experiment_id ? `experiment: ${orch.experiment_id}` : "no experiment running"}
        actions={
          isRunning && (
            <button
              onClick={stop}
              disabled={busy}
              className="rounded-md bg-bad/15 px-3 py-1.5 text-xs font-medium text-bad hover:bg-bad/25 disabled:opacity-50"
            >
              Stop experiment
            </button>
          )
        }
      >
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <StatusBadge label={orch?.status ?? "idle"} tone={orchestratorTone(orch?.status)} pulse={isRunning} />
            {orch?.error && <StatusBadge label={orch.error} tone="bad" />}
          </div>
          <LifecycleStepper current={orch?.record?.state} />
          {orch?.record && (orch.record.error || orch.record.abort_reason) && (
            <div
              className={`rounded-lg border px-3 py-2 text-xs ${
                orch.record.injected ? "border-bad/30 bg-bad/5 text-bad" : "border-warn/30 bg-warn/5 text-warn"
              }`}
            >
              <span className="font-semibold">
                {orch.record.injected ? "A real fault was injected. " : "Refused before anything was touched — "}
              </span>
              {orch.record.error ?? orch.record.abort_reason}
            </div>
          )}
        </div>
      </Panel>
    </div>
  );
}
