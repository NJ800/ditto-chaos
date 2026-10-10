import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { FleetView, TwinRow } from "../api/types";
import { ProfileHealthTable, SignalsTable, SyntheticNotice, usePoll } from "../components/fleet";
import { Badge, Button, Empty, ErrorNote, KV, List, Mono, Note, PageHeader, Panel, Skeleton, Stat, StateBadge } from "../components/ui";
import { useAsync } from "../hooks";
import { DASH, ago, clock, dateTime, humanize, num } from "../lib/format";
import { useNow } from "../live/LiveContext";

const STATUSES = ["fresh", "stale", "invalid", "missing", "unknown"] as const;

function latestTelemetry(f: FleetView): number | null {
  const ts = Object.values(f.latest?.profiles ?? {})
    .map((b) => b.latest_sent_at ?? null)
    .filter((x): x is number => x !== null);
  return ts.length ? Math.max(...ts) : null;
}

export function Fleet() {
  const now = useNow();
  const poll = usePoll(5000);
  const [params, setParams] = useSearchParams();
  const profile = params.get("profile") ?? "";
  const kind = params.get("kind") ?? "";
  const status = params.get("status") ?? "";
  const q = params.get("twin") ?? "";
  const selected = params.get("open");
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    if (k === "profile") next.delete("kind");
    setParams(next, { replace: true });
  };

  const fleet = useAsync((s) => api.fleet(s), [poll]);
  const twins = useAsync((s) => api.fleetTwins({ profile, kind, status, q }, s), [poll, profile, kind, status, q]);
  const f = fleet.data;
  const kinds = useMemo(() => (f && profile ? Object.keys(f.registry[profile]?.kinds ?? {}) : []), [f, profile]);

  if (fleet.error && !f) return <ErrorNote message={fleet.error} onRetry={fleet.reload} />;
  if (!f) return <Skeleton rows={6} />;
  const counts = twins.data?.counts;
  const latest = latestTelemetry(f);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Products & digital twins"
        sub={`${f.total_twins} synthetic twins in namespace ${f.namespace}, ${Object.keys(f.profiles).length} product categor${Object.keys(f.profiles).length === 1 ? "y" : "ies"}. Configuration: ${f.source}${f.legacy_car_only ? " (car-only workflow)" : ""}.`}
        actions={<Button onClick={() => { fleet.reload(); twins.reload(); }}>Refresh</Button>}
      />
      <SyntheticNotice text={f.synthetic_notice} />
      {f.notes.map((n) => (
        <Note key={n} tone="warn" role="status">
          {n}
        </Note>
      ))}
      {fleet.error && <ErrorNote message={`Latest refresh failed: ${fleet.error}`} onRetry={fleet.reload} />}

      <Panel title="Fleet" meta={f.latest ? `newest probe tick ${ago(f.latest.ts, now)}` : "no probe data"}>
        <div className="grid grid-cols-2 gap-x-3 gap-y-4 md:grid-cols-6">
          <Stat label="Twins" value={f.total_twins} sub={`limit ${f.max_twins} (hard ${f.hard_max_twins})`} />
          <Stat label="Active (fresh)" value={counts ? counts.fresh : DASH} tone={counts ? "ok" : undefined} sub="newest update within its profile's limit" />
          <Stat label="Stale" value={counts ? counts.stale : DASH} tone={counts?.stale ? "warn" : undefined} sub="update older than 3 intervals + 1 probe" />
          <Stat label="Invalid / missing" value={counts ? `${counts.invalid} / ${counts.missing}` : DASH} tone={counts && counts.invalid + counts.missing ? "bad" : undefined} sub="schema, rule or presence check failed" />
          <Stat label="Latest telemetry" value={latest ? clock(latest) : DASH} sub={latest ? ago(latest, now) : "no twin data read"} />
          <Stat
            label="Simulator"
            value={f.simulator?.age_s !== null && f.simulator?.age_s !== undefined && f.simulator.age_s < 10 ? "running" : f.simulator ? "stalled" : "not running"}
            tone={f.simulator?.signature_match === false ? "bad" : undefined}
            sub={f.simulator?.stats ? `${f.simulator.stats.ok}/${f.simulator.stats.sent} updates acknowledged` : "no state file"}
          />
        </div>
      </Panel>

      <Panel title="Health by product category" meta="from the newest probe tick; system-level health is on Live monitoring" flush>
        <ProfileHealthTable profiles={f.latest?.profiles} selected={profile} onSelect={(k) => set("profile", profile === k ? "" : k)} />
      </Panel>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Panel title="Health indicators" meta="fleet mean (min–max) per product; 'not applicable' when the product has no such feature" flush>
            <SignalsTable profiles={f.latest?.profiles} signals={f.signals} />
          </Panel>
        </div>
        <Panel title="Simulator acknowledgements" meta={f.simulator?.written_at ? `state written ${ago(f.simulator.written_at, now)}` : undefined}>
          {f.simulator?.stats_by_profile && Object.keys(f.simulator.stats_by_profile).length ? (
            <KV
              items={Object.entries(f.simulator.stats_by_profile).map(([k, v]) => [
                f.registry[k]?.title ?? k,
                <span className="num">
                  {v.ok}/{v.sent} ok{v.failed ? <span className="text-bad"> · {v.failed} failed</span> : null}
                </span>,
              ])}
            />
          ) : (
            <Empty>No per-profile simulator statistics yet.</Empty>
          )}
        </Panel>
      </div>

      <Panel
        title="Digital twins"
        meta={twins.data ? `${twins.data.total} shown${twins.data.ts ? ` · status from ${clock(twins.data.ts)}` : ""}` : undefined}
        flush
        actions={
          <>
            <select className="field" aria-label="Product category" value={profile} onChange={(e) => set("profile", e.target.value)}>
              <option value="">all categories</option>
              {f.enabled.map((k) => (
                <option key={k} value={k}>
                  {f.registry[k]?.title ?? k}
                </option>
              ))}
            </select>
            <select className="field" aria-label="Device kind" value={kind} disabled={!profile} onChange={(e) => set("kind", e.target.value)}>
              <option value="">all device kinds</option>
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {humanize(k)}
                </option>
              ))}
            </select>
            <select className="field" aria-label="Twin status" value={status} onChange={(e) => set("status", e.target.value)}>
              <option value="">any status</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                  {counts ? ` (${counts[s]})` : ""}
                </option>
              ))}
            </select>
            <input className="field w-48" type="search" placeholder="Twin id contains" aria-label="Twin id" value={q} onChange={(e) => set("twin", e.target.value)} />
          </>
        }
      >
        {twins.data?.note && (
          <div className="p-3">
            <Note tone="warn">{twins.data.note}</Note>
          </div>
        )}
        {twins.error ? (
          <div className="p-3">
            <ErrorNote message={twins.error} onRetry={twins.reload} />
          </div>
        ) : !twins.data ? (
          <div className="p-3">
            <Skeleton rows={5} />
          </div>
        ) : !twins.data.items.length ? (
          <Empty>No twin matches these filters.</Empty>
        ) : (
          <TwinTable rows={twins.data.items} open={selected} onOpen={(id) => set("open", selected === id ? "" : id)} />
        )}
      </Panel>

      {selected && <TwinDetailPanel id={selected} onClose={() => set("open", "")} />}
    </div>
  );
}

