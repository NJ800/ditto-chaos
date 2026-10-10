import { api } from "../api/client";
import { Badge, Button, Empty, ErrorNote, Note, PageHeader, Panel, Skeleton, StateBadge } from "../components/ui";
import { useAction, useAsync } from "../hooks";
import { dateTime } from "../lib/format";
import { useLive } from "../live/LiveContext";

export function Dependencies() {
  const live = useLive();
  const map = useAsync((s) => api.dependencies(s));
  const risk = useAsync((s) => api.risk(s), [live.activity]);
  const refresh = useAction(() => api.refreshDependencies(), () => {
    map.reload();
    risk.reload();
  });
  const m = map.data;
  const liveState = Object.fromEntries((live.services?.services ?? []).map((s) => [s.service, s.state]));

  return (
    <div className="space-y-4">
      <PageHeader
        title="Dependencies"
        sub="Which service calls which, and what a failure of each one can reach. Edges come from Ditto's documented architecture, checked against the containers that were running when the map was built."
        actions={
          <Button busy={refresh.pending} onClick={() => refresh.run()}>
            Rebuild from running containers
          </Button>
        }
      />
      {refresh.error && <ErrorNote message={refresh.error} />}
      {map.error && <ErrorNote message={map.error} onRetry={map.reload} />}
      {map.loading && !m && <Skeleton rows={5} />}
      {m?.docker_query_error && <Note tone="warn">The map was built without Docker's view of the containers: {m.docker_query_error}</Note>}
      {risk.data?.limitations.map((l) => (
        <Note key={l} tone="warn">
          {l}
        </Note>
      ))}

      {m && (
        <>
          <Panel title="Services" meta={`map generated ${dateTime(m.generated_at_utc)}`} flush>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] border-collapse text-xs">
                <thead>
                  <tr>
                    {["Service", "Now", "Calls", "Called by", "Blast radius if it fails", "Risk", "Evidence runs"].map((h) => (
                      <th key={h} className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {m.services.map((svc) => {
                    const r = risk.data?.targets[svc.name];
                    const calls = m.edges.filter((e) => e.from === svc.name).map((e) => e.to);
                    const calledBy = m.edges.filter((e) => e.to === svc.name).map((e) => e.from);
                    const state = liveState[svc.name];
                    return (
                      <tr key={svc.name}>
                        <td className="td">
                          <span className="font-medium text-ink">{svc.name}</span>
                          <div className="text-xxs text-faint">
                            {svc.role}
                            {svc.in_fault_allowlist ? " · fault target" : ""}
                          </div>
                        </td>
                        <td className="td">{state ? <StateBadge state={state === "running" ? "ok" : "FAILED"} label={state} /> : <Badge>unknown</Badge>}</td>
                        <td className="td text-dim">{calls.join(", ") || "—"}</td>
                        <td className="td text-dim">{calledBy.join(", ") || "—"}</td>
                        <td className="td">
                          {r ? (
                            r.blast_radius.length ? (
                              <span>
                                <span className="num text-ink">{r.blast_radius.length}</span> <span className="text-dim">{r.blast_radius.join(", ")}</span>
                              </span>
                            ) : (
                              <span className="text-faint">no service depends on it</span>
                            )
                          ) : (
                            <span className="text-faint">not a fault target</span>
                          )}
                        </td>
                        <td className="td">{r?.calculated_risk ? <StateBadge state={r.calculated_risk} /> : "—"}</td>
                        <td className="td num">{r ? (r.undertested ? <Badge tone="warn">0 untested</Badge> : r.evidence_runs) : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="border-t border-line px-3 py-2 text-xxs text-faint">
              Risk is rated from fan-in: the more services call one, the more a fault there can reach. Blast radius follows "called by" transitively. "Untested" means no conclusive experiment has targeted the service.
            </p>
          </Panel>

          <Panel title="Call edges" meta={`${m.edges.length} edges`} flush>
            {m.edges.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] border-collapse text-xs">
                  <thead>
                    <tr>
                      {["From", "To", "What flows"].map((h) => (
                        <th key={h} className="th">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {m.edges.map((e) => (
                      <tr key={`${e.from}-${e.to}`}>
                        <td className="td text-ink">{e.from}</td>
                        <td className="td text-ink">{e.to}</td>
                        <td className="td text-dim">{e.note ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty>No edges in the map.</Empty>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
