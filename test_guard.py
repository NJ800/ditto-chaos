"""Tests for guard.py: kill-switch logic, fault drivers, lifecycle, locking, persistence.

Run:  python -m pytest -q

Nothing here ever shells out to a real `docker` or hits a real Ditto instance --
Docker subprocesses are mocked via guard.run_cmd, and health data via probe.probe_once.
No destructive/real fault injection runs as part of this suite.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import config
import guard
import probe

# ---------------------------------------------------------------- shared fixtures/helpers
OK = {"ts": 0, "direct_ok": True, "search_ok": True, "direct_ms": 10.0, "search_ms": 10.0,
      "stale_p50_s": 1.0, "stale_p95_s": 1.0, "stale_max_s": 1.0, "lost_acked": 0, "search_drift": 0}
BASE = {"direct_ms": {"p95": 20}}
TH = guard.Thresholds.from_env()


def row(**kw):
    return {**OK, **kw}


def make_probe(rows, default=None):
    """Fake probe.probe_once: returns rows in order, then repeats `default` forever."""
    it = iter(rows)
    default = default if default is not None else OK

    async def fake(client, ids):
        try:
            return dict(next(it))
        except StopIteration:
            return dict(default)
    return fake


class FakeFault:
    name = "kill"

    def __init__(self, dry_run=False, fail_inject=False, fail_revert=False):
        self.params = {"fake": True}
        self.dry_run = dry_run
        self.fail_inject = fail_inject
        self.fail_revert = fail_revert
        self.injected = False
        self.reverted = False
        self.preflight_called = False

    async def preflight(self):
        self.preflight_called = True

    async def inject(self):
        self.injected = True
        if self.fail_inject:
            raise RuntimeError("inject boom")

    async def revert(self):
        self.reverted = True
        if self.fail_revert:
            raise guard.RollbackError("revert boom")


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """Keep every test fast: tiny probe interval, independent of the real Ditto timeout."""
    monkeypatch.setattr(config, "PROBE_INTERVAL_S", 0.01)


# ================================================================== check_breach / is_healthy
def test_check_breach_healthy_no_breach():
    assert guard.check_breach([row()] * 3, BASE, TH) == (False, "")


def test_check_breach_empty_window_no_crash():
    assert guard.check_breach([], BASE, TH) == (False, "")


def test_check_breach_needs_full_window_for_consecutive_signals():
    assert guard.check_breach([row(direct_ok=False)] * 2, BASE, TH)[0] is False


def test_check_breach_single_blip_does_not_trigger():
    window = [row(), row(direct_ok=False), row()]
    assert guard.check_breach(window, BASE, TH)[0] is False


def test_check_breach_consecutive_direct_outage():
    breached, reason = guard.check_breach([row(direct_ok=False)] * 3, BASE, TH)
    assert breached and "direct-read" in reason


def test_check_breach_consecutive_search_outage():
    breached, reason = guard.check_breach([row(search_ok=False)] * 3, BASE, TH)
    assert breached and "search" in reason


def test_check_breach_latency_breach_and_floor():
    assert guard.check_breach([row(direct_ms=900)] * 3, BASE, TH)[0] is True
    assert guard.check_breach([row(direct_ms=150)] * 3, BASE, TH)[0] is False  # under 200ms floor


def test_check_breach_no_baseline_skips_latency_rule():
    assert guard.check_breach([row(direct_ms=5000)] * 3, {}, TH)[0] is False


def test_check_breach_staleness():
    breached, reason = guard.check_breach([row(stale_p95_s=30)] * 3, BASE, TH)
    assert breached and "staleness" in reason


def test_check_breach_lost_acked_fires_before_full_window():
    """Regression test for the P0 bug: silent data loss must NOT wait for breach_window ticks."""
    window = [row(), row(lost_acked=2)]  # only 2 ticks; breach_window default is 3
    breached, reason = guard.check_breach(window, BASE, TH)
    assert breached and "CRITICAL" in reason


def test_check_breach_wide_window_error_rate_without_any_3_in_a_row():
    window = ([row(direct_ok=False), row(), row(search_ok=False), row()] * 3)[:10]
    breached, reason = guard.check_breach(window, BASE, TH)
    assert breached and "overall probe failure rate" in reason


def test_is_healthy():
    assert guard.is_healthy(row(), BASE, TH)
    assert not guard.is_healthy(row(search_ok=False), BASE, TH)
    assert not guard.is_healthy(row(direct_ms=500), BASE, TH)
    assert not guard.is_healthy(row(stale_p95_s=999), BASE, TH)


# ================================================================== Thresholds
def test_thresholds_validate_ok():
    guard.Thresholds.from_env().validate()


@pytest.mark.parametrize("field,value", [
    ("max_error_rate", 0), ("max_error_rate", 1.5),
    ("max_latency_factor", 1.0), ("min_latency_floor_ms", -1),
    ("max_stale_s", 0), ("breach_window", 0),
    ("recovery_latency_factor", 1.0), ("recovery_stale_s", 0),
    ("error_rate_window", 1),  # < breach_window(3)
])
def test_thresholds_validate_rejects_bad_values(field, value):
    base = dict(max_error_rate=0.3, error_rate_window=10, max_latency_factor=3.0,
                min_latency_floor_ms=200.0, max_stale_s=15.0, breach_window=3,
                recovery_latency_factor=2.0, recovery_stale_s=5.0)
    base[field] = value
    with pytest.raises(guard.GuardError):
        guard.Thresholds(**base).validate()


# ================================================================== baseline validation
def test_validate_baseline_rejects_missing():
    with pytest.raises(guard.GuardError):
        guard.validate_baseline(None, "fresh", 3600)


def test_validate_baseline_rejects_missing_p95():
    with pytest.raises(guard.GuardError):
        guard.validate_baseline({"direct_ms": {}}, "fresh", 3600)


def test_validate_baseline_rejects_unhealthy_rates():
    with pytest.raises(guard.GuardError):
        guard.validate_baseline({"direct_ms": {"p95": 50}, "direct_error_rate": 0.5}, "fresh", 3600)


def test_validate_baseline_rejects_stale_file():
    old = {"direct_ms": {"p95": 50}, "recorded_at": time.time() - 100000}
    with pytest.raises(guard.GuardError):
        guard.validate_baseline(old, "file", 3600)


def test_validate_baseline_accepts_good_fresh():
    good = {"direct_ms": {"p95": 50}, "direct_error_rate": 0.0, "search_error_rate": 0.0}
    guard.validate_baseline(good, "fresh", 3600)  # must not raise


def test_validate_baseline_accepts_good_file_within_age():
    good = {"direct_ms": {"p95": 50}, "direct_error_rate": 0.0, "search_error_rate": 0.0,
            "recorded_at": time.time() - 10}
    guard.validate_baseline(good, "file", 3600)  # must not raise


# ================================================================== resolve_container
def make_run_cmd_for_resolve(ps_line, full_id):
    """`docker ps --format {{.ID}}` returns a SHORT id; `docker inspect -f {{.Id}}`
    (called right after, to get an id that's comparable at revert time) returns the
    FULL id. The fake must distinguish the two calls the way real docker CLI output
    would, or this test can't catch a short-id/full-id mismatch regression."""
    async def fake(args, dry_run=False, check=True, timeout=None):
        if "{{.Id}}" in args:
            return full_id
        return ps_line
    return fake


async def test_resolve_container_exact_match_captures_full_id_not_short_id(monkeypatch):
    # Regression test: docker ps's short id ("abc123") must NOT end up as ContainerRef.id --
    # revert() later compares against the FULL id from `docker inspect`, and a short/full
    # mismatch there was a real bug (always "detects" a false identity change and refuses
    # to restart the container).
    full_id = "abc1234567890123456789012345678901234567890123456789012345678"
    monkeypatch.setattr(guard, "run_cmd",
                         make_run_cmd_for_resolve("docker-things-1\tabc123\trunning", full_id))
    ref = await guard.resolve_container("things", dry_run=False)
    assert ref.name == "docker-things-1"
    assert ref.id == full_id
    assert ref.id != "abc123"


async def test_resolve_container_no_match(monkeypatch):
    monkeypatch.setattr(guard, "run_cmd", make_run_cmd_returning(""))
    with pytest.raises(guard.GuardError):
        await guard.resolve_container("things", dry_run=False)


async def test_resolve_container_ambiguous_match(monkeypatch):
    out = "docker-things-1\tabc\trunning\ndocker-things-2\tdef\trunning"
    monkeypatch.setattr(guard, "run_cmd", make_run_cmd_returning(out))
    with pytest.raises(guard.GuardError):
        await guard.resolve_container("things", dry_run=False)


async def test_resolve_container_filters_by_compose_project_label(monkeypatch):
    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        assert f"label=com.docker.compose.project={guard.COMPOSE_PROJECT}" in args
        return ""  # simulates: nothing from our project matched
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    with pytest.raises(guard.GuardError):
        await guard.resolve_container("things", dry_run=False)


async def test_resolve_container_rejects_not_running(monkeypatch):
    monkeypatch.setattr(guard, "run_cmd", make_run_cmd_returning("docker-things-1\tabc123\texited"))
    with pytest.raises(guard.GuardError):
        await guard.resolve_container("things", dry_run=False)


async def test_resolve_container_dry_run_makes_no_docker_call(monkeypatch):
    called = {"yes": False}

    async def fake_run_cmd(*a, **kw):
        called["yes"] = True
        return ""
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    ref = await guard.resolve_container("things", dry_run=True)
    assert called["yes"] is False
    assert ref.id == "dry-run-fake-id"


async def test_resolve_container_rejects_non_allowlisted():
    with pytest.raises(guard.GuardError):
        await guard.resolve_container("evil-service", dry_run=False)


def make_run_cmd_returning(text):
    async def fake(args, dry_run=False, check=True, timeout=None):
        return text
    return fake


# ================================================================== KillFault
async def test_killfault_inject_calls_docker_kill(monkeypatch):
    calls = []

    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        calls.append(args)
        return ""
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    fault = guard.KillFault(guard.ContainerRef("c1", "id1"), dry_run=False)
    await fault.inject()
    assert calls[0][:2] == ["docker", "kill"]


async def test_killfault_revert_success(monkeypatch):
    seq = iter(["id1", "", "true"])  # current-id check, docker start, running check

    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        return next(seq)
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    fault = guard.KillFault(guard.ContainerRef("c1", "id1"), dry_run=False)
    await fault.revert()  # must not raise


async def test_killfault_revert_detects_identity_mismatch(monkeypatch):
    monkeypatch.setattr(guard, "run_cmd", make_run_cmd_returning("some-other-id"))
    fault = guard.KillFault(guard.ContainerRef("c1", "id1"), dry_run=False)
    with pytest.raises(guard.RollbackError, match="different"):
        await fault.revert()


async def test_killfault_revert_fails_closed_if_never_running(monkeypatch):
    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        if "{{.Id}}" in args:
            return "id1"
        return ""  # "docker start" output and "{{.State.Running}}" both -> never "true"
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)

    async def fast_false(name, timeout):
        return False
    monkeypatch.setattr(guard, "_wait_running", fast_false)  # avoid a real 15s wait
    fault = guard.KillFault(guard.ContainerRef("c1", "id1"), dry_run=False)
    with pytest.raises(guard.RollbackError, match="running"):
        await fault.revert()


