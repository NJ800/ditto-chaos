"""Project CLI -- ties the read-only/analysis tooling together, plus `plan`/`run`,
which go through orchestrator.py -- the exact same module api.py (the dashboard's
backend) calls. There is no separate "CLI kill switch" and "API kill switch": both
front doors call guard.py's run_experiment() through orchestrator.py, so they share
one allowlist, one set of thresholds, and one approval rule.

`run` is NOT an autonomous "AI decides and executes" command, even though an AI
(or, if AI is disabled/unavailable, a deterministic heuristic) picks the proposal.
A real (non-dry-run) experiment still requires the same human confirmation guard.py
has always required -- type 'yes', or pass --yes. Exactly one experiment runs per
invocation; see `run --help` for why --max-experiments doesn't accept more than 1.

Commands:
  python main.py doctor                 -- check Docker/Ditto/AI readiness, nothing more
  python main.py map                    -- write dependency_map.json/.md
  python main.py health [--window-s N]  -- print probe.current_health()
  python main.py baseline --duration N  -- record a real baseline.json (runs probe.py)
  python main.py plan                   -- ask the planner agent for one proposal (read-only)
  python main.py run [--dry-run] [--yes] -- authorize + run ONE proposed experiment
  python main.py analyze <exp.json>     -- AI/deterministic findings + remediation plan
  python main.py report                 -- list saved experiments and their analyses
  python main.py serve                  -- run the dashboard's backend API (api.py)
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import subprocess
import sys

import ai_client
import config
import dependency_mapper
import orchestrator
import probe
import remediation_planner
import resilience_agent


def cmd_doctor(args) -> int:
    print("[doctor] Docker daemon ...", end=" ")
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=10)
        print("OK" if r.returncode == 0 else f"FAILED ({r.stderr.strip()})")
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"FAILED ({e})")

    print(f"[doctor] Ditto at {config.DITTO_URL} ...", end=" ")
    try:
        import httpx
        r = httpx.get(f"{config.DITTO_URL}/api/2/things", params={"ids": "org.hackathon:car-001"},
                      auth=(config.DITTO_USER, config.DITTO_PASS), timeout=5)
        print(f"HTTP {r.status_code}" + (" OK" if r.status_code == 200 else " (unexpected)"))
    except Exception as e:  # noqa: BLE001 -- doctor must report, never crash, on any failure
        print(f"FAILED ({e})")

    print("[doctor] baseline.json ...", end=" ")
    print("present" if os.path.isfile(config.BASELINE_FILE) else "MISSING -- run `python main.py baseline`")

    print("[doctor] AI (Gemini) ...", end=" ")
    if not config.AI_ENABLED:
        print("disabled (AI_ENABLED=false) -- analysis will use the deterministic fallback")
    elif not config.GEMINI_API_KEY:
        print("AI_ENABLED=true but no GEMINI_API_KEY set -- will fall back to deterministic analysis")
    else:
        print(f"configured (model={config.GEMINI_MODEL}) -- note: this does not verify the key works")
    return 0


def cmd_map(args) -> int:
    m = dependency_mapper.build_dependency_map()
    jp, mp = dependency_mapper.save(m)
    print(f"[map] wrote {jp}")
    print(f"[map] wrote {mp}")
    if m["docker_query_error"]:
        print(f"[map] WARNING: {m['docker_query_error']}")
        return 1
    for r in m["fault_target_recommendations"]:
        print(f"  {r['service']:16s} risk={r['calculated_risk']:6s} fan_in={r['fan_in']} "
              f"allowlisted={r['allowlisted_for_faults']}")
    return 0


def cmd_health(args) -> int:
    h = probe.current_health(window_s=args.window_s)
    print(json.dumps(h, indent=2))
    return 0 if h["status"] != "unknown" else 1


def cmd_baseline(args) -> int:
    print(f"[baseline] recording for {args.duration}s (needs `python probe.py`'s own loop logic -- "
          f"reusing it directly)...")
    asyncio.run(probe.run(args.duration, write_baseline=True))
    return 0 if os.path.isfile(config.BASELINE_FILE) else 1


def cmd_plan(args) -> int:
    proposal = orchestrator.generate_plan()
    print(f"[plan] method={proposal['method']}")
    print(f"[plan] fault={proposal['fault']} target={proposal['target']} params={proposal['params']}")
    print(f"[plan] risk={proposal['risk']}")
    print(f"[plan] hypothesis: {proposal['hypothesis']}")
    print(f"[plan] expected_impact: {proposal['expected_impact']}")
    print(f"[plan] reason: {proposal['reason']}")
    print(f"[plan] success_criteria: {proposal['success_criteria']}")
    print(f"[plan] max_duration_s={proposal['max_duration_s']} recovery_timeout_s={proposal['recovery_timeout_s']}")
    print(f"[plan] proposal_id={proposal['proposal_id']} (pass to `run` within this same process -- "
          f"the CLI and API each hold their own in-memory proposal, so this id is only valid if "
          f"`run` is invoked in the SAME `main.py` process/command)")
    return 0


def _confirm_real_run(proposal: dict, args) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise orchestrator.OrchestratorError(
            "refusing to run a REAL experiment in a non-interactive session without --yes")
    print(f"[run] About to authorize a REAL '{proposal['fault']}' fault against "
          f"'{proposal['target']}' (params={proposal['params']}). This can disrupt anything "
          f"using that service while the experiment runs.", flush=True)
    if input("[run] Type 'yes' to continue: ").strip().lower() != "yes":
        raise orchestrator.OrchestratorError("aborted by user (confirmation not given)")


def cmd_run(args) -> int:
    if args.max_experiments != 1:
        print("[run] Day 2 only supports exactly one authorized experiment per `main.py run` "
              "invocation, by design -- re-run the command for another one. Pass "
              "--max-experiments 1 (the default) to acknowledge this.")
        return 2

    proposal = orchestrator.generate_plan()
    print(f"[run] proposal: {proposal['method']} fault={proposal['fault']} target={proposal['target']} "
          f"risk={proposal['risk']}")
    print(f"[run] reason: {proposal['reason']}")

    try:
        if not args.dry_run:
            _confirm_real_run(proposal, args)
        result = asyncio.run(orchestrator.authorize_and_run(proposal["proposal_id"], dry_run=args.dry_run))
    except orchestrator.OrchestratorError as e:
        print(f"[run] REFUSED: {e}")
        return 2
    except KeyboardInterrupt:
        # A real Ctrl+C here does NOT mean "nothing happened": guard.py's own
        # finally-block revert still runs (Python runs `finally` no matter what
        # exception is propagating), so if a fault was already injected, it has
        # almost certainly already been reverted by the time you see this message --
        # watch for guard.py's own "<<< fault reverted (verified)" line above. What
        # this interrupt DOES mean: asyncio.run() re-raises KeyboardInterrupt to its
        # caller unconditionally once a real Ctrl+C lands, discarding whatever
        # run_experiment() would otherwise have returned -- so the experiment record
        # was NOT saved to experiments/, and no AI analysis ran. Check
        # `docker ps --filter name=<target>` yourself to be sure.
        print("\n[run] interrupted -- if a fault was already injected, guard.py's own "
              "revert logic still ran (see the output above). The experiment record "
              "was NOT saved this time; verify the target container's state manually "
              "(`docker ps`) before trusting it.")
        return 130

    record = result["record"]
    print(f"[run] final state: {record['state']} (aborted={record['aborted']}, "
          f"recovery_outcome={record['recovery_outcome']})")
    print(f"[run] analysis: method={result['analysis']['analysis_method']} "
          f"severity={result['analysis']['severity']}")
    print(f"[run] saved {result['experiment_path']}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    import api
    print(f"[serve] starting API on http://{config.API_HOST}:{config.API_PORT} "
          f"(frontend origins allowed: {config.FRONTEND_ORIGINS})")
    uvicorn.run(api.app, host=config.API_HOST, port=config.API_PORT)
    return 0


def cmd_analyze(args) -> int:
    with open(args.experiment_file, encoding="utf-8") as f:
        record = json.load(f)
    dep_map = None
    if os.path.isfile("dependency_map.json"):
        with open("dependency_map.json", encoding="utf-8") as f:
            dep_map = json.load(f)
    analysis = resilience_agent.analyze_experiment(record, dep_map)
    analysis_path = resilience_agent.save_report(analysis)
    plan = remediation_planner.build_plan(analysis)
    plan_path = remediation_planner.save_plan(plan)

    print(f"[analyze] method={analysis['analysis_method']} severity={analysis['severity']}")
    print(f"[analyze] {analysis['summary']}")
    for f in analysis["findings"]:
        print(f"  - [{f['severity']}] {f['title']} ({f['confidence']})")
    print(f"[analyze] saved {analysis_path}")
    print(f"[analyze] saved {plan_path}")
    return 0


def cmd_report(args) -> int:
    files = sorted(glob.glob(os.path.join(config.EXPERIMENTS_DIR, "*.json")))
    if not files:
        print(f"[report] no saved experiments in {config.EXPERIMENTS_DIR}/")
        return 1
    for path in files:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        analysis_path = os.path.join(config.REPORTS_DIR, f"experiment_{rec.get('id')}_analysis.json")
        analyzed = "analyzed" if os.path.isfile(analysis_path) else "not yet analyzed"
        print(f"  {rec.get('id')}: fault={rec.get('fault')} target={rec.get('target')} "
              f"state={rec.get('state')} ({analyzed})")
    latest = os.path.join(config.REPORTS_DIR, "latest_findings.md")
    if os.path.isfile(latest):
        print(f"\n[report] see {latest} for findings detail")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="main.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check Docker/Ditto/AI readiness (read-only)")
    sub.add_parser("map", help="write dependency_map.json/.md from the real running containers")

    p = sub.add_parser("health", help="print probe.current_health()")
    p.add_argument("--window-s", type=float, default=10)

    p = sub.add_parser("baseline", help="record a real baseline.json")
    p.add_argument("--duration", type=float, default=120)

    p = sub.add_parser("analyze", help="AI/deterministic findings + remediation plan for one experiment")
    p.add_argument("experiment_file", help="path to experiments/<id>.json")

    sub.add_parser("report", help="list saved experiments and whether they've been analyzed")

    sub.add_parser("plan", help="ask the planner agent for one proposed experiment (read-only)")

    p = sub.add_parser("run", help="authorize + run ONE proposed experiment through orchestrator.py")
    p.add_argument("--dry-run", action="store_true", help="print commands only, inject nothing real")
    p.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    p.add_argument("--max-experiments", type=int, default=1,
                   help="must be 1 -- see this command's help text for why")

    sub.add_parser("serve", help="run the dashboard's backend API (api.py) with uvicorn")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    handlers = {"doctor": cmd_doctor, "map": cmd_map, "health": cmd_health,
               "baseline": cmd_baseline, "plan": cmd_plan, "run": cmd_run,
               "analyze": cmd_analyze, "report": cmd_report, "serve": cmd_serve}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
