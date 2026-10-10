# ditto-chaos

AI-assisted chaos engineering for [Eclipse Ditto](https://github.com/eclipse-ditto/ditto) (EPL-2.0) — a deterministic, safety-first fault injector with a verified kill switch, plus an optional AI layer that reads the evidence a real experiment produced and explains it.

## Why this exists

Digital-twin platforms like Ditto sit in the critical path for fleets of real devices. "It works on a healthy day" isn't resilience — resilience is knowing what breaks, how badly, and whether the system recovers, *before* a real outage teaches you the hard way. This project deliberately breaks one thing at a time in a local sandbox, watches what happens with the same signals a human on-call engineer would watch, and automatically reverts the moment things look unsafe.

**What makes this "AI-driven" — and what doesn't.** An LLM never decides what to break or when to stop; that would mean an LLM could cause (or fail to stop) a real outage, which this project treats as a hard line. Deterministic Python (`guard.py`) owns every decision that touches a running container or network namespace. The AI's job starts only *after* an experiment guard.py already ran and reverted: it reads the real before/during/after measurements and produces grounded root-cause hypotheses, severity-ranked findings, and recommended fixes — and even that output is schema-validated before being trusted, with a deterministic fallback when AI is disabled or unavailable.

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
| 193 automated tests, all mocked/offline | ✅ `python -m pytest -q` |

**Explicitly still not built**, and why: Category B/C auto-remediation (applying a fix automatically) and a fully autonomous multi-experiment loop. Every real fault injection still requires one explicit human action — a dashboard checkbox or a typed `yes`/`--yes` — no matter which agent proposed it. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full reasoning.

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
frontend/                 (Day 2) React + Vite + TS + Tailwind dashboard, polls api.py
main.py                  CLI: doctor / map / health / baseline / plan / run / analyze / report / serve
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

## Run the dashboard (Day 2)

The dashboard is an AI planner + human-approval front end for the exact same `guard.py` lifecycle above -- it does not add a second, different way to inject a fault. Three terminals:

```bash
python simulator.py            # terminal 1: fleet traffic
python probe.py                # terminal 2: live health data for the dashboard to show
python main.py serve           # terminal 3: backend API on http://127.0.0.1:8000
```

```bash
# terminal 4
cd frontend
npm install                    # first time only
cp .env.example .env           # only if the API isn't on the default host/port
npm run dev                    # http://localhost:5173
```

Open http://localhost:5173. **Overview** shows live health; **AI Experiment Center** generates a proposal and requires you to explicitly check "I am an operator and I authorize a REAL fault injection" (or leave "Preview only" checked for a zero-Docker dry run) before the run button does anything real; **Live Monitoring** charts real probe data against your baseline and configured thresholds; **Experiment History** lists every saved run; **AI Insights & Remediation** shows findings and lets you approve/reject/verify remediation items (approving records an audit-trail decision -- see "Day 1 policy" below, it does not apply anything automatically).

The CLI has the equivalent, scriptable path through the same `orchestrator.py` -- useful for a terminal-only demo or CI:

```bash
python main.py plan                          # ask the planner, read-only, prints the proposal
python main.py run --dry-run                 # preview: zero Docker/tc commands
python main.py run                           # real: asks for 'yes' confirmation, same as guard.py always has
python main.py run --yes                     # real: skips the prompt (non-interactive)
```

`main.py run` always re-validates the proposal's target against `guard.py`'s live `ALLOWLIST` and checks current health immediately before injecting -- a proposal from a minute ago might not be safe anymore. **An AI proposal can never run on its own**: the CLI still requires the same typed confirmation `guard.py` has always required, and the API still requires `authorize: true` set by an explicit dashboard click.

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

```bash
python -m pytest -q
```
193 tests, ~8 seconds, zero real Docker/HTTP/AI calls (everything is mocked). Last real run: **193 passed**. Covers `guard.py`/`probe.py` (Day 1), plus `planner_agent.py`, `orchestrator.py`, and `api.py` (Day 2, via FastAPI's `TestClient`).

Frontend: `cd frontend && npm run build` type-checks (`tsc --noEmit`) and produces a production bundle -- there's no separate frontend unit-test suite yet (Day 2 was verified by building, driving it with a real headless browser against the real backend, and screenshotting all 5 pages + the approve/verify interactions; see DAY2_CHECKLIST.md).

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
