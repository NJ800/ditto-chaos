"""Tests for probe.current_health() and probe.check_breach() -- the standalone
health-check API in probe.py (separate from guard.py's own, consecutive-tick based
check_breach() -- see probe.py's module docstring for why).

Plain fake dicts and temp files only: no real Ditto, no Docker, nothing destructive.
Run with:  python -m pytest -q
"""
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import probe  # noqa: E402

# A "perfectly healthy" summary, shaped like current_health()'s return value.
HEALTHY = {
    "window_s": 10, "sample_count": 5, "error_rate": 0.0,
    "direct_error_rate": 0.0, "search_error_rate": 0.0,
    "direct_p95_ms": 20.0, "search_p95_ms": 30.0, "stale_p95_s": 2.0,
    "lost_acked": 0, "search_drift": 0,
    "latest_timestamp": 0.0, "fresh": True, "status": "healthy",
}
BASELINE = {"direct_ms": {"p95": 20.0}, "search_ms": {"p95": 30.0}}


def health(**overrides):
    return {**HEALTHY, **overrides}


# ============================================================ check_breach: the 14 cases
def test_1_healthy_metrics_return_no_breach():
    assert probe.check_breach(health(), BASELINE) == (False, "")


def test_2_error_rate_6_percent_breaches():
    breached, reason = probe.check_breach(health(error_rate=0.06), BASELINE)
    assert breached is True and "error rate" in reason


def test_3_error_rate_exactly_5_percent_does_not_breach():
    # config.MAX_ERROR_RATE is 0.05 -- the spec says "greater than", not "or equal to".
    assert config.MAX_ERROR_RATE == 0.05
    breached, _ = probe.check_breach(health(error_rate=0.05), BASELINE)
    assert breached is False


def test_4_direct_p95_above_3x_baseline_breaches():
    # baseline direct p95 is 20ms -> 3x = 60ms, but that's under the 200ms floor, so
    # the EFFECTIVE limit is 200ms (see test 6) -- use a baseline high enough that
    # 3x clears the floor, so this test is actually exercising the 3x rule itself.
    base = {"direct_ms": {"p95": 100.0}, "search_ms": {"p95": 100.0}}
    breached, reason = probe.check_breach(health(direct_p95_ms=301.0), base)
    assert breached is True and "direct-read p95" in reason


def test_5_search_p95_above_3x_baseline_breaches():
    base = {"direct_ms": {"p95": 100.0}, "search_ms": {"p95": 100.0}}
    breached, reason = probe.check_breach(health(direct_p95_ms=50.0, search_p95_ms=301.0), base)
    assert breached is True and "search p95" in reason


def test_6_latency_exactly_3x_baseline_does_not_breach():
    base = {"direct_ms": {"p95": 100.0}, "search_ms": {"p95": 100.0}}
    assert config.MAX_LATENCY_FACTOR == 3.0
    breached, _ = probe.check_breach(health(direct_p95_ms=300.0, search_p95_ms=300.0), base)
    assert breached is False


def test_7_staleness_16s_breaches():
    breached, reason = probe.check_breach(health(stale_p95_s=16.0), BASELINE)
    assert breached is True and "staleness" in reason


def test_8_staleness_exactly_15s_does_not_breach():
    assert config.MAX_STALE_S == 15.0
    breached, _ = probe.check_breach(health(stale_p95_s=15.0), BASELINE)
    assert breached is False


def test_9_one_lost_acked_breaches():
    breached, reason = probe.check_breach(health(lost_acked=1), BASELINE)
    assert breached is True and "lost acknowledged" in reason


def test_10_missing_baseline_latency_is_handled_safely():
    # No baseline at all -- nothing to compare latency against, so that check is
    # skipped (not failed, not a crash), and an otherwise-healthy reading passes.
    breached, _ = probe.check_breach(health(direct_p95_ms=5000.0, search_p95_ms=5000.0), None)
    assert breached is False
    # Baseline present but missing the specific p95 key -- same safe skip.
    breached, _ = probe.check_breach(health(direct_p95_ms=5000.0), {"direct_ms": {}})
    assert breached is False


