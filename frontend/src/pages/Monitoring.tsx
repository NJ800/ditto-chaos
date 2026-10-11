import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { MetricRow } from "../api/types";
import { MetricChart, faultWindows, seriesSlot } from "../components/MetricChart";
import { Empty, Note, PageHeader, Panel, Stat, StateBadge } from "../components/ui";
import { ago, clock } from "../lib/format";
import { useLive, useNow } from "../live/LiveContext";
import { healthLabel } from "../live/stream";
import { ServicesTable } from "./Overview";

/** A product category keeps its colour slot whatever the category filter shows. */

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
  const [profile, setProfile] = useState("");
  const [service, setService] = useState("");
  const [twin, setTwin] = useState("");
  const profileTitles = useMemo(() => {
    const out: Record<string, string> = {};
    for (const r of rows) for (const [k, b] of Object.entries(r.profiles ?? {})) out[k] = b.title;
    return out;
  }, [rows]);
  const profileKeys = Object.keys(profileTitles);
  const slotOf = (k: string) => seriesSlot(profileKeys.indexOf(k));
  const shown = profile && profileKeys.includes(profile) ? [profile] : profileKeys;
  const block = (r: MetricRow, k: string) => {
    const b = r.profiles?.[k];
    return b && b.measured ? b : null;
  };
  const limitFor = profile ? last?.profiles?.[profile]?.freshness_limit_s ?? null : null;

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
              { key: "direct", label: "direct read", color: "s1", value: (r) => (r.direct_ok ? r.direct_ms : null) },
              { key: "search", label: "search", color: "s2", value: (r) => (r.search_ok ? r.search_ms : null) },
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
              { key: "direct_fail", label: "direct read failed", color: "s1", value: (r) => (r.direct_ok ? 0 : 1) },
              { key: "search_fail", label: "search failed", color: "s2", value: (r) => (r.search_ok ? 0 : 1) },
            ]}
          />
        </Panel>
        <Panel title="Twin staleness p95" meta="seconds since the stored update was sent">
          <MetricChart rows={rows} unit=" s" series={[{ key: "stale", label: "staleness p95", color: "s1", value: (r) => r.stale_p95_s }]} limit={th ? { value: th.max_stale_s, label: "stop limit" } : null} />
        </Panel>
        <Panel title="Data consistency" meta="unknown values are gaps, never zero">
          <MetricChart
            rows={rows}
            unit=""
            series={[
              { key: "lost", label: "lost acknowledged updates", color: "s1", value: (r) => r.lost_acked },
              { key: "drift", label: "search drift (twins)", color: "s2", value: (r) => r.search_drift },
              { key: "faults", label: "active faults", color: "s3", value: (r) => r.active_faults ?? null },
            ]}
          />
        </Panel>
      </div>

      <Panel
        title="Product data quality"
        meta="per product category; a data-quality view only -- the kill switch uses the system metrics above"
        actions={
          <>
            <select className="field" aria-label="Product category filter" value={profile} onChange={(e) => setProfile(e.target.value)}>
              <option value="">all categories</option>
              {profileKeys.map((k) => (
                <option key={k} value={k}>
                  {profileTitles[k]}
                </option>
              ))}
            </select>
            <form
              className="flex items-center gap-1.5"
              onSubmit={(e) => {
                e.preventDefault();
              }}
            >
              <input className="field w-44" type="search" placeholder="Twin id" aria-label="Find a twin" value={twin} onChange={(e) => setTwin(e.target.value)} />
              <Link className={`text-xs ${twin.trim() ? "text-accent hover:underline" : "pointer-events-none text-faint"}`} to={`/fleet?twin=${encodeURIComponent(twin.trim())}`}>
                Open twin
              </Link>
            </form>
          </>
        }
      >
        {!profileKeys.length ? (
          <Empty>No per-profile data in this window (rows from the car-only probe, or the probe is not running).</Empty>
        ) : (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div>
              <h3 className="mb-1 text-xs font-medium text-dim">Fresh twins (%)</h3>
              <MetricChart
                rows={rows}
                unit="%"
                series={shown.map((k) => ({ key: `fresh-${k}`, label: profileTitles[k], color: slotOf(k), value: (r: MetricRow) => { const b = block(r, k); return b && b.fresh_ratio !== null ? b.fresh_ratio * 100 : null; } }))}
              />
            </div>
            <div>
              <h3 className="mb-1 text-xs font-medium text-dim">Staleness p95 (s)</h3>
              <MetricChart
                rows={rows}
                unit=" s"
                series={shown.map((k) => ({ key: `stale-${k}`, label: profileTitles[k], color: slotOf(k), value: (r: MetricRow) => block(r, k)?.stale_p95_s ?? null }))}
                limit={limitFor !== null ? { value: limitFor, label: "freshness limit" } : null}
              />
            </div>
            <div>
              <h3 className="mb-1 text-xs font-medium text-dim">Valid twins (%)</h3>
              <MetricChart
                rows={rows}
                unit="%"
                series={shown.map((k) => ({ key: `valid-${k}`, label: profileTitles[k], color: slotOf(k), value: (r: MetricRow) => { const b = block(r, k); return b && b.valid_ratio !== null ? b.valid_ratio * 100 : null; } }))}
              />
            </div>
            <div>
              <h3 className="mb-1 text-xs font-medium text-dim">Lost updates + search drift (twins)</h3>
              <MetricChart
                rows={rows}
                unit=""
                series={shown.map((k) => ({ key: `loss-${k}`, label: profileTitles[k], color: slotOf(k), value: (r: MetricRow) => { const b = block(r, k); return b && b.lost_acked !== null && b.search_drift !== null ? b.lost_acked + b.search_drift : null; } }))}
              />
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="Service state and resource usage"
        meta={live.services?.ts ? `docker stats, queried ${ago(live.services.ts, now)}` : undefined}
        flush
        actions={
          <select className="field" aria-label="Service filter" value={service} onChange={(e) => setService(e.target.value)}>
            <option value="">all services</option>
            {(live.services?.services ?? []).map((s) => (
              <option key={s.container} value={s.service}>
                {s.service}
              </option>
            ))}
          </select>
        }
      >
        <ServicesTable service={service} />
      </Panel>
    </div>
  );
}
