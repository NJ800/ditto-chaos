import { CartesianGrid, Legend, Line, LineChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { MetricRow } from "../api/types";
import { clock } from "../lib/format";
import { CHART, SERIES_SLOTS, useTheme } from "../lib/theme";
import { Empty } from "./ui";

export interface Series {
  key: string;
  label: string;
  /** A colour slot "s1".."s8" (resolved per theme), or a literal CSS colour. */
  color: string;
  /** Returns null when the measurement does not exist (failed request, unknown value). */
  value: (r: MetricRow) => number | null;
}

/** Slot name for the n-th entity (0-based). The slot follows the entity, not its position on screen. */
export function seriesSlot(index: number): string {
  return `s${(Math.max(0, index) % SERIES_SLOTS) + 1}`;
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
  const c = CHART[useTheme()];
  if (!rows.length) return <Empty>No samples in this window. Is `python probe.py` running?</Empty>;
  const data = rows.map((r) => {
    const point: Record<string, number | null> = { ts: r.ts };
    for (const s of series) point[s.key] = s.value(r);
    return point;
  });
  const windows = faultWindows(rows);
  const stroke = (color: string) => {
    const m = /^s([1-8])$/.exec(color);
    return m ? c.series[Number(m[1]) - 1] : color;
  };
  const legend = series.length > 1;
  return (
    <div style={{ height: height + (legend ? 22 : 0) }} aria-label={`${series.map((s) => s.label).join(", ")} chart`}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={c.grid} strokeDasharray="2 4" vertical={false} />
          <XAxis dataKey="ts" type="number" domain={["dataMin", "dataMax"]} tickFormatter={(t) => clock(t as number)} tick={{ fill: c.tick, fontSize: 11 }} stroke={c.axis} minTickGap={60} />
          <YAxis tick={{ fill: c.tick, fontSize: 11 }} stroke={c.axis} width={48} unit={unit} />
          <Tooltip
            isAnimationActive={false}
            contentStyle={{ background: c.tooltipBg, border: `1px solid ${c.tooltipBorder}`, borderRadius: 6, fontSize: 12, color: c.text }}
            labelStyle={{ color: c.text, fontWeight: 600 }}
            itemStyle={{ color: c.text }}
            labelFormatter={(t) => clock(t as number)}
            formatter={(v, name) => [v === null || v === undefined ? "no data" : `${Number(v).toFixed(1)}${unit}`, name]}
          />
          {legend && <Legend verticalAlign="bottom" height={22} iconType="plainline" iconSize={14} formatter={(value) => <span style={{ color: c.tick, fontSize: 11 }}>{value}</span>} />}
          {windows.map((w) => (
            <ReferenceArea key={w.from} x1={w.from} x2={w.to} fill={c.fault} fillOpacity={0.1} stroke={c.fault} strokeOpacity={0.35} ifOverflow="extendDomain" />
          ))}
          {baseline && <ReferenceLine y={baseline.value} stroke={c.baseline} strokeDasharray="4 4" label={{ value: baseline.label, fill: c.baseline, fontSize: 11, position: "insideTopLeft" }} />}
          {limit && <ReferenceLine y={limit.value} stroke={c.limit} strokeDasharray="4 4" label={{ value: limit.label, fill: c.limit, fontSize: 11, position: "insideTopRight" }} />}
          {series.map((s) => (
            <Line key={s.key} dataKey={s.key} name={s.label} stroke={stroke(s.color)} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
