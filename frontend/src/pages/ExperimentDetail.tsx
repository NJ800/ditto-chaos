import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { usePolling } from "../hooks";
import { Panel, EmptyState, ErrorState } from "../components/Panel";
import { StatusBadge } from "../components/StatusBadge";
import { FindingsPanel } from "../components/FindingsPanel";
import { RemediationPanel } from "../components/RemediationPanel";

export function ExperimentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data: detail, error, refresh } = usePolling(() => api.getExperiment(id!), 8000, [id]);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);

  async function runAnalysis() {
    if (!id) return;
    setAnalyzing(true);
    setAnalyzeError(null);
    try {
      await api.analyzeExperiment(id);
      await refresh();
    } catch (e) {
      setAnalyzeError(e instanceof ApiError ? e.message : "Analysis failed.");
    } finally {
      setAnalyzing(false);
    }
  }

  if (error) return <ErrorState message={error} />;
  if (!detail) return <EmptyState>Loading…</EmptyState>;

  const { record, analysis, remediation_plan } = detail;
  const beforeRows = record.timeline.filter((r) => r.phase === "before");
  const faultRows = record.timeline.filter((r) => r.phase === "fault");
  const recoveryRows = record.timeline.filter((r) => r.phase === "recovery");

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Link to="/history" className="text-xs text-accent hover:underline">
          ← back to history
        </Link>
        <h1 className="mt-1 font-mono text-lg font-semibold text-slate-100">{record.id}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <StatusBadge label={record.state} tone={record.state === "COMPLETED" ? "good" : record.state.includes("FAIL") ? "bad" : "muted"} />
          <StatusBadge label={`fault: ${record.fault}`} tone="accent" />
          <StatusBadge label={`target: ${record.target}`} tone="accent" />
          {record.dry_run && <StatusBadge label="dry run" tone="muted" />}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Panel title="Baseline ticks"><p className="text-2xl font-semibold">{beforeRows.length}</p></Panel>
        <Panel title="Fault-period ticks"><p className="text-2xl font-semibold">{faultRows.length}</p></Panel>
        <Panel title="Recovery ticks"><p className="text-2xl font-semibold">{recoveryRows.length}</p></Panel>
        <Panel title="Rollback">
          <StatusBadge label={record.rollback_verified ? "verified" : "NOT verified"} tone={record.rollback_verified ? "good" : "bad"} />
        </Panel>
      </div>

      <Panel title="Timeline (raw probe ticks)">
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-panel">
              <tr className="border-b border-border text-muted">
                <th className="py-1.5 pr-3">phase</th>
                <th className="py-1.5 pr-3">time</th>
                <th className="py-1.5 pr-3">direct</th>
                <th className="py-1.5 pr-3">search</th>
                <th className="py-1.5 pr-3">stale p95</th>
                <th className="py-1.5 pr-3">lost acked</th>
              </tr>
            </thead>
            <tbody>
              {record.timeline.map((r, i) => (
                <tr key={i} className="border-b border-border/40">
                  <td className="py-1 pr-3 text-muted">{r.phase}</td>
                  <td className="py-1 pr-3">{new Date(r.ts * 1000).toLocaleTimeString()}</td>
                  <td className={`py-1 pr-3 ${r.direct_ok ? "text-good" : "text-bad"}`}>{r.direct_ms?.toFixed(0)}ms</td>
                  <td className={`py-1 pr-3 ${r.search_ok ? "text-good" : "text-bad"}`}>{r.search_ms?.toFixed(0)}ms</td>
                  <td className="py-1 pr-3">{r.stale_p95_s ?? "—"}</td>
                  <td className={`py-1 pr-3 ${r.lost_acked ? "text-bad font-semibold" : ""}`}>{r.lost_acked ?? "unknown"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {analyzeError && <ErrorState message={analyzeError} />}

      {analysis ? (
        <>
          <FindingsPanel analysis={analysis} />
          {remediation_plan && <RemediationPanel experimentId={record.id} plan={remediation_plan} onChanged={refresh} />}
        </>
      ) : (
        <Panel title="AI findings">
          <EmptyState>
            <div className="flex flex-col items-center gap-3">
              <span>Not analyzed yet.</span>
              <button
                onClick={runAnalysis}
                disabled={analyzing}
                className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent/90 disabled:opacity-50"
              >
                {analyzing ? "Analyzing…" : "Run AI analysis"}
              </button>
            </div>
          </EmptyState>
        </Panel>
      )}
    </div>
  );
}
