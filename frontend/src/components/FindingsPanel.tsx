import type { Analysis } from "../api";
import { Panel } from "./Panel";
import { StatusBadge, severityTone } from "./StatusBadge";

export function FindingsPanel({ analysis }: { analysis: Analysis }) {
  return (
    <div className="flex flex-col gap-4">
      <Panel
        title="Executive summary"
        subtitle={`method: ${analysis.analysis_method} — generated ${new Date(analysis.generated_at_utc).toLocaleString()}`}
        actions={<StatusBadge label={analysis.severity} tone={severityTone(analysis.severity)} />}
      >
        <p className="text-sm text-slate-200">{analysis.summary}</p>
        <div className="mt-3 flex items-center gap-2 text-xs text-muted">
          <StatusBadge label={analysis.recovery_verified ? "recovery verified" : "recovery NOT verified"} tone={analysis.recovery_verified ? "good" : "bad"} />
        </div>
      </Panel>

      {analysis.root_cause_hypotheses.length > 0 && (
        <Panel title="Root-cause hypotheses" subtitle="Ranked; each one shows the evidence for AND against it">
          <ul className="flex flex-col gap-3">
            {analysis.root_cause_hypotheses.map((h, i) => (
              <li key={i} className="rounded-lg border border-border bg-panel2 p-3 text-sm">
                <div className="font-medium text-slate-200">{h.hypothesis}</div>
                <div className="mt-1 text-xs text-muted">
                  <span className="text-good">supports:</span> {h.supporting_evidence || "—"}
                </div>
                <div className="text-xs text-muted">
                  <span className="text-bad">contradicts:</span> {h.contradicting_evidence || "—"}
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <StatusBadge label={h.confidence} tone="muted" />
                  {h.affected_services.map((s) => (
                    <StatusBadge key={s} label={s} tone="accent" />
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel title="Severity-ranked findings" subtitle={`${analysis.findings.length} finding(s)`}>
        <ul className="flex flex-col gap-3">
          {analysis.findings.map((f, i) => (
            <li key={i} className="rounded-lg border border-border bg-panel2 p-3 text-sm">
              <div className="flex items-center gap-2">
                <StatusBadge label={f.severity} tone={severityTone(f.severity)} />
                <span className="font-medium text-slate-200">{f.title}</span>
              </div>
              <div className="mt-1.5 text-xs text-muted">evidence: {f.evidence}</div>
              <div className="text-xs text-slate-300">impact: {f.impact}</div>
              <div className="mt-1 text-xs text-accent">fix: {f.recommended_fix}</div>
              <div className="mt-1 text-[11px] text-muted">confidence: {f.confidence}</div>
            </li>
          ))}
        </ul>
      </Panel>

      {analysis.recommended_next_experiment && (
        <Panel title="Suggested next experiment">
          <p className="text-sm text-slate-200">{analysis.recommended_next_experiment}</p>
        </Panel>
      )}
    </div>
  );
}
