import type { ButtonHTMLAttributes, ReactNode } from "react";
import { DASH, stateTone, type Tone } from "../lib/format";

const TONE_TEXT: Record<Tone, string> = { ok: "text-ok", warn: "text-warn", bad: "text-bad", accent: "text-accent", muted: "text-dim" };
const TONE_DOT: Record<Tone, string> = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", accent: "bg-accent", muted: "bg-faint" };

const TONE_FILL: Record<Tone, string> = { ok: "border-ok/25 bg-ok/10", warn: "border-warn/30 bg-warn/10", bad: "border-bad/30 bg-bad/10", accent: "border-accent/25 bg-accent/10", muted: "border-line bg-raised" };

export function Badge({ children, tone }: { children: ReactNode; tone?: Tone }) {
  const t = tone ?? "muted";
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-xxs font-semibold uppercase tracking-wide ${TONE_FILL[t]} ${TONE_TEXT[t]}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${TONE_DOT[t]}`} aria-hidden="true" />
      {children}
    </span>
  );
}

/** `state` picks the colour; `label` overrides the text when the two differ. */
export function StateBadge({ state, label }: { state: string | null | undefined; label?: string }) {
  return <Badge tone={stateTone(state)}>{label ?? (state ? state.replace(/_/g, " ") : "unknown")}</Badge>;
}

export function Panel({ title, meta, actions, children, flush }: { title: string; meta?: ReactNode; actions?: ReactNode; children: ReactNode; flush?: boolean }) {
  return (
    <section className="overflow-hidden rounded-lg border border-line bg-surface shadow-sm">
      <header className="flex min-h-[2.75rem] flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2">
        <div className="flex items-baseline gap-2">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          {meta && <span className="text-xs text-faint">{meta}</span>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </header>
      <div className={flush ? "" : "p-4"}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className="min-w-0 border-l border-line pl-3 first:border-l-0 first:pl-0">
      <div className="truncate text-xs font-medium text-faint">{label}</div>
      <div className={`num mt-0.5 text-xl font-semibold leading-7 ${tone ? TONE_TEXT[tone] : "text-ink"}`}>{value}</div>
      {sub && <div className="mt-0.5 truncate text-xs text-faint">{sub}</div>}
    </div>
  );
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "primary" | "danger"; busy?: boolean };

export function Button({ variant = "default", busy, disabled, children, className = "", ...rest }: BtnProps) {
  const look =
    variant === "primary"
      ? "border-accent bg-accent text-white shadow-sm hover:bg-blue-700"
      : variant === "danger"
        ? "border-bad bg-bad text-white shadow-sm hover:bg-red-800"
        : "border-line bg-surface text-ink shadow-sm hover:bg-raised";
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={`inline-flex h-8 items-center justify-center whitespace-nowrap rounded-md border px-3 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${look} ${className}`}
    >
      {busy ? "Working…" : children}
    </button>
  );
}

export function Note({ tone = "muted", children, role }: { tone?: Tone; children: ReactNode; role?: "alert" | "status" }) {
  const look = { ok: "border-ok/30 bg-ok/5", warn: "border-warn/40 bg-warn/10", bad: "border-bad/40 bg-bad/5", accent: "border-accent/30 bg-accent/5", muted: "border-line bg-raised" }[tone];
  return (
    <div role={role ?? (tone === "bad" ? "alert" : undefined)} className={`rounded-md border ${look} px-3 py-2 text-xs leading-5 text-dim`}>
      {children}
    </div>
  );
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Note tone="bad">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-ink">{message}</span>
        {onRetry && <Button onClick={onRetry}>Retry</Button>}
      </div>
    </Note>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-1 py-4 text-center text-xs text-faint">{children}</p>;
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div role="status" aria-label="Loading" className="space-y-2">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="h-4 rounded bg-raised" style={{ width: `${90 - i * 15}%` }} />
      ))}
    </div>
  );
}

export function KV({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-4 gap-y-1.5 text-xs">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-faint">{k}</dt>
          <dd className="min-w-0 break-words text-ink">{v === null || v === undefined || v === "" ? DASH : v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function PageHeader({ title, sub, actions }: { title: string; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {sub && <p className="mt-1 max-w-3xl text-sm text-dim">{sub}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <span className="num text-xs text-dim">{children}</span>;
}

export function List({ items, empty = "none" }: { items: string[] | undefined; empty?: string }) {
  if (!items?.length) return <span className="text-faint">{empty}</span>;
  return (
    <ul className="list-disc space-y-0.5 pl-4">
      {items.map((s, i) => (
        <li key={i}>{s}</li>
      ))}
    </ul>
  );
}