# ================================================================== DelayFault
async def test_delayfault_rejects_out_of_range_params():
    with pytest.raises(guard.GuardError):
        guard.DelayFault(guard.ContainerRef("c", "i"), False, ms=999999, ttl=10)
    with pytest.raises(guard.GuardError):
        guard.DelayFault(guard.ContainerRef("c", "i"), False, ms=100, ttl=1)


async def test_delayfault_preflight_missing_image(monkeypatch):
    # Regression test: `docker image inspect` on a MISSING image prints "[]" (non-empty!)
    # to stdout while still exiting non-zero -- the real bug was trusting stdout content
    # instead of the exit code, which silently let a preflight pass with the image absent.
    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        if "image" in args:
            raise guard.GuardError("No such image: nicolaka/netshoot:latest")
        return "qdisc noqueue 0: root"
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    with pytest.raises(guard.GuardError, match="image"):
        await fault.preflight()


async def test_delayfault_preflight_rejects_existing_netem(monkeypatch):
    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        return "sha256:xyz" if "image" in args else "qdisc netem 8001: root delay 100ms"
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    with pytest.raises(guard.GuardError, match="netem"):
        await fault.preflight()


async def test_delayfault_preflight_fails_closed_if_qdisc_check_itself_fails(monkeypatch):
    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        if "image" in args:
            return "sha256:xyz"
        raise guard.GuardError("command timed out after 30.0s")
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    with pytest.raises(guard.GuardError, match="could not read"):
        await fault.preflight()