function TwinTable({ rows, open, onOpen }: { rows: TwinRow[]; open: string | null; onOpen: (id: string) => void }) {
  return (
    <div className="max-h-[32rem] overflow-auto">
      <table className="w-full min-w-[980px] border-collapse text-xs">
        <thead className="sticky top-0 bg-surface">
          <tr>
            {["Twin", "Category", "Kind / model", "Status", "Last update", "Seq", "Operational", "Problems"].map((h) => (
              <th key={h} className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => (
            <tr key={t.id} className={`hover:bg-raised ${open === t.id ? "bg-raised" : ""}`}>
              <td className="td">
                <button type="button" className="num text-accent hover:underline" onClick={() => onOpen(t.id)}>
                  {t.id}
                </button>
              </td>
              <td className="td text-dim">{t.category}</td>
              <td className="td">
                {humanize(t.kind)}
                <div className="text-xxs text-faint">{t.model}</div>
              </td>
              <td className="td">
                <StateBadge state={t.status} />
              </td>
              <td className="td num">
                {t.age_s !== null ? `${num(t.age_s, 1)} s ago` : DASH}
                <div className="text-xxs text-faint">limit {num(t.freshness_limit_s, 0, " s")}</div>
              </td>
              <td className="td num">{t.seq ?? DASH}</td>
              <td className="td text-dim">{humanize(t.operational)}</td>
              <td className="td text-xxs">
                {t.missing_features.length > 0 && <div className="text-bad">missing: {t.missing_features.join(", ")}</div>}
                {t.violations.length > 0 ? <div className="num text-warn">{t.violations.join(", ")}</div> : !t.missing_features.length && <span className="text-faint">none</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TwinDetailPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const d = useAsync((s) => api.fleetTwin(id, s), [id]);
  return (
    <Panel
      title="Twin detail"
      meta={d.data ? `read live from Ditto at ${clock(d.data.fetched_at)}` : id}
      actions={
        <>
          <Button onClick={d.reload}>Re-read</Button>
          <Button onClick={onClose}>Close</Button>
        </>
      }
    >
      {d.error ? (
        <ErrorNote message={d.error} onRetry={d.reload} />
      ) : !d.data ? (
        <Skeleton rows={4} />
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <div className="space-y-3">
            <KV
              items={[
                ["Twin", <Mono>{d.data.twin.id}</Mono>],
                ["Category", d.data.twin.category],
                ["Kind / model", `${humanize(d.data.twin.kind)} / ${d.data.twin.model}`],
                ["Definition", <Mono>{d.data.thing.definition ?? DASH}</Mono>],
                ["Update interval", `${d.data.twin.interval_s} s (stale after ${d.data.twin.freshness_limit_s} s)`],
                ["Heartbeat feature", <Mono>{d.data.twin.heartbeat_feature}</Mono>],
                ["Operational state", humanize(d.data.validation.operational)],
                ["Checks run", d.data.validation.checks],
              ]}
            />
            <div className="flex flex-wrap gap-1.5">
              {d.data.validation.violations.length === 0 && d.data.validation.missing_features.length === 0 ? (
                <Badge tone="ok">valid</Badge>
              ) : (
                <Badge tone="bad">invalid</Badge>
              )}
              {Object.entries(d.data.signals).map(([k, v]) => (
                <Badge key={k} tone={v.applicable ? "accent" : "muted"}>
                  {humanize(k)}: {v.applicable ? num(v.value ?? null, 1) : "n/a"}
                </Badge>
              ))}
            </div>
            {(d.data.validation.violations.length > 0 || d.data.validation.missing_features.length > 0) && (
              <Note tone="warn">
                <List items={[...d.data.validation.missing_features.map((m) => `missing feature: ${m}`), ...d.data.validation.violations]} />
              </Note>
            )}
          </div>
          <div className="xl:col-span-2">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {Object.entries(d.data.thing.features ?? {}).map(([name, feat]) => (
                <div key={name} className="rounded border border-line bg-raised p-2">
                  <div className="mb-1 flex items-baseline justify-between gap-2">
                    <span className="text-xs font-medium text-ink">{name}</span>
                    <span className="num truncate text-xxs text-faint">{feat.definition?.[0] ?? ""}</span>
                  </div>
                  <KV items={Object.entries(feat.properties ?? {}).map(([k, v]) => [k, <span className="num">{fmtValue(k, v)}</span>])} />
                </div>
              ))}
            </div>
            <p className="mt-2 text-xxs text-faint">
              Attributes: {d.data.thing.attributes ? `${String(d.data.thing.attributes.manufacturer ?? "")} · serial ${String(d.data.thing.attributes.serialNumber ?? DASH)}` : DASH}.{" "}
              <Link className="text-accent hover:underline" to={`/history?twin=${encodeURIComponent(d.data.twin.id)}`}>
                Experiments that measured this twin's category
              </Link>
            </p>
          </div>
        </div>
      )}
    </Panel>
  );
}

function fmtValue(key: string, v: unknown): string {
  if (v === null || v === undefined) return DASH;
  if (typeof v === "number" && /At$/.test(key) && v > 1e9) return dateTime(v);
  if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
  return String(v);
}