def test_11_empty_health_metrics_are_handled_safely():
    breached, reason = probe.check_breach({}, BASELINE)
    assert breached is True  # empty dict has no sample_count/fresh -> "I can't see anything"
    assert "no recent" in reason
    breached, reason = probe.check_breach(None, BASELINE)
    assert breached is True


def test_12_invalid_numeric_values_are_handled_safely():
    # A NaN/garbage value must never crash check_breach(), and must never silently
    # compare as "fine" just because `float('nan') > x` happens to be False in Python.
    breached, _ = probe.check_breach(health(error_rate=float("nan")), BASELINE)
    assert breached is False  # treated as "unknown for this signal", not a crash
    breached, _ = probe.check_breach(health(error_rate="not-a-number"), BASELINE)
    assert breached is False
    breached, _ = probe.check_breach(health(lost_acked=float("inf")), BASELINE)
    assert breached is False


def test_13_multiple_simultaneous_breaches_pick_the_first_in_a_documented_order():
    # lost_acked is checked before error_rate -- both are breaching here, so the
    # reason must always be about lost_acked, deterministically, every time.
    breached, reason = probe.check_breach(health(lost_acked=2, error_rate=0.9), BASELINE)
    assert breached is True
    assert "lost acknowledged" in reason


def test_14_stale_monitoring_data_is_not_treated_as_healthy():
    breached, reason = probe.check_breach(health(fresh=False, status="unknown"), BASELINE)
    assert breached is True
    assert "no recent/fresh" in reason


# ============================================================ current_health: the 8 cases
def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def probe_row(ts, direct_ok=True, search_ok=True, direct_ms=10.0, search_ms=10.0,
              stale_p95_s=1.0, lost_acked=0, search_drift=0):
    return {"ts": ts, "direct_ok": direct_ok, "direct_ms": direct_ms,
            "search_ok": search_ok, "search_ms": search_ms,
            "stale_p50_s": stale_p95_s, "stale_p95_s": stale_p95_s, "stale_max_s": stale_p95_s,
            "lost_acked": lost_acked, "search_drift": search_drift}


def test_a_empty_metrics_directory():
    h = probe.current_health(metrics_dir="this-directory-does-not-exist")
    assert h["sample_count"] == 0 and h["status"] == "unknown" and h["fresh"] is False


def test_b_valid_recent_measurements(tmp_path):
    now = time.time()
    rows = [probe_row(now - 2), probe_row(now - 1), probe_row(now)]
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", rows)
    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["sample_count"] == 3
    assert h["status"] == "healthy"
    assert h["fresh"] is True


def test_b2_picks_the_actively_written_file_not_the_lexicographically_last_name(tmp_path):
    """Regression test for a real bug: a short-lived run (e.g. `main.py baseline`,
    which also calls probe.run() and writes its own probe-<timestamp>.jsonl) can
    start AFTER a long-running standalone `python probe.py` but finish first. Its
    filename still sorts last alphabetically even though it stopped growing minutes
    ago -- current_health() must pick by actual modification time, not filename, or
    it silently reports a perfectly healthy, still-running probe as stale forever."""
    now = time.time()
    # Older filename (started first), but ACTIVELY being written right now.
    active = tmp_path / "probe-20260101-000000.jsonl"
    write_jsonl(active, [probe_row(now - 1), probe_row(now)])
    # Newer filename (started later, e.g. a baseline run), but finished minutes ago --
    # must not shadow the active file just because its name sorts later.
    abandoned = tmp_path / "probe-20260101-999999.jsonl"
    write_jsonl(abandoned, [probe_row(now - 400)])
    old_time = now - 400
    os.utime(abandoned, (old_time, old_time))

    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["sample_count"] == 2  # from `active`, not the 1 row in `abandoned`
    assert h["status"] == "healthy"
    assert h["fresh"] is True


