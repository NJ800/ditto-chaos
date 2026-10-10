import { useMemo, useState } from "react";
import type { MetricRow } from "../api/types";
import { MetricChart, faultWindows } from "../components/MetricChart";
import { Note, PageHeader, Panel, Stat, StateBadge } from "../components/ui";
import { ago, clock } from "../lib/format";
import { useLive, useNow } from "../live/LiveContext";
import { healthLabel } from "../live/stream";
import { ServicesTable } from "./Overview";

const WINDOWS = [
  { label: "5 min", s: 300 },
  { label: "15 min", s: 900 },
  { label: "1 h", s: 3600 },
];

export function Monitoring() {
  const live = useLive();
  const now = useNow();
  const [windowS, setWindowS] = useState(900);
  const rows = useMemo<MetricRow[]>(() => {
    const cutoff = now / 1000 - windowS;
    return live.rows.filter((r) => r.ts >= cutoff);
    // re-slice when new rows arrive or the window changes; `now` is read but a 1s drift is irrelevant
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live.rows, windowS]);
  const label = healthLabel(live, now);
  const th = live.thresholds;
  const b95 = live.baseline?.direct_ms?.p95 ?? null;
  const latencyLimit = th && b95 ? Math.max(b95 * th.max_latency_factor, th.min_latency_floor_ms) : null;
  const failed = rows.filter((r) => !r.direct_ok || !r.search_ok).length;
  const timeouts = rows.filter((r) => r.probe_timeout).length;
  const windows = faultWindows(rows);
  const last = rows.length ? rows[rows.length - 1] : null;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Live monitoring"
        sub="Measurements from probe.py and, during an experiment, from the controller's own probe. Shaded bands mark intervals with an active fault."
        actions={
          <div role="group" aria-label="Time window" className="flex overflow-hidden rounded border border-line">
            {WINDOWS.map((w) => (
              <button key={w.s} type="button" aria-pressed={windowS === w.s} onClick={() => setWindowS(w.s)} className={`h-8 px-3 text-xs ${windowS === w.s ? "bg-raised text-ink" : "text-dim hover:text-ink"}`}>
                {w.label}
              </button>
            ))}
          </div>
        }
      />

      {label !== "HEALTHY" && label !== "DEGRADED" && (
        <Note tone="warn" role="status">
          Target state is <strong className="text-ink">{label}</strong>: the newest sample is {ago(last?.ts ?? live.health?.latest_timestamp, now)}. The charts show recorded history, not the present.
        </Note>
      )}

      <Panel title="Window summary" actions={<StateBadge state={label} />}>
        <div className="grid grid-cols-2 gap-x-3 gap-y-4 md:grid-cols-5">
          <Stat label="Samples" value={rows.length} sub={last ? `newest ${clock(last.ts)}` : "none"} />
          <Stat label="Failed ticks" value={failed} tone={failed ? "bad" : undefined} sub="direct or search request failed" />
          <Stat label="Probe timeouts" value={timeouts} tone={timeouts ? "bad" : undefined} sub="the health check itself hung" />
          <Stat label="Fault intervals" value={windows.length} sub={windows.length ? `latest ${clock(windows[windows.length - 1].from)}` : "none in window"} />
          <Stat label="Latency stop limit" value={latencyLimit ? `${latencyLimit.toFixed(0)} ms` : "n/a"} sub={latencyLimit ? `${th!.max_latency_factor}x baseline p95` : "needs a baseline"} />
        </div>
      </Panel>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="Request latency" meta="successful requests only; gaps are failures">
          <MetricChart
            rows={rows}
            unit=" ms"
            series={[
              { key: "direct", label: "direct read", color: "#6b9bd8", value: (r) => (r.direct_ok ? r.direct_ms : null) },
              { key: "search", label: "search", color: "#b08fd8", value: (r) => (r.search_ok ? r.search_ms : null) },
            ]}
            baseline={b95 ? { value: b95, label: "baseline p95" } : null}
            limit={latencyLimit ? { value: latencyLimit, label: "stop limit" } : null}
          />
        </Panel>
        <Panel title="Request failures" meta="1 = the request failed on that tick">
          <MetricChart
            rows={rows}
            unit=""
            series={[
              { key: "direct_fail", label: "direct read failed", color: "#dc5f58", value: (r) => (r.direct_ok ? 0 : 1) },
              { key: "search_fail", label: "search failed", color: "#d6a03a", value: (r) => (r.search_ok ? 0 : 1) },
            ]}
          />
        </Panel>
        <Panel title="Twin staleness p95" meta="seconds since the stored update was sent">
          <MetricChart rows={rows} unit=" s" series={[{ key: "stale", label: "staleness p95", color: "#4fae7b", value: (r) => r.stale_p95_s }]} limit={th ? { value: th.max_stale_s, label: "stop limit" } : null} />
        </Panel>
        <Panel title="Data consistency" meta="unknown values are gaps, never zero">
          <MetricChart
            rows={rows}
            unit=""
            series={[
              { key: "lost", label: "lost acknowledged updates", color: "#dc5f58", value: (r) => r.lost_acked },
              { key: "drift", label: "search drift (twins)", color: "#d6a03a", value: (r) => r.search_drift },
              { key: "faults", label: "active faults", color: "#70777f", value: (r) => r.active_faults ?? null },
            ]}
          />
        </Panel>
      </div>

      <Panel title="Service state and resource usage" meta={live.services?.ts ? `docker stats, queried ${ago(live.services.ts, now)}` : undefined} flush>
        <ServicesTable />
      </Panel>
    </div>
  );
}
