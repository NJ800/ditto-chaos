"""Experiment activity log: the step-by-step "what is happening now" record.

Every experiment record (guard.py single-fault runs and scenario.py multi-fault runs,
real or dry) carries an `events` list. Each event is:

    {"seq": 12, "ts": "2026-10-10T17:20:03+00:00", "t_s": 14.2, "state": "MONITORING",
     "kind": "decision", "message": "...one plain sentence...", "detail": {...}}

kind is one of: info, command, health, decision, warning, error.

This module only RECORDS. It is called from the controllers at the points where they
already made a decision or ran a command; it never influences one. emit() cannot raise
into its caller: a failure to record an event must never get in the way of a kill switch
or a rollback.

Records written before this existed have no `events` key; events_of() returns [] for them.
"""
from __future__ import annotations

import re
import shlex
import time

import config
import guard

KINDS = ("info", "command", "health", "decision", "warning", "error")
MAX_EVENTS = 2000
MAX_TEXT = 600
READ_ONLY_VERBS = {"version", "info", "ps", "inspect", "image", "stats"}

_PATTERNS = [
    (re.compile(r"(?i)\b(key|token|password|passwd|secret|api[_-]?key)=([^\s&\"']+)"), r"\1=***"),
    (re.compile(r"(?i)(authorization:\s*)(\S+\s+)?\S+"), r"\1***"),
    (re.compile(r"(?i)(x-api-token:\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(-e\s+\w*(?:PASS|SECRET|TOKEN|KEY)\w*=)\S+"), r"\1***"),
]


def _secrets() -> list[str]:
    """Configured secret VALUES, redacted wherever they appear verbatim. Short values are
    skipped: the default Ditto password is the word 'ditto', which is not a secret and
    appears in every container name."""
    return [s for s in (config.API_TOKEN, config.GEMINI_API_KEY, config.DITTO_PASS) if s and len(s) >= 8]


def sanitize(value):
    """Redact secrets and bound sizes, recursively. Returns JSON-safe data."""
    if isinstance(value, str):
        text = value
        for secret in _secrets():
            text = text.replace(secret, "***")
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "... (truncated)"
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in list(value)[:60]]
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if value is None or isinstance(value, (bool, int)):
        return value
    return sanitize(str(value))


def emit(record: dict, kind: str, message: str, **detail) -> dict | None:
    """Append one event to record['events']. Never raises."""
    try:
        if kind not in KINDS:
            kind = "info"
        seq = int(record.get("event_seq") or 0) + 1
        record["event_seq"] = seq                     # survives trimming: seq never restarts
        t0 = record.get("events_t0")
        if t0 is None:
            t0 = record["events_t0"] = time.time()
        event = {"seq": seq, "ts": guard.utc_now_iso(), "t_s": round(max(0.0, time.time() - t0), 2),
                 "state": record.get("state"), "kind": kind, "message": sanitize(message),
                 "detail": sanitize(detail) if detail else {}}
        events = record.setdefault("events", [])
        events.append(event)
        if len(events) > MAX_EVENTS:
            del events[: len(events) - MAX_EVENTS]
        return event
    except Exception as e:  # noqa: BLE001 -- recording must never break the controller
        print(f"[activity] could not record an event: {type(e).__name__}: {e}", flush=True)
        return None


def events_of(record: dict | None) -> list[dict]:
    """Events of any record, including legacy ones that have none."""
    events = (record or {}).get("events")
    return [e for e in events if isinstance(e, dict) and isinstance(e.get("seq"), int)] \
        if isinstance(events, list) else []


def events_after(record: dict | None, after_seq: int) -> list[dict]:
    return sorted((e for e in events_of(record) if e["seq"] > after_seq), key=lambda e: e["seq"])


# ---------------------------------------------------------------- commands
def command_sink(record: dict):
    """Callback for guard.run_cmd(): turns each command (run or only previewed) into a
    `command` event. What it reports is what run_cmd observed, not what was planned."""
    def on_command(info: dict) -> None:
        argv = [str(a) for a in info.get("argv", [])]
        text = sanitize(" ".join(shlex.quote(a) for a in argv))
        read_only = len(argv) > 1 and argv[1] in READ_ONLY_VERBS
        detail = {"command": text, "executed": bool(info.get("executed")), "read_only": read_only}
        if not info.get("executed"):
            emit(record, "command", f"Would run (dry run, not executed): {text}", **detail)
            return
        detail.update(returncode=info.get("returncode"), duration_ms=info.get("duration_ms"),
                      timed_out=bool(info.get("timed_out")))
        if info.get("stderr"):
            detail["stderr"] = str(info["stderr"])[:400]
        if info.get("timed_out"):
            emit(record, "command", f"Command timed out after {info.get('timeout_s')}s; its effect is unknown: {text}",
                 **detail)
        elif info.get("returncode") not in (0, None):
            emit(record, "command", f"Command exited {info.get('returncode')}: {text}", **detail)
        else:
            emit(record, "command", f"Ran: {text}", **detail)
    return on_command


