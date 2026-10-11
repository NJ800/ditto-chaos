import { Activity, Bot, Boxes, FlaskConical, History as HistoryIcon, LayoutDashboard, Lightbulb, Menu, Moon, Network, Sun, TriangleAlert, Workflow, type LucideIcon } from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { api, getToken, setToken } from "../api/client";
import { useAction } from "../hooks";
import { ago, humanize } from "../lib/format";
import { setTheme, useTheme } from "../lib/theme";
import { useLive, useLiveClient, useNow } from "../live/LiveContext";
import { healthLabel } from "../live/stream";
import { Sheet, SheetContent, SheetTrigger } from "./sheet";
import { Badge, Button, ErrorNote, Note, StateBadge } from "./ui";

const NAV: { to: string; label: string; icon: LucideIcon }[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard },
  { to: "/monitoring", label: "Live monitoring", icon: Activity },
  { to: "/fleet", label: "Products & twins", icon: Boxes },
  { to: "/control", label: "Experiment control", icon: FlaskConical },
  { to: "/pipeline", label: "Pipeline", icon: Workflow },
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/history", label: "History", icon: HistoryIcon },
  { to: "/insights", label: "Insights & learning", icon: Lightbulb },
  { to: "/dependencies", label: "Dependencies", icon: Network },
];

/** The one navigation list: rendered in the sidebar on wide screens and in the drawer on narrow ones. */
function PrimaryNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Primary" className="flex flex-col gap-0.5 p-2">
      {NAV.map((n) => (
        <NavLink
          key={n.to}
          to={n.to}
          end={n.to === "/"}
          onClick={onNavigate}
          className={({ isActive }) =>
            `flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors ${isActive ? "bg-accent/10 font-semibold text-accent" : "text-dim hover:bg-raised hover:text-ink"}`
          }
        >
          <n.icon className="h-4 w-4 shrink-0" aria-hidden="true" />
          {n.label}
        </NavLink>
      ))}
    </nav>
  );
}

function ThemeToggle() {
  const theme = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  const Icon = theme === "dark" ? Sun : Moon;
  return (
    <button type="button" onClick={() => setTheme(next)} aria-label={`Switch to ${next} theme`} title={`Switch to ${next} theme`} className="rounded-md border border-line bg-surface p-1.5 text-dim transition-colors hover:bg-raised hover:text-ink">
      <Icon className="h-4 w-4" aria-hidden="true" />
    </button>
  );
}

function ConnectionIndicator() {
  const live = useLive();
  const client = useLiveClient();
  const now = useNow();
  const tone = live.conn === "live" ? "ok" : live.conn === "offline" ? "bad" : "warn";
  const text = { live: "Live", connecting: "Connecting", reconnecting: "Reconnecting", offline: "Disconnected" }[live.conn];
  const retryIn = live.retryAt ? Math.max(0, Math.ceil((live.retryAt - now) / 1000)) : null;
  return (
    <div className="flex items-center gap-2" role="status" aria-live="polite">
      <Badge tone={tone}>{text}</Badge>
      <span className="hidden text-xxs text-faint md:inline">
        {live.conn === "offline" && retryIn !== null ? `retry in ${retryIn}s (attempt ${live.attempt})` : `last update ${ago(live.lastEventAt ? live.lastEventAt / 1000 : null, now)}`}
      </span>
      {live.conn === "offline" && (
        <Button onClick={() => client.reconnectNow()} className="h-6 px-2">
          Reconnect now
        </Button>
      )}
    </div>
  );
}