async def test_delayfault_preflight_passes_when_clean(monkeypatch):
    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        return "sha256:xyz" if "image" in args else "qdisc noqueue 0: root"
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    await fault.preflight()  # must not raise


async def test_delayfault_inject_verifies_it_actually_applied(monkeypatch):
    responses = iter(["", "qdisc netem 8001: root delay 100ms"])

    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        return next(responses)
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    await fault.inject()  # must not raise


async def test_delayfault_inject_raises_if_not_actually_applied(monkeypatch):
    monkeypatch.setattr(guard, "run_cmd", make_run_cmd_returning("qdisc noqueue 0: root"))
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    with pytest.raises(guard.GuardError, match="did not apply"):
        await fault.inject()


async def test_delayfault_revert_is_idempotent_when_already_clean(monkeypatch):
    monkeypatch.setattr(guard, "run_cmd", make_run_cmd_returning("qdisc noqueue 0: root"))
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    await fault.revert()
    await fault.revert()  # calling it twice must still be safe


async def test_delayfault_revert_raises_never_silently_succeeds(monkeypatch):
    monkeypatch.setattr(guard, "run_cmd", make_run_cmd_returning("qdisc netem 8001: root delay 100ms"))
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    with pytest.raises(guard.RollbackError, match="could not confirm"):
        await fault.revert()


