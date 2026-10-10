type Tone = "good" | "warn" | "bad" | "muted" | "accent";

const TONE_CLASSES: Record<Tone, string> = {
  good: "bg-good/15 text-good border-good/30",
  warn: "bg-warn/15 text-warn border-warn/30",
  bad: "bg-bad/15 text-bad border-bad/30",
  muted: "bg-slate-600/15 text-muted border-slate-600/30",
  accent: "bg-accent/15 text-accent border-accent/30",
};

export function StatusBadge({ label, tone, pulse = false }: { label: string; tone: Tone; pulse?: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${TONE_CLASSES[tone]}`}
    >
      {pulse && <span className={`h-1.5 w-1.5 rounded-full animate-pulse bg-current`} />}
      {label}
    </span>
  );
}

export function healthTone(status: string | undefined): Tone {
  switch (status) {
    case "healthy":
      return "good";
    case "degraded":
      return "warn";
    case "unknown":
    default:
      return "muted";
  }
}

export function severityTone(severity: string | undefined): Tone {
  switch (severity) {
    case "critical":
      return "bad";
    case "high":
      return "bad";
    case "medium":
      return "warn";
    case "low":
      return "good";
    default:
      return "muted";
  }
}

export function orchestratorTone(status: string | undefined): Tone {
  switch (status) {
    case "running":
      return "accent";
    case "analyzing":
      return "accent";
    case "done":
      return "good";
    case "error":
      return "bad";
    case "planned":
      return "warn";
    default:
      return "muted";
  }
}
