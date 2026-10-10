import { Link } from "react-router-dom";
import { api } from "../api";
import { usePolling } from "../hooks";
import { Panel, EmptyState, ErrorState } from "../components/Panel";
import { StatusBadge } from "../components/StatusBadge";

export function History() {
  const { data: experiments, error, loading } = usePolling(() => api.listExperiments(), 10000);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Experiment History</h1>
        <p className="text-sm text-muted">Every experiment guard.py has run and saved, newest first.</p>
      </div>

      {error && <ErrorState message={error} />}

      <Panel>
        {loading && !experiments ? (
          <EmptyState>Loading…</EmptyState>
        ) : !experiments || experiments.length === 0 ? (
          <EmptyState>No experiments yet. Generate a plan in the AI Experiment Center.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-border text-xs uppercase text-muted">
                  <th className="py-2 pr-4">ID</th>
                  <th className="py-2 pr-4">Started</th>
                  <th className="py-2 pr-4">Fault</th>
                  <th className="py-2 pr-4">Target</th>
                  <th className="py-2 pr-4">Duration</th>
                  <th className="py-2 pr-4">Kill switch</th>
                  <th className="py-2 pr-4">Rollback</th>
                  <th className="py-2 pr-4">Recovery</th>
                  <th className="py-2 pr-4">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {experiments.map((e) => {
                  const durationS =
                    e.ended_at_utc && e.started_at_utc
                      ? Math.round((new Date(e.ended_at_utc).getTime() - new Date(e.started_at_utc).getTime()) / 1000)
                      : null;
                  return (
                    <tr key={e.id} className="border-b border-border/60 hover:bg-white/5">
                      <td className="py-2 pr-4">
                        <Link to={`/history/${e.id}`} className="font-mono text-xs text-accent hover:underline">
                          {e.id}
                        </Link>
                      </td>
                      <td className="py-2 pr-4 text-xs text-muted">{new Date(e.started_at_utc).toLocaleString()}</td>
                      <td className="py-2 pr-4">{e.fault}</td>
                      <td className="py-2 pr-4">{e.target}</td>
                      <td className="py-2 pr-4">{durationS !== null ? `${durationS}s` : "—"}</td>
                      <td className="py-2 pr-4">
                        {e.aborted ? <StatusBadge label="triggered" tone="warn" /> : <StatusBadge label="no" tone="muted" />}
                      </td>
                      <td className="py-2 pr-4">
                        {e.injected === false ? (
                          <StatusBadge label="n/a (never injected)" tone="muted" />
                        ) : e.rollback_verified ? (
                          <StatusBadge label="verified" tone="good" />
                        ) : (
                          <StatusBadge label="unverified" tone="bad" />
                        )}
                      </td>
                      <td className="py-2 pr-4">{e.recovery_s !== null ? `${e.recovery_s}s` : e.recovery_outcome ?? "—"}</td>
                      <td className="py-2 pr-4">
                        <StatusBadge label={e.state} tone={e.state === "COMPLETED" ? "good" : e.state.includes("FAIL") ? "bad" : "muted"} />
                        {(e.error || e.abort_reason) && (
                          <div className="mt-1 max-w-xs text-[11px] text-muted" title={e.error ?? e.abort_reason ?? undefined}>
                            {e.error ?? e.abort_reason}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
