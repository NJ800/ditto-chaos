import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Findings, LearningCard, Recommendations } from "../components/experiment";
import { Badge, Button, Empty, ErrorNote, Note, PageHeader, Panel, Skeleton, Stat } from "../components/ui";
import { useAsync } from "../hooks";
import { dateTime, humanize, num } from "../lib/format";
import { useLive } from "../live/LiveContext";

const COMPONENT_LABEL: Record<string, string> = {
  novelty: "novelty",
  outcome_uncertainty: "uncertainty",
  usefulness: "usefulness",
  relevance: "relevance",
  follow_up: "re-test",
  repetition_penalty: "repeat penalty",
};

export function Insights() {
  const live = useLive();
  const learn = useAsync((s) => api.learning(s), [live.activity]);
  const recs = useAsync((s) => api.recommendations(s), [live.activity]);
  const analysis = live.orchestrator?.analysis ?? null;
  const s = learn.data?.summary;
  const acc = learn.data?.prediction_accuracy;
  const byStatus = (recs.data ?? []).reduce<Record<string, number>>((m, r) => ({ ...m, [r.status]: (m[r.status] ?? 0) + 1 }), {});

  return (
    <div className="space-y-4">
      <PageHeader
        title="Insights and learning"
        sub="What stored experiments show, how that changes what is proposed next, and which recommendations were later confirmed. All figures are counts from the knowledge store."
        actions={
          <Button
            onClick={() => {
              learn.reload();
              recs.reload();
            }}
          >
            Refresh
          </Button>
        }
      />
      {learn.error && <ErrorNote message={`Learning data unavailable: ${learn.error}`} onRetry={learn.reload} />}
      {learn.loading && !learn.data && <Skeleton rows={5} />}

      {s && acc && (
        <>
          <Panel title="Evidence base">
            <div className="grid grid-cols-2 gap-x-3 gap-y-4 md:grid-cols-5">
              <Stat label="Evidence runs" value={s.evidence_runs} sub={`of ${s.total_records} stored records`} />
              <Stat label="Not evidence" value={s.simulated_or_not_injected} sub="dry runs, not injected, inconclusive" />
              <Stat label="Single-fault coverage" value={`${s.single_fault_coverage.covered} / ${s.single_fault_coverage.possible}`} sub="fault x target pairs with evidence" />
              <Stat label="Predictions checked" value={acc.evaluated} sub={acc.evaluated ? `${acc.outcome_matched} of ${acc.evaluated} matched the outcome` : "none yet: needs a repeat run"} />
              <Stat label="Recommendations" value={recs.data?.length ?? "—"} sub={`${byStatus.verified ?? 0} verified · ${byStatus.not_effective ?? 0} not effective · ${byStatus.implemented ?? 0} awaiting re-test`} />
            </div>
            <div className="mt-3">
              <Note>
                <span className="text-ink">How learning works here: </span>
                {s.model.method}. {s.model.reason}
              </Note>
            </div>
          </Panel>

          <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
            <Panel title="Next-experiment ranking" meta="higher score = more worth running next" flush>
              {learn.data!.ranking.length ? (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] border-collapse text-xs">
                    <thead>
                      <tr>
                        {["#", "Scenario", "Score", "Runs", "Score components", "Why"].map((h) => (
                          <th key={h} className="th">
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {learn.data!.ranking.slice(0, 12).map((c) => (
                        <tr key={c.signature}>
                          <td className="td num text-faint">{c.rank}</td>
                          <td className="td num text-ink">{c.signature}</td>
                          <td className="td num">{c.blocked ? <Badge tone="bad">blocked</Badge> : num(c.score, 3)}</td>
                          <td className="td num">{c.runs}</td>
                          <td className="td num text-xxs text-dim">
                            {Object.entries(c.components)
                              .filter(([, v]) => v)
                              .map(([k, v]) => `${COMPONENT_LABEL[k] ?? k} ${v}`)
                              .join(" · ")}
                          </td>
                          <td className="td max-w-sm text-xxs text-faint">{c.rationale.join("; ")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>No candidate is eligible under the current policy.</Empty>
              )}
              <p className="border-t border-line px-3 py-2 text-xxs text-faint">
                score = {Object.entries(s.weights).map(([k, w]) => `${w} x ${COMPONENT_LABEL[k] ?? k}`).join(" + ").replace(/\+ -/g, "- ")}
              </p>
            </Panel>

            <Panel title="Repeated patterns" meta="per scenario, evidence runs only" flush>
              {s.patterns.length ? (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[560px] border-collapse text-xs">
                    <thead>
                      <tr>
                        {["Scenario", "Runs", "Outcomes", "Recurring symptoms", "Median recovery", "Last run"].map((h) => (
                          <th key={h} className="th">
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {s.patterns.map((p) => (
                        <tr key={p.signature}>
                          <td className="td num text-ink">{p.signature}</td>
                          <td className="td num">{p.runs}</td>
                          <td className="td text-dim">{Object.entries(p.outcomes).map(([k, n]) => `${humanize(k)} x${n}`).join(", ")}</td>
                          <td className="td text-dim">{Object.keys(p.recurring_symptoms).length ? Object.entries(p.recurring_symptoms).map(([k, n]) => `${humanize(k)} x${n}`).join(", ") : p.runs < 2 ? "needs 2+ runs" : "none"}</td>
                          <td className="td num">{num(p.median_recovery_s, 1, " s")}</td>
                          <td className="td num text-xxs">{dateTime(p.last_run_utc)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>No evidence runs stored yet.</Empty>
              )}
            </Panel>
          </div>

          <Panel title="Learning records" meta="one per experiment: prediction, outcome, and the resulting change in ranking">
            {learn.data!.records.length ? (
              <div className="space-y-3">
                {learn.data!.records.slice(0, 6).map((r) => (
                  <div key={r.experiment_id} className="rounded border border-line bg-raised p-3">
                    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
                      <Link className="num text-accent hover:underline" to={`/history/${r.experiment_id}`}>
                        {r.experiment_id}
                      </Link>
                      <span className="num text-ink">{r.signature}</span>
                      <span className="text-faint">{dateTime(r.created_at_utc)}</span>
                    </div>
                    <LearningCard rec={r} />
                  </div>
                ))}
              </div>
            ) : (
              <Empty>No learning records yet. One is written after each experiment run through the controller.</Empty>
            )}
          </Panel>
        </>
      )}

      <Panel title="Latest analysis" meta={analysis ? <span className="num">{analysis.experiment_id}</span> : "most recent experiment of this session"}>
        {analysis ? <Findings analysis={analysis} /> : <Empty>No experiment has been analysed in this backend session. Open one from History to see its findings.</Empty>}
      </Panel>

      <Panel title="Recommendations" meta="across all experiments">
        {recs.error ? <ErrorNote message={recs.error} onRetry={recs.reload} /> : recs.loading && !recs.data ? <Skeleton /> : <Recommendations recs={recs.data ?? []} onChanged={recs.reload} />}
      </Panel>
    </div>
  );
}
