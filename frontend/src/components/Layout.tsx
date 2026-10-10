import { NavLink, Outlet } from "react-router-dom";
import { api } from "../api";
import { usePolling } from "../hooks";
import { StatusBadge, orchestratorTone } from "./StatusBadge";

const NAV = [
  { to: "/", label: "Overview" },
  { to: "/experiments", label: "AI Experiment Center" },
  { to: "/monitoring", label: "Live Monitoring" },
  { to: "/history", label: "Experiment History" },
  { to: "/insights", label: "AI Insights & Remediation" },
];

export function Layout() {
  const { data: status } = usePolling(() => api.orchestratorStatus(), 5000);

  return (
    <div className="flex min-h-screen bg-bg">
      <aside className="hidden w-60 flex-col border-r border-border bg-panel px-4 py-6 sm:flex">
        <div className="mb-8 px-2">
          <div className="text-sm font-bold tracking-tight text-slate-100">ditto-chaos</div>
          <div className="text-xs text-muted">AI Chaos Engineering Console</div>
        </div>
        <nav className="flex flex-1 flex-col gap-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive ? "bg-accent/15 text-accent" : "text-slate-300 hover:bg-white/5"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto px-2 text-xs text-muted">
          Day 2 build — real fault injection still requires explicit operator authorization.
        </div>
      </aside>

      <div className="flex min-h-screen flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-border bg-panel px-6 py-3">
          <div className="text-sm text-muted">
            Backend: <span className="font-mono text-slate-300">{import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000"}</span>
          </div>
          <StatusBadge
            label={`orchestrator: ${status?.status ?? "…"}`}
            tone={orchestratorTone(status?.status)}
            pulse={status?.status === "running" || status?.status === "analyzing"}
          />
        </header>
        <main className="flex-1 overflow-y-auto px-6 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
