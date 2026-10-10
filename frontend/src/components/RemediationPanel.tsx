import { useState } from "react";
import { api, ApiError, type RemediationPlan } from "../api";
import { Panel, ErrorState } from "./Panel";
import { StatusBadge, severityTone } from "./StatusBadge";

const CATEGORY_LABEL: Record<string, string> = {
  A: "A — already automated by guard.py",
  B: "B — bounded config change",
  C: "C — requires human approval",
};

export function RemediationPanel({
  experimentId,
  plan,
  onChanged,
}: {
  experimentId: string;
  plan: RemediationPlan;
  onChanged: () => void;
}) {
  const [busyIndex, setBusyIndex] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(index: number, action: "approve" | "reject") {
    setBusyIndex(index);
    setError(null);
    try {
      if (action === "approve") await api.approveRemediation(experimentId, index);
      else await api.rejectRemediation(experimentId, index);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to record decision.");
    } finally {
      setBusyIndex(null);
    }
  }

  async function verify() {
    setBusyIndex(-1);
    setError(null);
    try {
      await api.verifyRemediation(experimentId);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to verify.");
    } finally {
      setBusyIndex(null);
    }
  }

  return (
    <Panel
      title="Remediation plan"
      subtitle={plan.policy_note}
      actions={
        <button
          onClick={verify}
          disabled={busyIndex !== null}
          className="rounded-md bg-accent/15 px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent/25 disabled:opacity-50"
        >
          Verify current health
        </button>
      }
    >
      {error && <ErrorState message={error} />}
      <ul className="flex flex-col gap-3">
        {plan.items.map((item, i) => (
          <li key={i} className="rounded-lg border border-border bg-panel2 p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge label={item.severity} tone={severityTone(item.severity)} />
              <span className="font-medium text-slate-200">{item.finding_title}</span>
              <StatusBadge label={CATEGORY_LABEL[item.category] ?? item.category} tone="muted" />
            </div>
            <div className="mt-1.5 text-xs text-slate-300">{item.recommended_fix}</div>
            <div className="mt-1 text-[11px] text-muted">
              status: {item.status} · auto_applied: {String(item.auto_applied)}
            </div>
            {item.category === "A" ? (
              <p className="mt-2 text-xs text-muted">Not applicable — already handled by guard.py's own rollback.</p>
            ) : item.operator_decision === "approved" || item.operator_decision === "rejected" ? (
              <StatusBadge
                label={`operator: ${item.operator_decision}`}
                tone={item.operator_decision === "approved" ? "good" : "bad"}
              />
            ) : (
              <div className="mt-2 flex gap-2">
                <button
                  onClick={() => act(i, "approve")}
                  disabled={busyIndex !== null}
                  className="rounded-md bg-good/15 px-2.5 py-1 text-xs font-medium text-good hover:bg-good/25 disabled:opacity-50"
                >
                  Approve
                </button>
                <button
                  onClick={() => act(i, "reject")}
                  disabled={busyIndex !== null}
                  className="rounded-md bg-bad/15 px-2.5 py-1 text-xs font-medium text-bad hover:bg-bad/25 disabled:opacity-50"
                >
                  Reject
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>

      {plan.verification && (
        <div className="mt-4 rounded-lg border border-border bg-panel2 p-3 text-xs">
          <div className="mb-1 font-semibold uppercase tracking-wide text-muted">Before / after verification</div>
          <StatusBadge
            label={plan.verification.verified_healthy_now ? "healthy now" : "not healthy now"}
            tone={plan.verification.verified_healthy_now ? "good" : "bad"}
          />
          <p className="mt-1 text-muted">{plan.verification.note}</p>
          <p className="mt-1 text-muted">checked: {new Date(plan.verification.checked_at_utc).toLocaleString()}</p>
        </div>
      )}
    </Panel>
  );
}
