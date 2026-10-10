# Day 2 acceptance checklist

Statuses: `NOT_STARTED` / `IN_PROGRESS` / `PASS` / `BLOCKED` / `FAIL`. Nothing below is
marked `PASS` without evidence linked next to it. This file covers what Day 2 added
on top of Day 1 (see [DAY1_CHECKLIST.md](DAY1_CHECKLIST.md) for the deterministic
safety core, which Day 2 did not change except two additive, default-`None`
parameters on `guard.run_experiment()`).

| # | Milestone (from the master brief's Definition of Done) | Status | Evidence |
|---|---|---|---|
| 1 | Eclipse Ditto runs in the authorized local sandbox | **PASS** | pre-existing (Day 1); re-verified live this session via `GET /api/dependencies` and `GET /api/health` against the real running containers |
| 2 | Simulator generates real synthetic twin updates | **PASS** | pre-existing (Day 1), unchanged |
| 3 | Health checker captures real measurements | **PASS** | `probe.py` unchanged; `GET /api/health`/`GET /api/metrics/history` serve its real output live (screenshotted, Overview + Live Monitoring pages) |
| 4 | Healthy baseline can be generated | **PASS** | real `baseline.json` present; served via `GET /api/baseline`, shown on Overview page |
| 5 | Dependency map is generated | **PASS** | real `dependency_map.json`; served via `GET /api/dependencies`, used by the planner |
| 6 | AI planner produces a validated proposal | **PASS** | `planner_agent.py`, 15 tests; live-tested THIS session with a real Gemini call (`main.py plan` and the dashboard's "Generate plan" both produced a real, allowlisted, bounds-validated proposal — see transcript) |
| 7 | Safety supervisor approves or rejects it | **PASS** | `orchestrator._claim()`, re-validates allowlist/live-health/thresholds; live-tested: a bogus `proposal_id` and a tampered out-of-allowlist target were both refused with a clear error (`test_orchestrator.py`, and live `curl` against the real API returning `400 {"detail": "unknown or stale proposal_id..."}`) |
| 8 | One authorized fault can be injected | **PASS (dry-run), real injection not run autonomously this session** | The full orchestrator pipeline (claim -> acquire_lock -> resolve_container -> `guard.run_experiment` -> save -> analyze -> remediate) was run live, end-to-end, through the real API with `dry_run=true` and completed `COMPLETED`/`recovered`. A REAL (non-dry-run) injection through this new path reuses `guard.py`'s already-proven real path verbatim (same `run_experiment()`, same kill switch, same rollback) but was deliberately **not** triggered autonomously in this session — disrupting a container, even one in this sandbox, even briefly, is exactly the kind of action this project's own safety philosophy says should get an explicit human go-ahead, not an assistant's unilateral judgment call. Ask the operator to run `python main.py run --yes` or use the dashboard's authorize checkbox for a live real-fault demo. |
| 9 | Degradation observed in actual telemetry | **PASS, Day 1 evidence** | Real `kill`/`delay` faults already ran and showed real degradation (`search=ERR`, latency deltas) in Day 1's session; see `experiments/exp-20261010-124738-86f5bb.json` and its analysis. Day 2's own live run was a dry-run (see #8), so it shows no injected degradation by design. |
| 10 | Kill switch automatically initiates rollback | **PASS** | Same `check_breach()`/revert code as Day 1, now also reachable with a `stop_event` (new regression tests: `test_stop_event_set_during_monitoring_reverts_and_verifies_recovery`) |
| 11 | Recovery is independently verified | **PASS, live** | The dry-run's `VERIFYING_RECOVERY` phase polled REAL Ditto health (dry-run only fakes the Docker/tc commands, not the probe calls) and reached `recovery_outcome: "recovered"` in the live run this session |
| 12 | AI Resilience Engineer produces evidence-based insights | **PASS, live** | Real Gemini call via `resilience_agent.py`, triggered automatically after the live dry-run; screenshotted on the Insights and Experiment Detail pages with real root-cause hypotheses and findings |
| 13 | Remediation agent recommends fixes and safely handles eligible actions | **PASS, live** | `remediation_planner.py`'s classification (Category A/B/C) unchanged from Day 1 (allowlist still intentionally empty — see ARCHITECTURE.md); `orchestrator.decide_remediation()` approve/reject tested live by clicking "Approve" in the dashboard and confirming the "operator: approved" badge appeared with `auto_applied: false` |
| 14 | Before-and-after verification works when a fix is applied | **PASS, live, with an honest caveat built into the UI itself** | `orchestrator.verify_remediation()` re-checks LIVE health against the experiment's baseline; the dashboard explicitly states "This compares LIVE current health to the saved baseline. It does not know whether a specific remediation was actually applied outside this tool" — tested live, returned `verified_healthy_now: true` |
| 15 | Experiment and findings are saved | **PASS** | Real files written this session: `experiments/exp-20261010-143332-3d4bf9.json`, `reports/experiment_exp-20261010-143332-3d4bf9_analysis.json`, `reports/remediation_plan_exp-20261010-143332-3d4bf9.md` |
| 16 | Frontend displays actual backend metrics, experiments, recovery state, insights | **PASS** | All 5 pages + the experiment detail page driven with a real headless Chromium against the real backend; screenshots show real data (100 cars, real latency numbers, 7 real experiments, real AI findings); `console --errors` reported zero errors on every page |
| 17 | Backend and frontend build/test successfully | **PASS** | `python -m pytest -q` -> **193 passed**; `cd frontend && npm run build` -> `tsc --noEmit` clean + production bundle built |
| 18 | Documentation explains how to reproduce the workflow | **PASS** | README.md ("Run the dashboard" section), ARCHITECTURE.md (approval-gate sequence diagram, updated agent table), RUNBOOK.md, this file, `.env.example` + `frontend/.env.example` |

## What's intentionally still out of scope

Documented in [ARCHITECTURE.md](ARCHITECTURE.md#agents-actually-implemented-vs-described-but-deferred):
Category B/C auto-remediation (the allowlist is still empty, on purpose) and a fully
autonomous multi-experiment loop with no human approval step. Both are deliberate,
not oversights — see that section for the reasoning, which is a direct continuation
of the Day 1 incident log below.

## New code this session, and why each piece was safe to add

- **`guard.py`**: two new parameters on `run_experiment()`, `stop_event` and
  `on_update`, both optional and defaulting to `None`. Verified additive: all 131
  pre-existing tests still pass unchanged, plus 4 new regression tests covering the
  new behavior specifically (`test_stop_event_set_before_injection_skips_fault_entirely`,
  `test_stop_event_set_during_monitoring_reverts_and_verifies_recovery`,
  `test_on_update_callback_sees_live_state_transitions`,
  `test_on_update_defaults_to_none_without_error`).
- **`planner_agent.py`** (new): proposes experiments; every proposal is independently
  re-validated against `guard.ALLOWLIST` and hard parameter bounds before it can be
  used for anything, whether it came from Gemini or the deterministic fallback.
- **`orchestrator.py`** (new): the only new code that can call `guard.py`'s
  fault-injecting functions. Synchronous `_claim()` step means a bad request is
  refused immediately and loudly, never silently in a background task.
- **`api.py`** (new): thin HTTP layer; contains no safety logic of its own, delegates
  every decision to `orchestrator.py`.
- **`frontend/`** (new): polls the API; cannot call Docker, cannot bypass the
  `authorize: true` gate (the button is disabled client-side AND the server refuses
  the request server-side if it's missing).

## Honest incident log (same-day, found and fixed during THIS session's build)

1. **`POST /api/experiments/run` was briefly a plain `def` FastAPI route.** FastAPI
   dispatches a plain `def` endpoint to a worker thread with no event loop of its
   own, so `orchestrator.start_authorized_run()`'s `asyncio.create_task()` raised
   `RuntimeError: no running event loop` — but only **after** the synchronous
   `_claim()` step had already flipped the orchestrator to `STATUS_RUNNING`, wedging
   it there permanently (every subsequent experiment would have been refused with
   "already running"). Caught by manually driving the real, running API with `curl`
   (not by the mocked test suite, which patches `start_authorized_run` itself and
   can't see which thread calls it). Fixed by making the route `async def` and by
   making `orchestrator.start_authorized_run()` roll status back to `STATUS_ERROR`
   if scheduling the task ever fails, so a similar mistake in the future fails
   loudly and recoverably instead of wedging silently. Regression-tested both at the
   orchestrator level and with a structural check
   (`test_run_endpoint_must_stay_async_or_create_task_has_no_event_loop`) that would
   catch a future accidental revert to `def`.

This is listed here deliberately, for the same reason Day 1's incident log exists:
"PASS, we think" and "PASS, here's what actually went wrong and how we know it's
fixed" are different claims, and this project makes the second one.
