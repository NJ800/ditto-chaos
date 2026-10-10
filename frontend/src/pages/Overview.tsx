import { api } from "../api";
import { usePolling } from "../hooks";
import { Panel, ErrorState } from "../components/Panel";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge, healthTone, orchestratorTone } from "../components/StatusBadge";

function lifecycleLabel(orchStatus: string | undefined, recordState: string | undefined, recoveryOutcome: string | null | undefined) {
  if (orchStatus === "running") {
    if (recordState === "REVERTING") return { label: "Rollback in progress", tone: "warn" as const };
    if (recordState === "VERIFYING_RECOVERY") return { label: "Verifying recovery", tone: "accent" as const };
    return { label: "Experiment running", tone: "accent" as const };
  }
  if (orchStatus === "analyzing") return { label: "Analyzing results", tone: "accent" as const };
  if (orchStatus === "done") {
    if (recoveryOutcome === "recovered") return { label: "Recovery verified", tone: "good" as const };
    if (recoveryOutcome === "timeout") return { label: "Recovery failed", tone: "bad" as const };
    return { label: "Experiment complete", tone: "muted" as const };
  }
  if (orchStatus === "error") return { label: "Orchestrator error", tone: "bad" as const };
  return null;
}

function fmt(n: number | null | undefined, digits = 1) {
  return n === null || n === undefined ? null : Number(n.toFixed(digits));
}

export function Overview() {
  const { data: health, error: healthError, loading: healthLoading } = usePolling(() => api.health(10), 5000);
  const { data: baseline } = usePolling(() => api.baseline(), 20000);
  const { data: orch } = usePolling(() => api.orchestratorStatus(), 4000);

  const lifecycle = lifecycleLabel(orch?.status, orch?.record?.state, orch?.record?.recovery_outcome);
  const dittoConnected = health ? health.status !== "unknown" : undefined;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Overview</h1>
        <p className="text-sm text-muted">Live resilience posture of this Eclipse Ditto instance.</p>
      </div>

      {healthError && <ErrorState message={healthError} />}

      <div className="flex flex-wrap items-center gap-3">
        <StatusBadge
          label={`Ditto: ${health ? health.status : healthLoading ? "checking…" : "unreachable"}`}
          tone={healthTone(health?.status)}
        />
        <StatusBadge
          label={`connection: ${dittoConnected === undefined ? "…" : dittoConnected ? "connected" : "no recent probe data"}`}
          tone={dittoConnected ? "good" : "muted"}
        />
        {lifecycle && <StatusBadge label={lifecycle.label} tone={lifecycle.tone} pulse={lifecycle.tone === "accent"} />}
        {health && !health.fresh && (
          <StatusBadge label="metrics are STALE -- is `python probe.py` running?" tone="warn" />
        )}
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        <MetricCard label="Monitored cars" value={health?.num_cars ?? null} />
        <MetricCard
          label="Direct-read p95"
          value={fmt(health?.direct_p95_ms)}
          unit="ms"
          tone={health?.direct_error_rate ? "bad" : "default"}
        />
        <MetricCard
          label="Search p95"
          value={fmt(health?.search_p95_ms)}
          unit="ms"
          tone={health?.search_error_rate ? "bad" : "default"}
        />
        <MetricCard
          label="Error rate"
          value={health?.error_rate !== null && health?.error_rate !== undefined ? `${(health.error_rate * 100).toFixed(1)}` : null}
          unit="%"
          tone={health?.error_rate ? (health.error_rate > 0.05 ? "bad" : "warn") : "good"}
        />
        <MetricCard
          label="Twin staleness p95"
          value={fmt(health?.stale_p95_s, 2)}
          unit="s"
          tone={health?.stale_p95_s && health.stale_p95_s > 15 ? "bad" : "default"}
        />
        <MetricCard
          label="Lost acked updates"
          value={health?.lost_acked ?? null}
          tone={health?.lost_acked ? "bad" : health?.lost_acked === 0 ? "good" : "default"}
          hint={health?.lost_acked === null ? "unknown -- simulator's state/sent.json not available" : undefined}
        />
        <MetricCard label="Search drift (max)" value={health?.search_drift ?? null} />
        <MetricCard
          label="Last measurement"
          value={health?.latest_timestamp ? new Date(health.latest_timestamp * 1000).toLocaleTimeString() : null}
          hint={health ? `window: ${health.window_s}s, ${health.sample_count} sample(s)` : undefined}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Baseline reference" subtitle="Recorded via `python main.py baseline`">
          {baseline?.present && baseline.baseline ? (
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-muted">Direct p95</dt>
                <dd className="text-slate-200">{baseline.baseline.direct_ms.p95 ?? "—"} ms</dd>
              </div>
              <div>
                <dt className="text-muted">Search p95</dt>
                <dd className="text-slate-200">{baseline.baseline.search_ms.p95 ?? "—"} ms</dd>
              </div>
              <div>
                <dt className="text-muted">Staleness p95</dt>
                <dd className="text-slate-200">{baseline.baseline.stale_p95_s ?? "—"} s</dd>
              </div>
              <div>
                <dt className="text-muted">Recorded</dt>
                <dd className="text-slate-200">{new Date(baseline.baseline.recorded_at * 1000).toLocaleString()}</dd>
              </div>
            </dl>
          ) : (
            <p className="text-sm text-muted">No baseline recorded yet. Run `python main.py baseline --duration 120`.</p>
          )}
        </Panel>

        <Panel title="Last recovery result">
          {orch?.record ? (
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-muted">Experiment</dt>
                <dd className="font-mono text-xs text-slate-200">{orch.record.id}</dd>
              </div>
              <div>
                <dt className="text-muted">State</dt>
                <dd className="text-slate-200">{orch.record.state}</dd>
              </div>
              <div>
                <dt className="text-muted">Reverted / verified</dt>
                <dd className="text-slate-200">
                  {orch.record.reverted ? "yes" : "no"} / {orch.record.rollback_verified ? "yes" : "no"}
                </dd>
              </div>
              <div>
                <dt className="text-muted">Recovery time</dt>
                <dd className="text-slate-200">
                  {orch.record.recovery_s !== null ? `${orch.record.recovery_s}s` : orch.record.recovery_outcome ?? "—"}
                </dd>
              </div>
            </dl>
          ) : (
            <p className="text-sm text-muted">No experiment has run yet in this session. See the AI Experiment Center.</p>
          )}
        </Panel>
      </div>
    </div>
  );
}
