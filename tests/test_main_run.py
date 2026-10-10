"""Tests for main.py's cmd_run() -- specifically its KeyboardInterrupt handling.

Context: a REAL Ctrl+C during `python main.py run` (mid-monitoring) does NOT raise a
literal KeyboardInterrupt inside guard.py's coroutine -- it raises asyncio.CancelledError
there (Task cancellation), which guard.py's own finally-block revert still runs
unconditionally. But asyncio.run()/Runner.run() on Python 3.11+ then re-raises
KeyboardInterrupt to ITS caller regardless of what the task internally did, discarding
the return value -- so cmd_run() must catch that KeyboardInterrupt itself instead of
crashing with a raw traceback (a real bug hit live: the fix lives in main.py's
cmd_run(), not guard.py, since guard.py's own revert already runs correctly via
`finally` -- confirmed by the "<<< fault reverted (verified)" line printing before
the crash in the session that found this).

This test exercises main.py's own exception handling by having
orchestrator.authorize_and_run raise KeyboardInterrupt directly -- the same shape
asyncio.run() produces for cmd_run(), regardless of the deeper asyncio/Windows
mechanics that produce it in a real terminal.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as main_mod  # noqa: E402
import orchestrator  # noqa: E402


def _args(dry_run=True, yes=True, max_experiments=1):
    return SimpleNamespace(dry_run=dry_run, yes=yes, max_experiments=max_experiments)


def test_cmd_run_handles_real_keyboard_interrupt_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "generate_plan",
                        lambda: {"proposal_id": "p1", "method": "deterministic_fallback",
                                 "fault": "kill", "target": "things", "risk": "medium",
                                 "reason": "test"})

    async def boom(proposal_id, dry_run=False):
        raise KeyboardInterrupt()
    monkeypatch.setattr(orchestrator, "authorize_and_run", boom)

    code = main_mod.cmd_run(_args())

    assert code == 130  # conventional "interrupted" exit code, not an unhandled crash
    out = capsys.readouterr().out
    assert "interrupted" in out
    assert "Traceback" not in out


def test_cmd_run_rejects_max_experiments_other_than_one(capsys):
    code = main_mod.cmd_run(_args(max_experiments=2))
    assert code == 2
    assert "exactly one" in capsys.readouterr().out


def test_cmd_run_reports_orchestrator_refusal(monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "generate_plan",
                        lambda: {"proposal_id": "p1", "method": "deterministic_fallback",
                                 "fault": "kill", "target": "things", "risk": "medium",
                                 "reason": "test"})

    async def refuse(proposal_id, dry_run=False):
        raise orchestrator.OrchestratorError("an experiment is already running")
    monkeypatch.setattr(orchestrator, "authorize_and_run", refuse)

    code = main_mod.cmd_run(_args())
    assert code == 2
    assert "REFUSED" in capsys.readouterr().out
