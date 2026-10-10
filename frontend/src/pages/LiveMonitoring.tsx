import { useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api";
import { usePolling } from "../hooks";
import { Panel, EmptyState, ErrorState } from "../components/Panel";
import { StatusBadge } from "../components/StatusBadge";

const WINDOWS = [
  { label: "5 min", s: 300 },
  { label: "15 min", s: 900 },
  { label: "60 min", s: 3600 },
];

function timeLabel(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

export function LiveMonitoring() {
  const [windowS, setWindowS] = useState(300);
  const { data: history, error } = usePolling(() => api.metricsHistory(windowS), 5000, [windowS]);
  const { data: baseline } = usePolling(() => api.baseline(), 20000);
  const { data: thresholds } = usePolling(() => api.thresholds(), 30000);

  const rows = history?.rows ?? [];
  const chartData = rows.map((r) => ({
    t: timeLabel(r.ts),
    direct_ms: r.direct_ok ? r.direct_ms : null,
    search_ms: r.search_ok ? r.search_ms : null,
    stale_p95_s: r.stale_p95_s,
    direct_failed: r.direct_ok ? 0 : 1,
    search_failed: r.search_ok ? 0 : 1,
    lost_acked: r.lost_acked ?? 0,
    search_drift: r.search_drift ?? 0,
  }));

  const baselineDirectP95 = baseline?.baseline?.direct_ms.p95 ?? null;
  const baselineSearchP95 = baseline?.baseline?.search_ms.p95 ?? null;
  const latencyThreshold =
    thresholds && baselineDirectP95
      ? Math.max(baselineDirectP95 * thresholds.max_latency_factor, thresholds.min_latency_floor_ms)
      : null;
  const killSwitchEvents = rows.filter((r) => (r.lost_acked ?? 0) > 0);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Live Monitoring</h1>
          <p className="text-sm text-muted">Real probe data from metrics/probe-*.jsonl. Polled every 5s.</p>
        </div>
        <div className="flex gap-1 rounded-lg border border-border bg-panel2 p-1">
          {WINDOWS.map((w) => (
            <button
              key={w.s}
              onClick={() => setWindowS(w.s)}
              className={`rounded-md px-3 py-1 text-xs font-medium ${
                windowS === w.s ? "bg-accent text-white" : "text-muted hover:text-slate-200"
              }`}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {error && <ErrorState message={error} />}
      {history?.stale_note && <StatusBadge label={history.stale_note} tone="warn" />}

      <Panel title="Direct-read & search latency" subtitle="Dashed lines = recorded baseline p95 / configured kill-switch threshold">
        {chartData.length === 0 ? (
          <EmptyState>No probe data in this window. Start `python probe.py`.</EmptyState>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={chartData}>
              <CartesianGrid stroke="#223049" strokeDasharray="3 3" />
              <XAxis dataKey="t" stroke="#8ea0bf" fontSize={11} minTickGap={30} />
              <YAxis
                stroke="#8ea0bf"
                fontSize={11}
                unit="ms"
                domain={[0, (dataMax: number) => Math.ceil(Math.max(dataMax, latencyThreshold ?? 0) * 1.15)]}
              />
              <Tooltip contentStyle={{ background: "#111a2e", border: "1px solid #223049", fontSize: 12 }} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line type="monotone" dataKey="direct_ms" name="direct p95" stroke="#3b9eff" dot={false} connectNulls />
              <Line type="monotone" dataKey="search_ms" name="search ms" stroke="#2bd576" dot={false} connectNulls />
              {baselineDirectP95 && (
                <ReferenceLine y={baselineDirectP95} stroke="#3b9eff" strokeDasharray="4 4" label={{ value: "baseline direct", fontSize: 10, fill: "#8ea0bf" }} />
              )}
              {baselineSearchP95 && (
                <ReferenceLine y={baselineSearchP95} stroke="#2bd576" strokeDasharray="4 4" />
              )}
              {latencyThreshold && (
                <ReferenceLine y={latencyThreshold} stroke="#f05252" strokeDasharray="4 4" label={{ value: "kill-switch threshold", fontSize: 10, fill: "#f05252" }} />
              )}
            </LineChart>
          </ResponsiveContainer>
        )}
      </Panel>

      <Panel title="Twin staleness" subtitle={thresholds ? `kill-switch threshold: ${thresholds.max_stale_s}s` : undefined}>
        {chartData.length === 0 ? (
          <EmptyState>No probe data in this window.</EmptyState>
        ) : (
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={chartData}>
              <CartesianGrid stroke="#223049" strokeDasharray="3 3" />
              <XAxis dataKey="t" stroke="#8ea0bf" fontSize={11} minTickGap={30} />
              <YAxis
                stroke="#8ea0bf"
                fontSize={11}
                unit="s"
                domain={[0, (dataMax: number) => Math.ceil(Math.max(dataMax, thresholds?.max_stale_s ?? 0) * 1.15)]}
              />
              <Tooltip contentStyle={{ background: "#111a2e", border: "1px solid #223049", fontSize: 12 }} />
              <Line type="monotone" dataKey="stale_p95_s" name="staleness p95" stroke="#f5a623" dot={false} connectNulls />
              {thresholds && (
                <ReferenceLine y={thresholds.max_stale_s} stroke="#f05252" strokeDasharray="4 4" />
              )}
            </LineChart>
          </ResponsiveContainer>
        )}
      </Panel>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Kill-switch events (confirmed lost acknowledged updates)">
          {killSwitchEvents.length === 0 ? (
            <EmptyState>None in this window — good.</EmptyState>
          ) : (
            <ul className="flex flex-col gap-1 text-sm text-bad">
              {killSwitchEvents.slice(-10).map((r, i) => (
                <li key={i}>
                  {timeLabel(r.ts)} — {r.lost_acked} car(s) with a confirmed lost update
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel title="Search index drift (max per tick)">
          {chartData.every((d) => !d.search_drift) ? (
            <EmptyState>No drift observed in this window.</EmptyState>
          ) : (
            <ResponsiveContainer width="100%" height={160}>
              <LineChart data={chartData}>
                <CartesianGrid stroke="#223049" strokeDasharray="3 3" />
                <XAxis dataKey="t" stroke="#8ea0bf" fontSize={11} minTickGap={30} />
                <YAxis stroke="#8ea0bf" fontSize={11} />
                <Tooltip contentStyle={{ background: "#111a2e", border: "1px solid #223049", fontSize: 12 }} />
                <Line type="monotone" dataKey="search_drift" stroke="#f5a623" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </Panel>
      </div>
    </div>
  );
}
