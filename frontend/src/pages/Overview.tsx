import { Link } from "react-router-dom";
import { Badge, Empty, KV, Note, PageHeader, Panel, Stat, StateBadge } from "../components/ui";
import { ago, clock, dateTime, humanize, num, pct } from "../lib/format";
import { useLive, useNow } from "../live/LiveContext";
import { healthLabel } from "../live/stream";

export function ServicesTable() {
  const { services } = useLive();
  if (!services || services.available === null) return <Empty>Container states have not been queried yet.</Empty>;
  if (!services.available || !services.services)
    return <Note tone="warn">Docker could not be queried, so container states are unknown (not "down"): {services.error}</Note>;
  if (!services.services.length) return <Empty>No containers found for this compose project. Is Ditto running?</Empty>;
  return (
    <div className="overflow-x-auto">
      {services.error && <p className="px-3 pt-2 text-xxs text-warn">Resource usage unavailable: {services.error}</p>}
      <table className="w-full min-w-[520px] border-collapse text-xs">
        <thead>
          <tr>
            {["Service", "State", "CPU", "Memory", "Fault target"].map((h) => (
              <th key={h} className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {services.services.map((s) => (
            <tr key={s.container}>
              <td className="td">
                <span className="text-ink">{s.service}</span>
                <div className="num text-xxs text-faint">{s.container}</div>
              </td>
              <td className="td">
                <StateBadge state={s.state === "running" ? "ok" : s.state === "paused" ? "ABORTED" : "FAILED"} label={s.state} />
                <div className="mt-0.5 text-xxs text-faint">{s.status_text}</div>
              </td>
              <td className="td num">{num(s.cpu_pct, 1, "%")}</td>
              <td className="td num">
                {num(s.mem_pct, 1, "%")}
                {s.mem_usage && <div className="text-xxs text-faint">{s.mem_usage}</div>}
              </td>
              <td className="td">{s.allowlisted ? <Badge tone="accent">allowlisted</Badge> : <span className="text-faint">no</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Overview() {
  const live = useLive();
  const now = useNow();
  const h = live.health;
  const label = healthLabel(live, now);
  const trusted = label === "HEALTHY" || label === "DEGRADED";
  const base = live.baseline;
  const mon = live.monitoring;
  const orch = live.orchestrator;
  const rec = orch?.record ?? null;
  const active = rec?.actions?.filter((a) => ["applying", "applied", "uncertain"].includes(a.status)).length ?? 0;
  const baseAge = base?.recorded_at ? now / 1000 - base.recorded_at : null;
  const baseExpired = baseAge !== null && live.baselineMaxAgeS !== null && baseAge > live.baselineMaxAgeS;
  const ratio = (v: number | null | undefined, b: number | null | undefined) => (v && b ? `${(v / b).toFixed(1)}x baseline (${num(b, 0)} ms)` : "no baseline");

  return (
    <div className="space-y-4">
      <PageHeader title="Overview" sub={`Eclipse Ditto at ${h?.ditto_url ?? "—"} · ${h?.num_cars ?? "—"} simulated vehicles`} />

      {!trusted && (
        <Note tone={label === "LOADING" ? "muted" : "warn"} role="status">
          {label === "STALE" && `The newest probe sample is ${ago(h?.latest_timestamp, now)}. Figures below are the last known values, not the current state. Is \`python probe.py\` still running?`}
          {label === "UNKNOWN" && "No probe data is available, so the target's health is unknown. Start `python probe.py` to measure it."}
          {label === "DISCONNECTED" && "Not connected to the backend. Figures below are the last received values."}
          {label === "LOADING" && "Loading…"}
        </Note>
      )}

      <Panel title="Target health" meta={`${h?.sample_count ?? 0} sample(s) over ${h?.window_s ?? 0}s · last sample ${clock(h?.latest_timestamp)}`} actions={<StateBadge state={label} />}>
        <div className={`grid grid-cols-2 gap-x-3 gap-y-4 md:grid-cols-4 xl:grid-cols-7 ${trusted ? "" : "opacity-60"}`}>
          <Stat label="Direct read p95" value={num(h?.direct_p95_ms, 0, " ms")} sub={ratio(h?.direct_p95_ms, base?.direct_ms?.p95)} />
          <Stat label="Search p95" value={num(h?.search_p95_ms, 0, " ms")} sub={ratio(h?.search_p95_ms, base?.search_ms?.p95)} />
          <Stat label="Direct errors" value={pct(h?.direct_error_rate)} tone={h?.direct_error_rate ? "bad" : undefined} />
          <Stat label="Search errors" value={pct(h?.search_error_rate)} tone={h?.search_error_rate ? "bad" : undefined} />
          <Stat label="Twin staleness p95" value={num(h?.stale_p95_s, 1, " s")} sub={live.thresholds ? `limit ${live.thresholds.max_stale_s}s` : undefined} />
          <Stat label="Lost acked updates" value={h?.lost_acked === null || h?.lost_acked === undefined ? "unknown" : h.lost_acked} tone={h?.lost_acked ? "bad" : undefined} sub={h?.lost_acked === null ? "simulator state not readable" : undefined} />
          <Stat label="Search drift" value={h?.search_drift === null || h?.search_drift === undefined ? "unknown" : h.search_drift} />
        </div>
      </Panel>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Panel title="Services" meta={live.services?.ts ? `queried ${ago(live.services.ts, now)}` : undefined} flush>
            <ServicesTable />
          </Panel>
        </div>
        <div className="space-y-4">
          <Panel title="Monitoring pipeline" actions={<StateBadge state={mon?.state ?? "unknown"} />}>
            <KV
              items={[
                ["Monitor loop", mon?.loop_running ? `running (tick ${num(mon.loop_age_s, 0, "s")} ago)` : "not running"],
                ["Probe data", mon?.probe_fresh ? `fresh (${num(mon.probe_age_s, 0, "s")} old)` : `stale or missing (${num(mon?.probe_age_s, 0, "s")})`],
                ["Docker", mon?.docker_available === null || mon?.docker_available === undefined ? "not queried yet" : mon.docker_available ? "reachable" : `unreachable: ${mon.docker_error}`],
                ["Last error", mon?.last_error],
              ]}
            />
            <p className="mt-2 text-xxs text-faint">This is the health of the monitoring itself, reported separately from the target's.</p>
          </Panel>

          <Panel title="Baseline" meta={base?.recorded_at ? `recorded ${dateTime(base.recorded_at)}` : undefined} actions={base ? <Badge tone={baseExpired ? "warn" : "ok"}>{baseExpired ? "expired" : "valid"}</Badge> : undefined}>
            {baseExpired && (
              <div className="mb-2">
                <Note tone="warn" role="status">
                  This baseline is older than {Math.round((live.baselineMaxAgeS ?? 0) / 60)} minutes. Experiments still run: each one measures its own fresh baseline over 30 seconds first. Re-record it with `python main.py baseline --duration 120` for a more reliable reference.
                </Note>
              </div>
            )}
            {base ? (
              <KV
                items={[
                  ["Direct p50 / p95", `${num(base.direct_ms?.p50, 0)} / ${num(base.direct_ms?.p95, 0)} ms`],
                  ["Search p50 / p95", `${num(base.search_ms?.p50, 0)} / ${num(base.search_ms?.p95, 0)} ms`],
                  ["Staleness p95", num(base.stale_p95_s, 2, " s")],
                  ["Age", base.recorded_at ? ago(base.recorded_at, now) : "unknown"],
                ]}
              />
            ) : (
              <Empty>No baseline recorded. Run `python main.py baseline --duration 120`.</Empty>
            )}
          </Panel>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="Experiment" actions={<Link className="text-xs text-accent hover:underline" to="/control">Open control</Link>}>
          {rec ? (
            <KV
              items={[
                ["Experiment", <Link className="num text-accent hover:underline" to={`/history/${rec.id}`}>{rec.id}</Link>],
                ["Scenario", <span className="num">{rec.signature ?? `${rec.fault}@${rec.target}`}</span>],
                ["State", <StateBadge state={orch?.status === "analyzing" ? "analyzing" : rec.state} />],
                ["Recovery", <StateBadge state={orch?.recovery_state} />],
                ["Active faults", active],
                ["Started", dateTime(rec.started_at_utc)],
                ["Stop reason", rec.abort_reason],
              ]}
            />
          ) : (
            <Empty>No experiment has run in this backend session.</Empty>
          )}
          {orch?.error && <div className="mt-2"><Note tone="warn">{orch.error}</Note></div>}
        </Panel>

        <Panel title="Alerts" meta="from this session's event stream">
          {live.alerts.length ? (
            <ul className="space-y-1.5 text-xs">
              {live.alerts.slice(0, 8).map((a) => (
                <li key={a.seq} className="grid grid-cols-[5rem_1fr] gap-2">
                  <span className="num text-faint">{clock(a.ts)}</span>
                  <span>
                    <StateBadge state={a.level === "critical" || a.level === "error" ? "FAILED" : a.level === "warning" ? "ABORTED" : "completed"} />
                    <span className="ml-2 text-ink">{humanize(a.kind)}</span> <span className="text-dim">{a.message}</span>
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No safety events since this page connected.</Empty>
          )}
        </Panel>
      </div>
    </div>
  );
}