async def test_delayfault_revert_fails_closed_when_verification_itself_fails(monkeypatch):
    # Regression test for a real bug found while verifying this tool: if the tc-show
    # verification command itself times out/fails, that must NOT be read as "no netem
    # found => clean". An unverifiable state must never be reported as a successful revert.
    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        if "show" in args[-1]:
            raise guard.GuardError("command timed out after 30.0s")
        return ""  # docker rm / tc qdisc del cleanup calls (check=False) succeed trivially
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    fault = guard.DelayFault(guard.ContainerRef("c1", "id1"), False, ms=100, ttl=10)
    with pytest.raises(guard.RollbackError, match="could not confirm"):
        await fault.revert()


# ================================================================== save() persistence
def test_save_is_atomic_and_filenames_are_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "EXPERIMENTS_DIR", str(tmp_path))
    p1 = guard.save({"id": "exp-1", "value": 1})
    p2 = guard.save({"id": "exp-2", "value": 2})
    assert p1 != p2
    with open(p1) as f:
        assert json.load(f)["value"] == 1
    assert not any(".tmp" in n for n in os.listdir(tmp_path))


def test_save_sanitizes_non_finite_floats(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "EXPERIMENTS_DIR", str(tmp_path))
    path = guard.save({"id": "exp-nan", "bad": float("nan"), "inf": float("inf"), "ok": 1.5})
    with open(path) as f:
        loaded = json.load(f)
    assert loaded["bad"] is None and loaded["inf"] is None and loaded["ok"] == 1.5


# ================================================================== locking
def test_lock_prevents_concurrent_same_target(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LOCKS_DIR", str(tmp_path / ".locks"))
    lock = guard.acquire_lock("things")
    with pytest.raises(guard.GuardError):
        guard.acquire_lock("things")
    guard.release_lock(lock)
    guard.release_lock(guard.acquire_lock("things"))  # free again after release


def test_stale_lock_is_removed_automatically(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LOCKS_DIR", str(tmp_path / ".locks"))
    monkeypatch.setattr(guard, "LOCK_STALE_S", 1.0)
    os.makedirs(tmp_path / ".locks", exist_ok=True)
    (tmp_path / ".locks" / "things.lock").write_text(
        json.dumps({"pid": 1, "started_at": time.time() - 100}))
    lock = guard.acquire_lock("things")  # stale -> should be removed and re-acquired
    assert os.path.exists(lock.path)
    guard.release_lock(lock)


def test_force_unlock_bypasses_an_active_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LOCKS_DIR", str(tmp_path / ".locks"))
    lock1 = guard.acquire_lock("things")
    lock2 = guard.acquire_lock("things", force=True)
    guard.release_lock(lock2)
    assert not os.path.exists(lock1.path)


# ================================================================== run_experiment lifecycle
async def _run(fault, probe_rows, **kw):
    defaults = dict(max_duration=0.3, pre_s=0.02, recovery_timeout=0.3,
                     fresh_baseline=False, max_baseline_age_s=3600,
                     stop_event=None, on_update=None)
    defaults.update(kw)
    th = kw.pop("th", TH) if "th" in kw else TH
    return await guard.run_experiment(fault, "things", guard.ContainerRef("c1", "id1"), th,
                                       defaults["max_duration"], defaults["pre_s"],
                                       defaults["recovery_timeout"], defaults["fresh_baseline"],
                                       defaults["max_baseline_age_s"],
                                       stop_event=defaults["stop_event"], on_update=defaults["on_update"])


async def test_happy_path_completes_and_reverts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2))  # 2 pre ticks, then all healthy
    fault = FakeFault()
    record = await _run(fault, None, max_duration=0.05, recovery_timeout=0.5)
    assert record["state"] == guard.STATE_COMPLETED
    assert record["reverted"] is True and record["rollback_verified"] is True
    assert fault.injected and fault.reverted
    assert record["recovery_outcome"] == "recovered"
    assert record["baseline_source"] == "fresh"
    assert record["schema_version"] == guard.SCHEMA_VERSION