def preview_command(record: dict, text: str, exact: bool = False) -> None:
    emit(record, "command", f"Would run (dry run, not executed): {text}",
         command=text, executed=False, read_only=False, exact_argv=exact)


# ---------------------------------------------------------------- health / decisions
def _v(value, unit: str = "", digits: int = 0) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unavailable"
    return f"{value:.{digits}f}{unit}"


def health(record: dict, row: dict) -> None:
    """One `health` event for one probe tick. A missing measurement is 'unavailable'."""
    if row.get("probe_timeout"):
        emit(record, "health", "Health probe did not answer in time; this tick counts as failed.",
             direct_ok=False, search_ok=False, probe_timeout=True, phase=row.get("phase"))
        return
    d = f"OK {_v(row.get('direct_ms'), ' ms')}" if row.get("direct_ok") else "FAILED"
    s = f"OK {_v(row.get('search_ms'), ' ms')}" if row.get("search_ok") else "FAILED"
    emit(record, "health",
         f"Direct read {d}; search {s}; staleness p95 {_v(row.get('stale_p95_s'), ' s', 1)}; "
         f"lost updates {_v(row.get('lost_acked'))}; search drift {_v(row.get('search_drift'))}.",
         direct_ok=bool(row.get("direct_ok")), direct_ms=row.get("direct_ms") if row.get("direct_ok") else None,
         search_ok=bool(row.get("search_ok")), search_ms=row.get("search_ms") if row.get("search_ok") else None,
         stale_p95_s=row.get("stale_p95_s"), lost_acked=row.get("lost_acked"),
         search_drift=row.get("search_drift"), phase=row.get("phase"), active_faults=row.get("active_faults"))


def latency_limit(baseline: dict | None, th: "guard.Thresholds") -> float | None:
    p95 = ((baseline or {}).get("direct_ms") or {}).get("p95")
    return max(p95 * th.max_latency_factor, th.min_latency_floor_ms) if p95 else None


def within_limits(row: dict, baseline: dict | None, th: "guard.Thresholds") -> str:
    """Plain-English reason for continuing, built from the same numbers check_breach saw."""
    parts = []
    limit = latency_limit(baseline, th)
    if row.get("direct_ok") and limit is not None:
        parts.append(f"direct read {_v(row.get('direct_ms'), ' ms')} versus the {limit:.0f} ms limit")
    elif not row.get("direct_ok"):
        parts.append(f"direct read failed this tick (stops after {th.breach_window} in a row)")
    if not row.get("search_ok"):
        parts.append(f"search failed this tick (stops after {th.breach_window} in a row)")
    if row.get("stale_p95_s") is not None:
        parts.append(f"staleness {_v(row.get('stale_p95_s'), ' s', 1)} versus the {th.max_stale_s:g} s limit")
    parts.append("no lost updates" if row.get("lost_acked") == 0 else "lost-update check unavailable"
                 if row.get("lost_acked") is None else f"{row.get('lost_acked')} lost update(s)")
    return "; ".join(parts) + ": no stop condition met, continuing."


def thresholds(record: dict, baseline: dict | None, th: "guard.Thresholds", source: str | None) -> None:
    """Explain the baseline and the configured stop conditions with their actual numbers."""
    p95 = ((baseline or {}).get("direct_ms") or {}).get("p95")
    recorded = (baseline or {}).get("recorded_at")
    age = f"{(time.time() - recorded) / 60:.0f} min old" if isinstance(recorded, (int, float)) else "measured just now"
    emit(record, "info",
         f"Baseline from {'baseline.json' if source == 'file' else 'fresh pre-check measurements'} ({age}): "
         f"direct read p95 {_v(p95, ' ms')}. Baseline validation passed.",
         baseline_source=source, baseline_direct_p95_ms=p95)
    limit = latency_limit(baseline, th)
    emit(record, "info",
         f"Stop conditions: any lost acknowledged update; direct read or search failing {th.breach_window} ticks in a row; "
         f"more than {th.max_error_rate:.0%} of the last {th.error_rate_window} ticks failing; direct read above "
         f"{_v(limit, ' ms')} ({th.max_latency_factor:g}x baseline, floor {th.min_latency_floor_ms:g} ms) for "
         f"{th.breach_window} ticks; staleness above {th.max_stale_s:g} s for {th.breach_window} ticks.",
         latency_limit_ms=limit, max_stale_s=th.max_stale_s, breach_window=th.breach_window,
         max_error_rate=th.max_error_rate)
