# Runbook

Step-by-step operator guide. Each step says what to expect — if you see something
else, stop and check Troubleshooting in the README before continuing.

## 1. Preflight

```bash
python main.py doctor
```
Expect: `Docker daemon ... OK`, `Ditto at http://localhost:8080 ... HTTP 200 OK`. If Docker fails, start Docker Desktop. If Ditto fails, see step 2.

## 2. Start the application (if not already running)

```bash
git clone https://github.com/eclipse-ditto/ditto.git
cd ditto/deployment/docker
docker compose up -d
```
Expect: 9 containers (`nginx`, `gateway`, `things`, `things-search`, `policies`, `connectivity`, `mongodb`, `swagger-ui`, `ditto-ui`) reach `running`. Verify with `docker ps`. Re-run `python main.py doctor` from this repo's directory.

## 3. Verify health

```bash
python main.py map
```
Expect: `dependency_map.json`/`.md` written, a per-service risk table printed, `mongodb` showing the highest fan-in/risk (calculated from real edges, not hardcoded).

## 4. Capture a baseline

```bash
python simulator.py                      # leave running in its own terminal
python main.py baseline --duration 120   # separate terminal
```
Expect: after ~2 minutes, `[probe] baseline written to baseline.json` and a JSON summary with non-null `direct_ms.p95`/`search_ms.p95`. If `direct_error_rate`/`search_error_rate` aren't 0, something's already unhealthy — fix that before experimenting; `guard.py` will refuse to trust this baseline otherwise.

## 5. Run a dry-run

```bash
python guard.py --fault delay --target mongodb --ms 2000 --ttl 60 --dry-run
```
Expect: a list of `docker`/`tc` commands printed, **no actual command executed** (verified by `test_dry_run_never_touches_subprocess` in `test_guard.py` — it monkeypatches `subprocess.run` to raise if called). Exit code 0.

## 6. Execute a safe controlled experiment

```bash
python guard.py --fault kill --target things-search --max-duration 60
```
Expect:
1. A confirmation prompt (type `yes`) — skip with `--yes` for a scripted demo.
2. `before` ticks, both healthy.
3. `>>> fault 'kill' injected into things-search`.
4. `fault` ticks — watch `search=ERR` appear.
5. After ~3 failing ticks: `!!! KILL SWITCH: search endpoint failed on all of the last 3 ticks -> reverting`.
6. `docker inspect` → `docker start` → `docker inspect` → `<<< fault reverted (verified)`.
7. `recovery` ticks until 3 consecutive healthy reads, or `recovery-timeout`.
8. `[guard] saved experiments/<id>.json`.

## 7. Observe metrics

While step 6 runs, in another terminal: `python main.py health` (if `python probe.py` is also running separately) shows the same degradation independently.

## 8. Verify kill-switch activation

Look at the printed `abort_reason` and the saved record's `state` field — should be `RECOVERY_TIMEOUT` or `COMPLETED` (both are "the kill switch worked, revert was verified" outcomes; see README's exit-code table). `state: FAILED` with a `rollback_error` means revert was **not** verified — treat that as a real incident, not a clean result; see README's "Manual recovery" instructions immediately.

## 9. Verify rollback

```bash
docker ps --filter name=things-search
```
Expect: `running`.

## 10. Inspect experiment results

```bash
python main.py report
python main.py analyze experiments/<the id you just ran>.json
```
Expect: a findings summary printed, `reports/experiment_<id>_analysis.json` and `reports/remediation_plan_<id>.md` written. Open the plan — every item should say `awaiting_human_approval` (or `already_automated_by_guard_py`); nothing should say it was applied.

## 11. Stop safely / emergency cleanup

- Ctrl+C during `guard.py`'s monitoring phase: it still attempts and verifies a revert before exiting (exit code reflects whether that succeeded).
- A **second** Ctrl+C during the revert itself: guard.py prints the exact manual command needed (differs for `kill` vs `delay`) and exits with a distinct status — run that command immediately.
- Stuck lock file (an old process died without releasing it): `python guard.py ... --force-unlock`, or delete `experiments/.locks/<target>.lock` once you've confirmed nothing is actually running against that target.
- To stop everything: `Ctrl+C` the simulator and probe terminals, then `docker compose down` from the Ditto deployment directory.

## 12. Run the same workflow through the dashboard (Day 2)

Everything below goes through `orchestrator.py` — the exact same `guard.py` lifecycle as steps 5–10, just reached via the API/UI instead of the `guard.py` CLI directly. Steps 1–4 above (preflight, Ditto up, dependency map, baseline) are still prerequisites.

1. **Start the backend + dashboard** (4 terminals total, on top of `simulator.py`/`probe.py` from step 4):
   ```bash
   python main.py serve        # terminal: API on http://127.0.0.1:8000
   cd frontend && npm install && npm run dev   # terminal: dashboard on http://localhost:5173
   ```
2. **Verify readiness**: open http://localhost:5173 — the Overview page should show `Ditto: healthy` and `connection: connected`, with real numbers (not all `—`). If it shows `unknown`/stale, `python probe.py` isn't running or hasn't produced a fresh sample yet.
3. **Generate a proposal**: go to AI Experiment Center → "Generate plan". Expect a proposal with a `fault`, `target` from `guard.py`'s allowlist, a `risk` level, and a stated `reason` — labeled `method: gemini` if AI is configured, `method: deterministic_fallback` otherwise. Either is a valid, already-validated proposal.
4. **Authorize ONE experiment**: leave "Preview only" checked for a zero-Docker dry run, or uncheck it and check "I am an operator and I authorize a REAL fault injection against `<target>`" — the run button stays disabled until you do. Click it.
5. **Observe the kill switch**: the "Experiment lifecycle" panel polls every 3s and steps through `PRECHECK → BASELINE → INJECTING → MONITORING → REVERTING → VERIFYING_RECOVERY → COMPLETED` live — this is the real `record["state"]`, not a simulated progress bar. If the kill switch trips mid-experiment, you'll see it jump straight to `REVERTING`.
6. **Stop early, if you want to**: the "Stop experiment" button (visible while running) requests a graceful early stop — `guard.py` still reverts and verifies recovery afterward, exactly like a kill-switch breach, just triggered by you instead of a threshold.
7. **Verify recovery**: once the lifecycle reaches `COMPLETED` (or `RECOVERY_TIMEOUT`/`FAILED`), the Overview page's "Last recovery result" panel and the orchestrator status badge reflect the real outcome.
8. **Inspect findings**: AI Insights & Remediation (or Experiment History → click the experiment) shows the real evidence-grounded analysis and remediation plan, generated automatically right after the run. Approve/reject items there — this records an audit-trail decision only; nothing is auto-applied (see `remediation_planner.py`'s docstring for why).
9. **Handle failed recovery**: if the state is `RECOVERY_TIMEOUT` or `FAILED`, treat it exactly as step 8 above describes for the CLI path — check `rollback_error` in the experiment detail's raw timeline/record, and follow the README's "Manual recovery" instructions if a fault may still be active. The dashboard will not let you start a second experiment while one is still `running`/`analyzing` (`already running` error), so there is never ambiguity about which experiment "Stop" refers to.