async def test_refuses_injection_when_prechecks_unhealthy(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row(direct_ok=False)] * 3))
    fault = FakeFault()
    record = await _run(fault, None)
    assert record["injected"] is False
    assert fault.injected is False
    assert record["state"] == guard.STATE_ABORTED
    assert "not healthy BEFORE" in record["error"]


async def test_kill_switch_on_consecutive_direct_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    pre_and_fault = [row()] * 2 + [row(direct_ok=False)] * 5
    monkeypatch.setattr(probe, "probe_once", make_probe(pre_and_fault, default=row()))
    fault = FakeFault()
    record = await _run(fault, None, max_duration=5.0, recovery_timeout=0.5)
    assert record["aborted"] is True
    assert "direct-read endpoint failed" in record["abort_reason"]
    assert record["reverted"] is True
    assert record["detected_s"] is not None


async def test_immediate_rollback_on_lost_acked_before_breach_window(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    pre_and_fault = [row()] * 2 + [row(lost_acked=3)]  # single fault tick, below breach_window
    monkeypatch.setattr(probe, "probe_once", make_probe(pre_and_fault, default=row()))
    fault = FakeFault()
    record = await _run(fault, None, max_duration=5.0, recovery_timeout=0.5)
    assert record["aborted"] is True
    assert "CRITICAL" in record["abort_reason"]
    assert record["reverted"] is True


async def test_failed_injection_still_reverts_and_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2))
    fault = FakeFault(fail_inject=True)
    record = await _run(fault, None)
    assert fault.injected is True and fault.reverted is True
    assert record["injection_error"] is not None
    assert record["state"] == guard.STATE_FAILED
    assert record["recovery_outcome"] is None


async def test_failed_rollback_is_recorded_never_masked_as_success(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2))
    fault = FakeFault(fail_revert=True)
    record = await _run(fault, None, max_duration=0.03)
    assert record["reverted"] is False
    assert record["rollback_verified"] is False
    assert record["rollback_error"] is not None
    assert record["state"] == guard.STATE_FAILED


async def test_recovery_timeout_is_recorded_distinctly(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    rows = [row()] * 2 + [row()] * 2  # pre + a couple fault ticks, no breach
    monkeypatch.setattr(probe, "probe_once", make_probe(rows, default=row(direct_ok=False)))
    fault = FakeFault()
    record = await _run(fault, None, max_duration=0.02, recovery_timeout=0.05)
    assert record["recovery_outcome"] == "timeout"
    assert record["state"] == guard.STATE_RECOVERY_TIMEOUT
    assert record["recovery_s"] is None
    assert "not recovered" in record["error"]


async def test_keyboard_interrupt_during_monitoring_still_reverts(monkeypatch, tmp_path):
    # Raise from the plain `await asyncio.sleep(...)` pacing call (not from inside the
    # asyncio.wait_for()-wrapped probe call) so this doesn't trip asyncio's special
    # sub-Task handling of BaseException, which re-raises/logs it a second time and
    # would otherwise print a misleading extra traceback for an already-handled error.
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2, default=row()))
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(delay):
        calls["n"] += 1
        if calls["n"] > 2:  # let the pre-tick + first fault-tick pacing sleep through
            raise KeyboardInterrupt()
        await real_sleep(0)
    monkeypatch.setattr(guard.asyncio, "sleep", fake_sleep)
    fault = FakeFault()
    record = await _run(fault, None, pre_s=0.01, max_duration=5.0, recovery_timeout=0.5)
    assert record["aborted"] is True
    assert record["abort_reason"] == "manual stop (Ctrl+C)"
    assert fault.reverted is True
    assert record["state"] == guard.STATE_ABORTED
    assert record["recovery_outcome"] == "skipped_manual_abort"


