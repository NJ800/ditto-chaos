import { CartesianGrid, Line, LineChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { MetricRow } from "../api/types";
import { clock } from "../lib/format";
import { Empty } from "./ui";

export interface Series {
  key: string;
  label: string;
  color: string;
  /** Returns null when the measurement does not exist (failed request, unknown value). */
  value: (r: MetricRow) => number | null;
}

/** Contiguous [from, to] ranges where at least one fault was active. */
export function faultWindows(rows: MetricRow[]): { from: number; to: number }[] {
  const out: { from: number; to: number }[] = [];
  let start: number | null = null;
  let last = 0;
  for (const r of rows) {
    const active = r.phase === "fault" || (r.active_faults ?? 0) > 0;
    if (active) {
      if (start === null) start = r.ts;
      last = r.ts;
    } else if (start !== null) {
      out.push({ from: start, to: last });
      start = null;
    }
  }
  if (start !== null) out.push({ from: start, to: last });
  return out;
}

export function MetricChart({
  rows,
  series,
  unit,
  limit,
  baseline,
  height = 180,
}: {
  rows: MetricRow[];
  series: Series[];
  unit: string;
  limit?: { value: number; label: string } | null;
  baseline?: { value: number; label: string } | null;
  height?: number;
}) {
  if (!rows.length) return <Empty>No samples in this window. Is `python probe.py` running?</Empty>;
  const data = rows.map((r) => {
    const point: Record<string, number | null> = { ts: r.ts };
    for (const s of series) point[s.key] = s.value(r);
    return point;
  });
  const windows = faultWindows(rows);
  return (
    <div style={{ height }} aria-label={`${series.map((s) => s.label).join(", ")} chart`}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#dce3ec" strokeDasharray="2 4" vertical={false} />
          <XAxis dataKey="ts" type="number" domain={["dataMin", "dataMax"]} tickFormatter={(t) => clock(t as number)} tick={{ fill: "#64748b", fontSize: 10 }} stroke="#dce3ec" minTickGap={60} />
          <YAxis tick={{ fill: "#64748b", fontSize: 10 }} stroke="#dce3ec" width={44} unit={unit} />
          <Tooltip
            isAnimationActive={false}
            contentStyle={{ background: "#ffffff", border: "1px solid #dce3ec", borderRadius: 4, fontSize: 11 }}
            labelFormatter={(t) => clock(t as number)}
            formatter={(v, name) => [v === null || v === undefined ? "no data" : `${Number(v).toFixed(1)}${unit}`, name]}
          />
          {windows.map((w) => (
            <ReferenceArea key={w.from} x1={w.from} x2={w.to} fill="#b91c1c" fillOpacity={0.12} stroke="#b91c1c" strokeOpacity={0.35} ifOverflow="extendDomain" />
          ))}
          {baseline && <ReferenceLine y={baseline.value} stroke="#64748b" strokeDasharray="4 4" label={{ value: baseline.label, fill: "#64748b", fontSize: 10, position: "insideTopLeft" }} />}
          {limit && <ReferenceLine y={limit.value} stroke="#b45309" strokeDasharray="4 4" label={{ value: limit.label, fill: "#b45309", fontSize: 10, position: "insideTopRight" }} />}
          {series.map((s) => (
            <Line key={s.key} dataKey={s.key} name={s.label} stroke={s.color} dot={false} strokeWidth={1.5} isAnimationActive={false} connectNulls={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
