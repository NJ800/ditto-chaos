// Formatting helpers. A missing value always renders as an em dash, never as 0.
export const DASH = "—";

export function num(v: number | null | undefined, digits = 0, unit = ""): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return `${v.toFixed(digits)}${unit}`;
}

export function pct(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined || !Number.isFinite(v) ? DASH : `${(v * 100).toFixed(digits)}%`;
}

export function clock(tsSeconds: number | null | undefined): string {
  if (!tsSeconds) return DASH;
  return new Date(tsSeconds * 1000).toLocaleTimeString([], { hour12: false });
}

export function dateTime(iso: string | number | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return DASH;
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleString([], { hour12: false, month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function ago(tsSeconds: number | null | undefined, nowMs: number): string {
  if (!tsSeconds) return "never";
  const s = Math.max(0, Math.round(nowMs / 1000 - tsSeconds));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s ago`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return DASH;
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export type Tone = "ok" | "warn" | "bad" | "accent" | "muted";

export function stateTone(state: string | null | undefined): Tone {
  switch (state) {
    case "COMPLETED":
    case "RECOVERED":
    case "HEALTHY":
    case "completed":
    case "verified":
    case "conclusive":
    case "tolerated":
    case "ok":
      return "ok";
    case "RECOVERY_REQUIRED":
    case "FAILED":
    case "failed":
    case "timeout":
    case "rollback_failure":
    case "data_loss":
    case "not_effective":
    case "invalid":
    case "DISCONNECTED":
    case "down":
    case "critical":
    case "high":
      return "bad";
    case "ABORTED":
    case "RECOVERY_TIMEOUT":
    case "HEALTH_NOT_CONFIRMED":
    case "STOP_REQUESTED":
    case "DEGRADED":
    case "STALE":
    case "stale":
    case "kill_switch":
    case "degraded":
    case "inconclusive":
    case "fallback":
    case "medium":
    case "implemented":
      return "warn";
    case "PREFLIGHT":
    case "BASELINE":
    case "INJECTING":
    case "MONITORING":
    case "REVERTING":
    case "VERIFYING_RECOVERY":
    case "ROLLBACK_IN_PROGRESS":
    case "FAULTS_ACTIVE":
    case "running":
    case "analyzing":
    case "monitoring":
    case "accepted":
      return "accent";
    default:
      return "muted";
  }
}

export function humanize(s: string | null | undefined): string {
  return s ? s.toLowerCase().replace(/_/g, " ") : DASH;
}
