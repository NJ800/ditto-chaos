export function MetricCard({
  label,
  value,
  unit,
  hint,
  tone = "default",
}: {
  label: string;
  value: string | number | null | undefined;
  unit?: string;
  hint?: string;
  tone?: "default" | "good" | "warn" | "bad";
}) {
  const valueColor =
    tone === "good" ? "text-good" : tone === "warn" ? "text-warn" : tone === "bad" ? "text-bad" : "text-slate-100";
  const display = value === null || value === undefined ? "—" : value;
  return (
    <div className="rounded-lg border border-border bg-panel2 p-4">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-1.5 text-2xl font-semibold ${valueColor}`}>
        {display}
        {unit && value !== null && value !== undefined && <span className="ml-1 text-sm font-normal text-muted">{unit}</span>}
      </div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}
