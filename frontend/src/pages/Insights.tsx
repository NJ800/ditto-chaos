import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import { usePolling } from "../hooks";
import { Panel, EmptyState, ErrorState } from "../components/Panel";
import { FindingsPanel } from "../components/FindingsPanel";
import { RemediationPanel } from "../components/RemediationPanel";

export function Insights() {
  const { data: insights, error, refresh } = usePolling(() => api.latestInsights(), 10000);
  const { data: experiments } = usePolling(() => api.listExperiments(), 10000);
  const [busy, setBusy] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);

  const latestUnanalyzed = experiments?.find((e) => !e.analyzed && e.state !== "RUNNING");

  async function generate() {
    const target = latestUnanalyzed ?? experiments?.[0];
    if (!target) return;
    setBusy(true);
    setGenError(null);
    try {
      await api.analyzeExperiment(target.id);
      await refresh();
    } catch (e) {
      setGenError(e instanceof ApiError ? e.message : "Failed to generate insights.");
    } finally {
      setBusy(false);
    }
  }

  const is404 = error?.toLowerCase().includes("no analysis reports");

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">AI Insights & Remediation</h1>
          <p className="text-sm text-muted">The most recent evidence-grounded analysis, with its remediation plan.</p>
        </div>
        <button
          onClick={generate}
          disabled={busy || !experiments?.length}
          className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent/90 disabled:opacity-50"
        >
          {busy ? "Generating…" : latestUnanalyzed ? `Analyze ${latestUnanalyzed.id}` : "Re-analyze latest"}
        </button>
      </div>

      {genError && <ErrorState message={genError} />}

      {is404 || !insights ? (
        <Panel>
          <EmptyState>
            {experiments?.length
              ? "No analysis has been generated yet. Click \"Analyze\" above."
              : "No experiments have run yet. Start one from the AI Experiment Center."}
          </EmptyState>
        </Panel>
      ) : (
        <>
          <div className="text-xs text-muted">
            For experiment{" "}
            <Link to={`/history/${insights.analysis.experiment_id}`} className="font-mono text-accent hover:underline">
              {insights.analysis.experiment_id}
            </Link>
          </div>
          <FindingsPanel analysis={insights.analysis} />
          {insights.remediation_plan && (
            <RemediationPanel experimentId={insights.analysis.experiment_id} plan={insights.remediation_plan} onChanged={refresh} />
          )}
        </>
      )}
    </div>
  );
}
