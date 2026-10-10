# ditto-chaos

AI-assisted chaos engineering for [Eclipse Ditto](https://github.com/eclipse-ditto/ditto) (EPL-2.0) — a deterministic, safety-first fault injector with a verified kill switch, plus an optional AI layer that reads the evidence a real experiment produced and explains it.

## Why this exists

Digital-twin platforms like Ditto sit in the critical path for fleets of real devices. "It works on a healthy day" isn't resilience — resilience is knowing what breaks, how badly, and whether the system recovers, *before* a real outage teaches you the hard way. This project deliberately breaks one thing at a time in a local sandbox, watches what happens with the same signals a human on-call engineer would watch, and automatically reverts the moment things look unsafe.

**What makes this "AI-driven" — and what doesn't.** An LLM never decides when to stop, never authorizes a run, and never issues a command; that would mean an LLM could cause (or fail to stop) a real outage, which this project treats as a hard line. In v2 a model may *choose among* experiment candidates that the deterministic policy engine has already validated — nothing more (see [docs/V2.md](docs/V2.md)). Deterministic Python (`guard.py`) owns every decision that touches a running container or network namespace. The AI's job starts only *after* an experiment guard.py already ran and reverted: it reads the real before/during/after measurements and produces grounded root-cause hypotheses, severity-ranked findings, and recommended fixes — and even that output is schema-validated before being trusted, with a deterministic fallback when AI is disabled or unavailable.

## What's actually implemented

**Day 1** (deterministic safety core):

| Capability | Status |
|---|---|
| Running application (Eclipse Ditto, 9 containers via Docker Compose) | ✅ verified live |
| Dependency map (observed containers + Ditto's documented architecture) | ✅ `dependency_mapper.py` |
| Health monitoring (latency, errors, staleness, lost updates, search drift) | ✅ `probe.py` |
| Healthy baseline capture | ✅ `probe.py --baseline` / `main.py baseline` |
| One safe, controlled fault injection (`kill`, `delay`) | ✅ `guard.py`, real-tested |
| Automatic kill switch | ✅ `guard.py` (own consecutive-tick policy) + `probe.py` (standalone rolling-window policy) |
| Verified rollback (not just "the command exited 0") | ✅ container identity + running-state checks; tc-qdisc verification |
| Recovery measurement | ✅ configurable consecutive-healthy-sample policy |
| Structured, atomic experiment records | ✅ `experiments/*.json` |
| AI-grounded findings + remediation plan (recommendations, not auto-applied) | ✅ `resilience_agent.py`, `remediation_planner.py` |

**Day 2** (AI planner, orchestrator, API, dashboard — built as a layer on top, see [ARCHITECTURE.md](ARCHITECTURE.md)):

| Capability | Status |
|---|---|
| AI experiment planner (proposes fault+target+params, AI-optional) | ✅ `planner_agent.py` — allowlist/bounds-validated before it can reach anything real |
| Shared orchestrator (one allowlist, one approval gate, for both CLI and API) | ✅ `orchestrator.py` |
| Backend API for the dashboard | ✅ `api.py` (FastAPI) — `python main.py serve` |
| 5-page React dashboard (overview, experiment center, live monitoring, history, insights) | ✅ `frontend/` |
| Human-authorized real fault injection via CLI or dashboard | ✅ `main.py plan`/`run`, `POST /api/experiments/run` with `authorize: true` |
| Stop an in-progress experiment early (still reverts + verifies recovery) | ✅ `POST /api/experiments/{id}/stop`, `orchestrator.request_stop()` |
| Remediation decision tracking (approve/reject) + live before/after verification | ✅ `orchestrator.decide_remediation()`/`verify_remediation()` |
| Automated tests (see "Running tests") | ✅ `python -m pytest -q` |

**Version 2** (multi-fault scenarios, agents, autonomy, live stream -- full detail in [docs/V2.md](docs/V2.md)):

| Capability | Status |
|---|---|
| Fault registry: `kill`, `stop`, `restart`, `pause`, `delay`, `jitter`, `loss`, `bandwidth`, `cpu_throttle` | implemented; mocked tests + real-Docker tests on a throwaway container |
| `cpu_stress`, `memory_pressure`, `request_failure` | **not supported**, listed with the reason |
| Multi-fault scenarios: atomic preflight, limits, conflict checks, tested-combination allowlist | implemented; mocked tests; one two-fault scenario run live |
| Verified, independent rollback of every applied fault; recovery journal; `RECOVERY_REQUIRED` lockout | implemented; mocked tests only for the failure paths |
| Operating modes `observe` / `recommend` / `manual` / `autonomous_sandbox` | implemented; mocked tests; the autonomous loop has not been run against real Ditto |
| Seven agents with a coordinator and persisted run status | implemented; mocked tests; post-run pipeline run live once (on the deterministic fallback) |
| Knowledge store (SQLite) + deterministic learning that re-ranks candidates | implemented; mocked tests; no trained model, by design |
| Live stream (SSE) with snapshot, replay, resync; readiness/liveness | implemented; tested; observed live |
| Redesigned 7-page dashboard | implemented; 73 component tests, production build, and a real-browser smoke test over every page (no console errors) |
| Activity log (`events` in every record, incremental API, `ActivityLog` component) and scenario dry run | implemented; mocked tests; a dry run was observed live through the API |

`AUTONOMY_MODE` defaults to `recommend`, where every real fault still needs an explicit operator authorization. The autonomous loop only exists when `AUTONOMY_SANDBOX_ENABLED=true` is set in `.env`; it cannot be enabled from the dashboard. Nothing in any mode auto-applies a remediation.

## Repository structure

```
config.py              shared settings, env-overridable, loads .env automatically
simulator.py            fleet of NUM_CARS virtual cars sending telemetry to Ditto
probe.py                health telemetry + a standalone kill-switch API (current_health/check_breach)
guard.py                 the actual experiment controller: inject one fault, watch, auto-revert, measure recovery
dependency_mapper.py    deterministic dependency discovery (docker ps + Ditto's known architecture)
ai_client.py            thin Gemini API wrapper; raises AIUnavailable on anything but success
resilience_agent.py     turns one saved experiment into evidence-grounded findings (AI or deterministic fallback)
remediation_planner.py  classifies findings into safety categories; never auto-applies anything
planner_agent.py        (Day 2) proposes one experiment; allowlist/bounds-validated, AI-optional
orchestrator.py          (Day 2) the ONE place a real fault gets authorized + run; shared by main.py and api.py
api.py                   (Day 2) FastAPI backend for the dashboard -- read-only + orchestrator-gated writes
frontend/                 React + Vite + TS + Tailwind dashboard (v2: snapshot + SSE stream)
main.py                  CLI: doctor / map / health / baseline / plan / run / analyze / report / serve
                         / faults / scenario / recover
faults.py                (v2) fault registry -- one class per fault type
scenario.py              (v2) multi-fault engine, recovery journal, lockout
controller.py            (v2) operating modes + the autonomous sandbox loop
agents.py                (v2) seven agents and their coordinator
scenario_planner.py      (v2) scenario proposals from ranked, pre-validated candidates
learning.py              (v2) observe / predict / compare / rank -- deterministic
store.py                 (v2) SQLite knowledge store
events.py, monitor.py    (v2) event bus + server-side monitor behind /api/stream
docs/V2.md               (v2) modes, faults, safety guarantees, learning formula, limitations
test_guard.py, tests/    193 tests, all offline (Docker/HTTP/AI calls mocked)
experiments/              one JSON record per guard.py run (atomic writes, unique filenames)
reports/                   AI/deterministic findings + remediation plans per experiment
metrics/, state/           probe.py's/simulator.py's working files (gitignored)
```

## Prerequisites

- Docker Desktop (or an equivalent Docker daemon) — tested with Docker 29.7
- Python 3.11+
- Node.js 18+ (only needed for the dashboard — `frontend/`; tested with Node 22)
- ~2GB free for the Ditto images (gateway, things, things-search, policies, connectivity, mongodb, nginx, swagger-ui, ditto-ui)
- Optional: a [Google AI Studio](https://aistudio.google.com/apikey) API key, if you want real AI analysis/planning instead of the deterministic fallback

## Setup

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then edit .env if you want AI analysis or non-default settings
```

```bash
# Linux/macOS
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

## Start Ditto

```bash
git clone https://github.com/eclipse-ditto/ditto.git
cd ditto/deployment/docker
docker compose up -d
```
Open http://localhost:8080/ui/ (login `ditto`/`ditto`). Ditto version used: commit `952e3c270e`.

Then from this repo:
```bash
python main.py doctor
```
This checks the Docker daemon, hits Ditto's real API, checks whether `baseline.json` exists, and reports whether AI is configured — read-only, nothing it does can break anything.

## Run the fleet simulator and capture a baseline

```bash
python simulator.py                       # terminal 1: creates/drives NUM_CARS synthetic cars
python main.py baseline --duration 120    # terminal 2: records a REAL baseline.json over 2 minutes
```
A baseline captured over too few samples is statistically weak (documented in `probe.py`'s docstrings) — 120s at the default 2s probe interval gives ~60 samples, enough for a believable p95. `guard.py` also validates a baseline's freshness and health before trusting it (see below).

## See current health

```bash
python main.py health                     # needs `python probe.py` running separately
```

## Dry-run a fault (zero Docker/tc commands executed)

```bash
python guard.py --fault delay --target mongodb --ms 2000 --dry-run
python guard.py --fault kill  --target things-search --dry-run
```

## Run a real, safe experiment

```bash
python guard.py --fault kill  --target things-search --max-duration 60
python guard.py --fault delay --target mongodb --ms 2000 --ttl 60
```
This asks for interactive confirmation (skip with `--yes`), acquires a per-target lock so two experiments can't collide, and **always** attempts a verified revert — on a kill-switch breach, at `--max-duration`, on Ctrl+C, or on a crash. See the kill-switch table below for exactly what triggers a stop.

## Preflight only (no fault, no mutation)

```bash
python guard.py --fault delay --target mongodb --ms 500 --ttl 10 --preflight-only
```

## Run the dashboard

Four terminals (Windows PowerShell shown; Ditto must already be up):

```powershell
.venv\Scripts\python.exe simulator.py                    # terminal 1: fleet traffic
.venv\Scripts\python.exe main.py baseline --duration 120 # once: a fresh baseline (expires after 60 min)
.venv\Scripts\python.exe probe.py                        # terminal 2: live health data
.venv\Scripts\python.exe main.py serve                   # terminal 3: API on http://127.0.0.1:8000
```

```powershell
# terminal 4
cd frontend
npm install        # first time only
npm run dev        # http://localhost:5173
```

`main.py serve` refuses to start, with a clear message, if port 8000 is already in use (run on another port with `$env:API_PORT=8001`, and set `VITE_API_BASE_URL` for the dashboard) or if `API_HOST` is not loopback and no `API_TOKEN` is set. Network faults need `docker pull nicolaka/netshoot` once.

Open http://localhost:5173. The header always shows the target's state (`HEALTHY`, `DEGRADED`, `STALE`, `UNKNOWN` or `DISCONNECTED` -- never `HEALTHY` for data that is not fresh), the operating mode, and the stream connection.

- **Overview** -- target health against the baseline, container states with CPU/memory, the monitoring pipeline's own health, alerts.
- **Live monitoring** -- latency, failures, staleness and consistency charts with the fault interval shaded and the stop limits drawn.
- **Experiment control** -- operating mode and sandbox checks; the agent proposal; a multi-fault scenario builder with **Preview plan** (pure policy check, shows the commands that would run) and **Preflight** (read-only container check); the run button needs a passed preview *and* the authorization checkbox; lifecycle, per-fault status and **Emergency stop** for the running experiment.
- **Agents** -- each agent's state, last result, failures and persisted runs; the experiment timeline from persisted backend events.
- **History** -- searchable list (dry runs are labelled and never count as evidence); per-experiment detail with measurements, validation, findings (measured vs hypothesis), learning record, recommendations, timeline.
- **Insights & learning** -- evidence counts, the next-experiment ranking with score components, repeated patterns, learning records, recommendation tracking.
- **Dependencies** -- call edges, blast radius, risk and test coverage per service.

**Activity log.** Every experiment, real or dry run, records a step-by-step log of what the controller did and why: precondition results, the baseline and the stop limits with their actual numbers, each command, one health event and one continue/stop decision per monitoring tick, each rollback and its verification, recovery progress, and any error. It is shown under the lifecycle on **Experiment control** while a run is in progress, and on each experiment's page in **History** as a replay of the saved record. Selecting a lifecycle step filters the log to that step and explains what the step does. Command rows show the exact command, a copy button, and whether it really ran: `EXECUTED` with its exit code and any error output, or `DRY RUN — NOT EXECUTED`. The log updates incrementally (`GET /api/experiments/{id}/events?after_seq=N`, every 1.5 s, only new events) and stops polling when the run ends; it follows the newest event until you scroll up, then offers **Jump to latest**. Rollback failures and unverified recovery appear as `ERROR` rows and are never followed by a success message. **Dry run (no injection)** in the scenario builder walks the whole lifecycle with real health checks and lists every command it would run without contacting Docker; it needs no authorization and never counts as evidence. Records saved before this feature have no log and say so.

If a rollback cannot be verified, a red **Recovery required** banner appears on every page with the manual commands, and all fault injection is refused until it is resolved (see [docs/V2.md](docs/V2.md#resolving-recovery_required)).

The CLI has the equivalent, scriptable path through the same `orchestrator.py` -- useful for a terminal-only demo or CI:

```bash
python main.py plan                          # ask the planner, read-only, prints the proposal
python main.py run --dry-run                 # preview: zero Docker/tc commands
python main.py run                           # real: asks for 'yes' confirmation, same as guard.py always has
python main.py run --yes                     # real: skips the prompt (non-interactive)
```

`main.py run` always re-validates the proposal's target against `guard.py`'s live `ALLOWLIST` and checks current health immediately before injecting -- a proposal from a minute ago might not be safe anymore. **An AI proposal can never run on its own**: the CLI still requires the same typed confirmation `guard.py` has always required, and the API still requires `authorize: true` set by an explicit dashboard click.

## Ranked experiment planner

**Experiment control → Rank candidates** lists the candidate experiments in rank order. Each row shows its rank, target, fault type, risk (from the dependency map; `unknown` when there is no rating), hypothesis, the reason it is ranked there, expected impact, and the stored runs that support it. The ranking uses the dependency map, calculated service risk, experiment history, coverage gaps and expected information gain; the formula is in [docs/V2.md](docs/V2.md#how-learning-works-exactly).

- Every candidate is validated against the fault registry, the target allowlist, parameter bounds and the policy. One that fails, or whose history contains an unverified rollback, is still listed but marked "not selectable" with the reason.
- Ranking and selecting are read-only. **Select** only loads the candidate as the current proposal; running it still needs the authorization checkbox, exactly like any other proposal.
- API: `GET /api/plan/candidates`, `POST /api/plan/select {"signature": ...}`. Code entry point: `planner_agent.rank_experiments()`.

## Re-run an experiment after a fix

After you have applied a remediation yourself, open the recommendation (on the experiment's page or under **Insights & learning**) and choose **Re-run experiment**.

1. The panel shows the plan: the original scenario with the same faults and parameters. If it can no longer be run as it was (the original was a dry run, a target left the allowlist, a fault type is no longer supported, a parameter is now out of bounds), it says why and that a revised plan is required. It never substitutes a different scenario.
2. Two confirmations are required: that the change **was actually applied** (accepting a recommendation is not evidence of that), and the usual authorization for real fault injection.
3. The follow-up goes through the normal path: same locks, kill switch and verified rollback. It ignores `baseline.json` and measures a fresh baseline over 30 s, and is refused if Ditto is not healthy during that window.
4. When it finishes, the experiment page shows **Before and after the remediation**: each metric (direct and search latency, direct and search error rate, staleness, lost acknowledged updates, search drift, recovery time) with its before value, after value, change, and `improved` / `regressed` / `unchanged`. A measurement missing on either side is shown as `not measured` / `not comparable` with the reason, never as zero.
5. The verification result is `benefit observed`, `benefit not observed` or `inconclusive`, and the recommendation becomes `verified` or `not effective`. The result is labelled an **observed correlation, not proof of causation**: it compares one run before with one run after.

The link between the original experiment, the recommendation, the follow-up run, who confirmed the change and the result is stored in the knowledge store (`GET /api/followups`) and in the follow-up's own record. API: `GET /api/experiments/{id}/rerun-plan?recommendation_index=N`, `POST /api/experiments/{id}/rerun`.

An expired baseline no longer stops a **dry run**: it warns that a real run would be refused and continues with a freshly measured baseline. The Overview page marks the baseline `expired` when real runs would be refused.

## Deploy (single process, single port)

This tool controls Docker on the machine it runs on, so it is deployed **on the host that runs the Ditto sandbox**, next to the Docker daemon -- not on a separate server and not inside a container without access to that daemon.

```powershell
# 1. build the dashboard once (and again after frontend changes)
cd frontend; npm ci; npm run build; cd ..

# 2. configure .env (copy .env.example) -- at minimum, for anything but your own laptop:
#      API_TOKEN=<long random string>     required whenever API_HOST is not loopback
#      API_HOST=0.0.0.0                   only if other machines must reach it
#      AUTONOMY_MODE=recommend            keep unless you have read docs/V2.md

# 3. run the three long-lived processes (use your service manager of choice)
.venv\Scripts\python.exe simulator.py
.venv\Scripts\python.exe probe.py
.venv\Scripts\python.exe main.py serve     # API + built dashboard on http://<host>:8000/
```

- `main.py serve` serves `frontend/dist` itself, so the dashboard and the API share one origin: no second web server, no CORS setup. The dev server (`npm run dev`) is only for working on the frontend.
- With `API_TOKEN` set, **every** `/api` route requires it (reads too), except `/api/livez` and `/api/readyz`. The dashboard asks for it once per browser tab ("Set API token"). The live stream uses a single-use 30-second ticket, so the token never appears in a URL or an access log.
- Health checks for a supervisor: `GET /api/livez` (process is up) and `GET /api/readyz` (200 only when the knowledge store is reachable, the monitor loop is ticking and there is no recovery lockout).
- Stop with Ctrl+C / SIGINT: shutdown waits for a running experiment to roll back. After a hard kill, the next start replays the recovery journal before accepting requests.
- Put TLS in front (a reverse proxy) if the port is reachable from a network you do not control; this process speaks plain HTTP. Disable response buffering for `/api/stream` in that proxy.
- State lives in `experiments/`, `reports/`, `state/` (knowledge DB, journal, lockout) and `baseline.json`. Back those up; everything else is rebuildable.
- Before first use on a new machine, run the real-Docker suite and the browser smoke test from "Running tests".

## Map dependencies

```bash
python main.py map
```
Writes `dependency_map.json`/`.md` from the **actually running** containers, cross-referenced against Ditto's documented architecture and `guard.py`'s real allowlist. Risk per service (`low`/`medium`/`high`) is *calculated* from fan-in — mongodb comes out `high` in this deployment because 4 edges point at it, not because it's hardcoded as critical.

## Analyze a finished experiment

```bash
python main.py analyze experiments/<id>.json
```
Produces `reports/experiment_<id>_analysis.json` (full evidence + findings) and `reports/remediation_plan_<id>.md` (each finding classified A/B/C — see [ARCHITECTURE.md](ARCHITECTURE.md)). Uses Gemini if `AI_ENABLED=true` and a valid `GEMINI_API_KEY` are set; otherwise a deterministic, rule-based comparison of the same real numbers, clearly labeled `"analysis_method": "deterministic_fallback"`.

## Kill-switch thresholds

`guard.py` and `probe.py` each have their **own** kill-switch policy — this is a deliberate non-merge, explained in [ARCHITECTURE.md](ARCHITECTURE.md#why-two-kill-switches). Both read from the same `config.py`/env-var names so they can't silently drift to different numbers.

| Signal | guard.py's policy | probe.py's policy (standalone) |
|---|---|---|
| Direct-read outage | all of last `BREACH_WINDOW` ticks fail | n/a (rate-based, see below) |
| Search outage | all of last `BREACH_WINDOW` ticks fail | n/a |
| Overall error rate | > `MAX_ERROR_RATE` over `MAX_ERROR_RATE_WINDOW` ticks | > `MAX_ERROR_RATE` (single rolling summary) |
| Latency | p95 > `MAX_LATENCY_FACTOR` × baseline p95 (floor `MIN_LATENCY_FLOOR_MS`), direct-only | same, direct and search evaluated independently |
| Staleness | p95 > `MAX_STALE_S` over `BREACH_WINDOW` ticks | p95 > `MAX_STALE_S` (single summary) |
| Lost acknowledged update | **any** confirmed loss, immediately, bypasses the window entirely | same |
| Stale/missing monitoring data | n/a (guard.py drives its own probing in-process) | `fresh=False` or no data → treated as a breach |

Defaults live in `.env.example`; override via environment variables or `.env`.

## Experiment JSON schema

See `guard.py`'s `run_experiment()` docstring for the authoritative field list. Key fields: `schema_version`, `id`, `state` (`PRECHECK → BASELINE → INJECTING → MONITORING → REVERTING → VERIFYING_RECOVERY → COMPLETED`, or a terminal `ABORTED`/`FAILED`/`RECOVERY_TIMEOUT`), `baseline`/`baseline_source`, `timeline` (every probe tick, labeled by phase), `abort_reason`, `reverted`/`rollback_verified`/`rollback_error`, `recovery_s`/`recovery_outcome`. Two real examples are in `experiments/`.

## Running tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```
Last run (2026-10-10): **454 passed, 10 skipped**, no warnings, in ~22 s. Docker, Ditto and the AI provider are mocked; the 10 skipped tests are the real-Docker suite below. The suite covers the v1 code (`guard.py`, `probe.py`, planner, orchestrator, API) plus v2: fault registry, scenario engine (partial failure, emergency stop, cancellation, lockout, journal replay), agents and coordinator, learning and store, controller/autonomous loop, event bus and monitor, and the HTTP contract.

Real Docker (opt-in; uses one throwaway `alpine` container, never the Ditto services):
```powershell
docker pull alpine:3.20; docker pull nicolaka/netshoot
$env:DITTO_CHAOS_REAL_DOCKER = "1"; .venv\Scripts\python.exe -m pytest tests/integration -q
```
Last run: **10 passed** (all nine fault types: apply, inspect, rollback, verify).

Frontend:
```powershell
cd frontend
npm run typecheck   # tsc --noEmit
npm test            # vitest: 73 tests (stream client, reducer, pages, activity log, candidates, re-run)
npm run build
```
Last run: type check clean, **73 passed** with no warnings, build OK with no warnings.

Real browser (headless Chromium via Playwright; needs the backend running and `npx playwright install chromium` once):
```powershell
cd frontend
npm run e2e                                                   # against the dev server on :5173
$env:E2E_BASE_URL = "http://127.0.0.1:8000"; npm run e2e      # against the built dashboard served by the API
```
It opens all seven pages, previews a scenario, starts a **dry run** (never a real fault), follows its activity log to the end, opens an experiment from history, and fails on any console error or warning, uncaught exception, failed request or HTTP status >= 400. Set `E2E_API_TOKEN` when the server has `API_TOKEN`; it then also checks that nothing loads without the token. Last run: clean in all three configurations (dev server, production build, production build with token auth).

CI (`.github/workflows/ci.yml`) runs the backend suite, the real-Docker suite on the runner's own daemon, and the frontend type check, tests and build. The browser smoke test is not in CI because it needs a running Ditto stack.

## Troubleshooting

- `guard.py` refuses to inject: read the error — it's almost always "Ditto isn't healthy before the experiment" or "no usable baseline." Run `python main.py doctor` first.
- `delay` fault fails preflight with "image not available": `docker pull nicolaka/netshoot` once.
- An experiment lock seems stuck: `guard.py --force-unlock`, or delete `experiments/.locks/<target>.lock` manually if you're sure nothing is actually running.
- **Manual recovery**, if a fault was somehow left active (e.g. a second Ctrl+C during rollback): for `kill`, `docker start <container>`; for `delay`, `docker run --rm --cap-add NET_ADMIN --net container:<container> nicolaka/netshoot tc qdisc del dev eth0 root`. `guard.py` prints the exact command when this happens.

## Safety limitations (read this before a live demo)

- **The network-delay dead-man's switch only survives guard.py crashing — not the Docker daemon or host crashing.** The helper container's own `sleep ttl; tc qdisc del` only runs if Docker itself is still alive to run it.
- **guard.py refuses to touch a container with an existing `netem` qdisc** (likely a leftover from a crashed prior run) rather than guessing how to restore it — but it can't detect other, non-netem custom `tc` configurations. Don't run this against a container with real custom traffic shaping.
- **Clock skew isn't handled.** Staleness assumes one shared clock; fine on localhost, not validated across machines.
- **AI analysis can be confidently wrong about mechanism, even when the measurements are real.** In one real run during this project's own development, the AI analyst attributed a rollback failure to "the container was recreated with a different ID" — plausible-sounding, but the actual bug was a short-vs-full container-ID string comparison, not a real recreation. It had picked up a hint already present in the error message text rather than independently diagnosing anything. **Treat AI findings as a lead to verify, not a verdict.**
- **A real incident happened during this project's build**, caught and fixed the same session: a `docker kill` ran against the actual live `things-search` container (not a sandbox copy) due to a testing mistake, and the same ID-format bug above blocked its automatic revert. It was manually restarted within ~30 seconds and confirmed healthy. The bug is fixed and regression-tested (`test_resolve_container_exact_match_captures_full_id_not_short_id` in `test_guard.py`), but it's a concrete reminder that `--dry-run`/`--preflight-only` exist for a reason — use them first against anything you're not sure is sandboxed.

## Day 1 / Day 2

Day 1 scope and status: [DAY1_CHECKLIST.md](DAY1_CHECKLIST.md). Day 2 scope and status (planner, orchestrator, API, dashboard): [DAY2_CHECKLIST.md](DAY2_CHECKLIST.md). Architecture, the kill-switch duplication decision, the approval-gate sequence diagram, and what's still deferred: [ARCHITECTURE.md](ARCHITECTURE.md). Step-by-step operator instructions: [RUNBOOK.md](RUNBOOK.md).

## Team responsibility boundaries

- `probe.py` — health telemetry + its own standalone kill-switch API (`current_health`/`check_breach`)
- `guard.py` — the experiment controller: fault injection, its own kill switch, rollback, recovery. Day 2 added two optional, default-`None` parameters (`stop_event`, `on_update`) to `run_experiment()` — additive, behavior-unchanged for every existing caller, covered by new regression tests in `test_guard.py`.
- `dependency_mapper.py`, `ai_client.py`, `resilience_agent.py`, `remediation_planner.py` — the Day 1 analysis/tooling layer, written to require (and still requiring) zero changes to `probe.py`'s or `guard.py`'s existing public functions
- `planner_agent.py`, `orchestrator.py`, `api.py`, `main.py`, `frontend/` — the Day 2 planning/orchestration/dashboard layer. `orchestrator.py` is the only new code that calls into `guard.py`'s fault-injecting functions; `api.py` and `main.py`'s `plan`/`run` commands both go through it, so there is exactly one allowlist and one approval gate no matter which front door is used.

## License and attribution

This project targets Eclipse Ditto (EPL-2.0): https://github.com/eclipse-ditto/ditto. This repository's own code has no license file yet — add one before any external distribution.
