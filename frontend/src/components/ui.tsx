import type { ButtonHTMLAttributes, ReactNode } from "react";
import { DASH, stateTone, type Tone } from "../lib/format";

const TONE_TEXT: Record<Tone, string> = { ok: "text-ok", warn: "text-warn", bad: "text-bad", accent: "text-accent", muted: "text-dim" };
const TONE_DOT: Record<Tone, string> = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", accent: "bg-accent", muted: "bg-faint" };

export function Badge({ children, tone }: { children: ReactNode; tone?: Tone }) {
  const t = tone ?? "muted";
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded border border-line bg-raised px-1.5 py-0.5 text-xxs font-medium uppercase tracking-wide ${TONE_TEXT[t]}`}>
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
    <section className="rounded-md border border-line bg-surface">
      <header className="flex min-h-[2.5rem] flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-1.5">
        <div className="flex items-baseline gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-dim">{title}</h2>
          {meta && <span className="text-xxs text-faint">{meta}</span>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </header>
      <div className={flush ? "" : "p-3"}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className="min-w-0 border-l border-line pl-3 first:border-l-0 first:pl-0">
      <div className="truncate text-xxs uppercase tracking-wider text-faint">{label}</div>
      <div className={`num mt-0.5 text-lg leading-6 ${tone ? TONE_TEXT[tone] : "text-ink"}`}>{value}</div>
      {sub && <div className="mt-0.5 truncate text-xxs text-faint">{sub}</div>}
    </div>
  );
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "primary" | "danger"; busy?: boolean };

export function Button({ variant = "default", busy, disabled, children, className = "", ...rest }: BtnProps) {
  const look =
    variant === "primary"
      ? "border-accent/60 bg-accent/15 text-ink hover:bg-accent/25"
      : variant === "danger"
        ? "border-bad/60 bg-bad/15 text-ink hover:bg-bad/25"
        : "border-line bg-raised text-ink hover:border-faint";
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={`inline-flex h-8 items-center justify-center whitespace-nowrap rounded border px-3 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-45 ${look} ${className}`}
    >
      {busy ? "Working…" : children}
    </button>
  );
}

export function Note({ tone = "muted", children, role }: { tone?: Tone; children: ReactNode; role?: "alert" | "status" }) {
  const border = { ok: "border-ok/40", warn: "border-warn/40", bad: "border-bad/50", accent: "border-accent/40", muted: "border-line" }[tone];
  return (
    <div role={role ?? (tone === "bad" ? "alert" : undefined)} className={`rounded border ${border} bg-raised px-3 py-2 text-xs leading-5 text-dim`}>
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
        <h1 className="text-base font-semibold text-ink">{title}</h1>
        {sub && <p className="mt-0.5 max-w-3xl text-xs text-faint">{sub}</p>}
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