function TokenControl({ required }: { required: boolean }) {
  const client = useLiveClient();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(getToken());
  const has = Boolean(getToken());
  if (!required && !has && !open) return null;
  return (
    <div className="relative">
      <Button className="h-6 px-2" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {has ? "API token set" : "Set API token"}
      </Button>
      {open && (
        <form
          className="absolute right-0 top-8 z-20 w-72 rounded-md border border-line bg-surface p-3 shadow-lg"
          onSubmit={(e) => {
            e.preventDefault();
            setToken(value.trim());
            setOpen(false);
            client.reconnectNow(); // reads need the token too: reload with it
          }}
        >
          <label className="label" htmlFor="api-token">
            API token (kept for this browser tab only)
          </label>
          <input id="api-token" type="password" className="field w-full" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="off" />
          <div className="mt-2 flex justify-end gap-2">
            <Button
              onClick={() => {
                setToken("");
                setValue("");
                setOpen(false);
              }}
            >
              Clear
            </Button>
            <Button variant="primary" type="submit">
              Save
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}

function LockoutBanner() {
  const live = useLive();
  const client = useLiveClient();
  const [note, setNote] = useState("");
  const lockout = live.controller?.lockout ?? null;
  const done = async (force: boolean) => {
    const r = await api.resolveRecovery(force, note);
    client.setController(await api.controller());
    return r;
  };
  const retry = useAction(() => done(false));
  const force = useAction(() => done(true));
  if (!lockout) return null;
  return (
    <div role="alert" className="border-b-2 border-l-4 border-bad bg-bad/10 px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-bad">
            <TriangleAlert className="h-4 w-4 shrink-0" aria-hidden="true" />
            Recovery required: fault injection is disabled
          </div>
          <div className="mt-0.5 text-xs text-dim">
            {lockout.reason}
            {lockout.experiment_id ? ` (experiment ${lockout.experiment_id})` : ""}. A fault could not be confirmed as removed.
          </div>
          {lockout.items.map((it, i) => (
            <div key={i} className="mt-2 text-xs text-dim">
              <span className="text-ink">
                {it.fault ?? "target"} on {it.target ?? "unknown"}
              </span>
              : {it.error}
              {it.manual?.map((cmd) => (
                <pre key={cmd} className="num mt-1 overflow-x-auto rounded border border-line bg-bg px-2 py-1 text-xxs text-ink">
                  {cmd}
                </pre>
              ))}
            </div>
          ))}
        </div>
        <div className="flex w-full max-w-sm flex-col gap-2">
          <Button variant="primary" busy={retry.pending} onClick={() => retry.run()}>
            Retry verified recovery
          </Button>
          <input className="field" placeholder="What you fixed by hand (required to force)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Manual recovery note" />
          <Button variant="danger" busy={force.pending} disabled={!note.trim()} onClick={() => force.run()}>
            I fixed it manually: clear lockout
          </Button>
          {(retry.error || force.error) && <ErrorNote message={(retry.error || force.error)!} />}
          {retry.error === null && !retry.pending && live.controller?.lockout && (
            <span className="text-xxs text-faint">Retry re-runs the rollback and only clears the lockout if the clean state is observed.</span>
          )}
        </div>
      </div>
    </div>
  );
}

export function Layout() {
  const live = useLive();
  const now = useNow();
  const label = healthLabel(live, now);
  const orch = live.orchestrator;
  const active = orch && (orch.status === "running" || orch.status === "analyzing");
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <div className="flex min-h-screen flex-col bg-bg lg:pl-56">
      <aside className="fixed inset-y-0 left-0 z-20 hidden w-56 flex-col border-r border-line bg-surface lg:flex">
        <div className="flex h-12 shrink-0 items-center border-b border-line px-4">
          <span className="text-base font-bold tracking-tight text-ink">ditto-chaos</span>
        </div>
        <div className="flex-1 overflow-y-auto">
          <PrimaryNav />
        </div>
        <p className="border-t border-line px-4 py-3 text-xs leading-4 text-faint">Chaos experiments for a local Eclipse Ditto sandbox. All telemetry is synthetic.</p>
      </aside>
      <header className="sticky top-0 z-10 border-b border-line bg-surface">
        <div className="flex min-h-[3rem] flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-2">
          <div className="flex items-center gap-3">
            <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
              <SheetTrigger className="rounded-md border border-line bg-surface p-1.5 text-dim hover:bg-raised hover:text-ink lg:hidden" aria-label="Open navigation">
                <Menu className="h-4 w-4" aria-hidden="true" />
              </SheetTrigger>
              <SheetContent title="ditto-chaos">
                <PrimaryNav onNavigate={() => setMenuOpen(false)} />
              </SheetContent>
            </Sheet>
            <span className="text-base font-bold tracking-tight text-ink lg:hidden">ditto-chaos</span>
            <span className="flex items-center gap-2 text-xs text-faint">
              Target <StateBadge state={label} />
            </span>
            <span className="hidden items-center gap-2 text-xxs text-faint sm:flex">
              Mode <Badge tone={live.controller?.mode === "autonomous_sandbox" ? "warn" : live.controller?.mode === "observe" ? "muted" : "accent"}>{humanize(live.controller?.mode)}</Badge>
            </span>
            {active && orch?.record && (
              <NavLink to="/control" className="hidden items-center gap-2 text-xxs text-faint lg:flex">
                Experiment <StateBadge state={orch.status === "analyzing" ? "analyzing" : orch.record.state} />
              </NavLink>
            )}
          </div>
          <div className="flex items-center gap-3">
            <ConnectionIndicator />
            <TokenControl required={Boolean(live.controller?.auth_required) || (live.error ?? "").startsWith("Not authorized")} />
            <ThemeToggle />
          </div>
        </div>
      </header>
      <LockoutBanner />
      <main className="mx-auto w-full max-w-[1500px] flex-1 px-4 py-4">
        {live.phase === "loading" && <Note role="status">Loading the current state from the backend…</Note>}
        {live.phase === "error" && (
          <ErrorNote message={`${live.error ?? "The backend is unreachable."} Nothing is shown because no valid state has been loaded yet.`} />
        )}
        {live.phase === "ready" && live.conn !== "live" && (
          <div className="mb-3">
            <Note tone="warn" role="status">
              Connection to the backend is {live.conn === "offline" ? "lost" : "being re-established"}. The values below are the last ones received (
              {ago(live.lastEventAt ? live.lastEventAt / 1000 : null, now)}) and are not live. A running experiment is unaffected: its safety controller runs on the server.
            </Note>
          </div>
        )}
        {live.phase === "ready" && <Outlet />}
      </main>
    </div>
  );
}