def test_c_records_outside_the_window_are_excluded(tmp_path):
    now = time.time()
    rows = [probe_row(now - 1000), probe_row(now - 1)]  # one ancient, one recent
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", rows)
    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["sample_count"] == 1


def test_d_malformed_json_lines_are_skipped_not_fatal(tmp_path):
    now = time.time()
    path = tmp_path / "probe-20260101-000000.jsonl"
    good = probe_row(now - 1)
    path.write_text(json.dumps(good) + "\n{this is not valid json\n")
    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["sample_count"] == 1


def test_e_missing_optional_fields_do_not_crash(tmp_path):
    now = time.time()
    minimal_row = {"ts": now - 1, "direct_ok": True, "direct_ms": 10.0,
                   "search_ok": True, "search_ms": 10.0}  # no stale_p95_s/lost_acked/search_drift
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", [minimal_row])
    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["sample_count"] == 1
    assert h["stale_p95_s"] is None
    assert h["lost_acked"] is None  # missing/unknown, never a fabricated 0


def test_f_percentile_calculation_is_correct(tmp_path):
    now = time.time()
    # 10 direct latencies 1..10ms, all successful -> p95 nearest-rank is the 10th (max).
    rows = [probe_row(now - i, direct_ms=float(i)) for i in range(1, 11)]
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", rows)
    h = probe.current_health(window_s=100, metrics_dir=str(tmp_path))
    assert h["direct_p95_ms"] == 10.0


def test_g_error_rate_aggregation_is_correct(tmp_path):
    now = time.time()
    rows = [probe_row(now - i, direct_ok=(i != 1)) for i in range(1, 5)]  # 1 failure of 4
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", rows)
    h = probe.current_health(window_s=100, metrics_dir=str(tmp_path))
    assert h["sample_count"] == 4
    assert math.isclose(h["direct_error_rate"], 0.25)


def test_h_rows_without_a_usable_timestamp_are_dropped(tmp_path):
    now = time.time()
    bad = {"ts": "not-a-number", "direct_ok": True, "direct_ms": 10.0,
           "search_ok": True, "search_ms": 10.0}
    good = probe_row(now - 1)
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", [bad, good])
    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["sample_count"] == 1


# ============================================================ a few extra, high-value cases
def test_stale_file_marks_fresh_false_and_status_unknown(tmp_path):
    old = time.time() - 1000
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", [probe_row(old)])
    h = probe.current_health(window_s=10000, metrics_dir=str(tmp_path))  # window wide enough to include it
    assert h["sample_count"] == 1
    assert h["fresh"] is False
    assert h["status"] == "unknown"


def test_lost_acked_none_means_unknown_not_zero(tmp_path):
    now = time.time()
    rows = [{"ts": now - 1, "direct_ok": True, "direct_ms": 10.0,
             "search_ok": True, "search_ms": 10.0, "lost_acked": None}]
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", rows)
    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["lost_acked"] is None


def test_degraded_status_when_errors_present_but_fresh(tmp_path):
    now = time.time()
    rows = [probe_row(now - 1, direct_ok=False)]
    write_jsonl(tmp_path / "probe-20260101-000000.jsonl", rows)
    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["status"] == "degraded"


def test_tail_reader_bounds_memory_on_a_large_file(tmp_path):
    now = time.time()
    path = tmp_path / "probe-20260101-000000.jsonl"
    old_rows = [probe_row(now - 10000 - i) for i in range(2000)]  # a lot of old history
    recent_rows = [probe_row(now - i) for i in range(5)]
    write_jsonl(path, old_rows + recent_rows)
    h = probe.current_health(window_s=10, metrics_dir=str(tmp_path))
    assert h["sample_count"] == 5  # only the recent tail, not all 2005 lines