async def test_hung_probe_does_not_hang_the_experiment(monkeypatch, tmp_path):
    """A health check that never returns must not block max-duration enforcement."""
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(guard, "PROBE_HARD_TIMEOUT_S", 0.02)
    calls = {"n": 0}

    async def fake_probe(client, ids):
        calls["n"] += 1
        if calls["n"] == 1:
            return dict(row())
        await asyncio.sleep(10)  # would hang forever without the wait_for() wrapper
    monkeypatch.setattr(probe, "probe_once", fake_probe)
    fault = FakeFault()
    record = await _run(fault, None, pre_s=0.01, max_duration=0.06, recovery_timeout=0.05)
    fault_rows = [r for r in record["timeline"] if r["phase"] == "fault"]
    assert any(r.get("probe_timeout") for r in fault_rows)


async def test_experiment_ids_are_unique(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2))
    r1 = await _run(FakeFault(), None, max_duration=0.02, recovery_timeout=0.05)
    r2 = await _run(FakeFault(), None, max_duration=0.02, recovery_timeout=0.05)
    assert r1["id"] != r2["id"]


# ================================================================== stop_event / on_update (orchestrator hook)
async def test_stop_event_set_before_injection_skips_fault_entirely(monkeypatch, tmp_path):
    """orchestrator.py's "stop" button, requested before the fault is even injected:
    nothing to revert, no Docker call should happen."""
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2))
    fault = FakeFault()
    stop_event = asyncio.Event()
    stop_event.set()
    record = await _run(fault, None, max_duration=5.0, recovery_timeout=0.05, stop_event=stop_event)
    assert fault.injected is False
    assert fault.reverted is False
    assert record["aborted"] is True
    assert record["abort_reason"] == "manual stop (requested before injection)"
    assert record["state"] == guard.STATE_ABORTED
    assert record["recovery_outcome"] == "skipped_manual_abort"


async def test_stop_event_set_during_monitoring_reverts_and_verifies_recovery(monkeypatch, tmp_path):
    """Requested mid-experiment: unlike Ctrl+C (STATE_ABORTED, recovery skipped), a
    manual stop from the API should still revert AND verify recovery, like a
    kill-switch breach does."""
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2, default=row()))
    fault = FakeFault()
    stop_event = asyncio.Event()

    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(delay):
        calls["n"] += 1
        if calls["n"] == 2:  # after the first fault tick has been recorded
            stop_event.set()
        await real_sleep(0)
    monkeypatch.setattr(guard.asyncio, "sleep", fake_sleep)

    record = await _run(fault, None, pre_s=0.01, max_duration=5.0, recovery_timeout=0.5,
                         stop_event=stop_event)
    assert fault.injected is True
    assert fault.reverted is True
    assert record["aborted"] is True
    assert record["abort_reason"] == "manual stop (requested during monitoring)"
    assert record["state"] == guard.STATE_COMPLETED
    assert record["recovery_outcome"] == "recovered"


async def test_on_update_callback_sees_live_state_transitions(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2))
    seen_states = []

    def on_update(record):
        seen_states.append(record["state"])
    record = await _run(FakeFault(), None, max_duration=0.02, recovery_timeout=0.05, on_update=on_update)
    assert len(seen_states) > 0
    assert seen_states[-1] == record["state"]  # last callback reflects the final state


async def test_on_update_defaults_to_none_without_error(monkeypatch, tmp_path):
    """on_update is optional -- omitting it (as every existing caller does) must not change behavior."""
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    monkeypatch.setattr(probe, "probe_once", make_probe([row()] * 2))
    record = await _run(FakeFault(), None, max_duration=0.02, recovery_timeout=0.05)
    assert record["state"] in (guard.STATE_COMPLETED, guard.STATE_RECOVERY_TIMEOUT)


# ================================================================== CLI / dry-run / main()
def test_dry_run_never_touches_subprocess(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("subprocess.run must never be called in dry-run")
    monkeypatch.setattr(subprocess, "run", boom)
    args = SimpleNamespace(fault="delay", target="mongodb", ms=100, ttl=10)
    code = asyncio.run(guard.run_dry_run(args))
    assert code == 0


def test_main_rejects_non_allowlisted_target(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["guard.py", "--fault", "kill", "--target", "nope", "--dry-run"])
    assert guard.main() == 2


def test_main_dry_run_exit_zero(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["guard.py", "--fault", "kill", "--target", "things", "--dry-run"])
    assert guard.main() == 0


def test_main_preflight_fails_when_docker_unreachable(monkeypatch):
    async def fake_run_cmd(args, dry_run=False, check=True, timeout=None):
        raise guard.GuardError("docker not reachable")
    monkeypatch.setattr(guard, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(sys, "argv",
                         ["guard.py", "--fault", "kill", "--target", "things", "--preflight-only"])
    assert guard.main() == 2
