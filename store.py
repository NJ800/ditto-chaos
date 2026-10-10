"""Durable knowledge store (SQLite) -- the index and learning tables on top of the
experiment JSON files, which remain the full source records.

  experiments      one compact, versioned row per experiment (see learning.to_case)
  learning         one learning record per experiment: prediction, observation, comparison,
                   and how the ranking changed once it was stored
  recommendations  every remediation item, its operator decision, and whether a later
                   experiment validated it
  audit            who did what (mode changes, authorizations, lockout resolution)

Existing experiments/*.json files of either schema are imported by sync_from_files();
a malformed file is counted and skipped, never fatal.

Every function raises StoreError when the database is unavailable. Callers treat that as
"learning is degraded" -- it must never block a rollback or a safety decision, and
nothing in scenario.py depends on this module.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

import config
import guard
import learning

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY, kind TEXT, signature TEXT, state TEXT, outcome_class TEXT,
    evidence INTEGER, simulated INTEGER, started_at_utc TEXT, ended_at_utc TEXT,
    schema_version TEXT, software_version TEXT, record_path TEXT, case_json TEXT NOT NULL,
    indexed_at_utc TEXT);
CREATE INDEX IF NOT EXISTS idx_experiments_signature ON experiments(signature);
CREATE TABLE IF NOT EXISTS learning (
    experiment_id TEXT PRIMARY KEY, created_at_utc TEXT, signature TEXT, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recommendations (
    experiment_id TEXT, idx INTEGER, signature TEXT, title TEXT, severity TEXT, category TEXT,
    item_json TEXT NOT NULL, status TEXT, validated_by TEXT, validation_note TEXT, updated_at_utc TEXT,
    PRIMARY KEY (experiment_id, idx));
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY, agent TEXT, version TEXT, experiment_id TEXT, proposal_id TEXT, status TEXT,
    started_at_utc TEXT, finished_at_utc TEXT, duration_ms INTEGER, run_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_agent_runs_exp ON agent_runs(experiment_id);
CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, experiment_id TEXT, proposal_id TEXT,
    type TEXT, data_json TEXT);
CREATE INDEX IF NOT EXISTS idx_timeline_exp ON timeline(experiment_id);
CREATE TABLE IF NOT EXISTS followups (
    followup_id TEXT PRIMARY KEY, original_id TEXT, rec_index INTEGER, recommendation_id TEXT,
    created_at_utc TEXT, confirmed_by TEXT, confirmation_note TEXT, status TEXT,
    completed_at_utc TEXT, result_json TEXT);
CREATE INDEX IF NOT EXISTS idx_followups_original ON followups(original_id);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT, actor TEXT, action TEXT, detail_json TEXT);
"""

# proposed -> accepted -> implemented -> verified | not_effective ; or rejected at any point.
REC_STATUSES = {"proposed", "accepted", "implemented", "verified", "rejected", "not_effective"}
_init_lock = threading.Lock()
_initialized: set[str] = set()


class StoreError(Exception):
    """The knowledge database could not be read or written."""


@contextmanager
def _db():
    path = config.KNOWLEDGE_DB
    conn = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        with _init_lock:
            if path not in _initialized or not os.path.getsize(path):
                conn.executescript(SCHEMA)
                _initialized.add(path)
        yield conn
        conn.commit()
    except (sqlite3.Error, OSError) as e:
        raise StoreError(f"knowledge store unavailable ({config.KNOWLEDGE_DB}): {e}") from e
    finally:
        if conn is not None:
            conn.close()


def ping() -> dict:
    try:
        with _db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        return {"available": True, "experiments": n, "path": config.KNOWLEDGE_DB, "error": None}
    except StoreError as e:
        return {"available": False, "experiments": None, "path": config.KNOWLEDGE_DB, "error": str(e)}


# ---------------------------------------------------------------- experiments
def upsert_experiment(record: dict, analysis: dict | None = None, record_path: str | None = None) -> dict:
    case = learning.to_case(record, analysis)
    if not case["id"]:
        raise StoreError("experiment record has no id")
    with _db() as conn:
        if analysis is None:   # keep severity/findings already stored by an earlier analysis
            row = conn.execute("SELECT case_json FROM experiments WHERE id=?", (case["id"],)).fetchone()
            if row:
                old = json.loads(row["case_json"])
                case["severity"], case["finding_titles"] = old.get("severity"), old.get("finding_titles", [])
        conn.execute(
            "INSERT OR REPLACE INTO experiments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case["id"], case["kind"], case["signature"], case["state"], case["outcome_class"],
             int(case["evidence"]), int(case["simulated"]), case["started_at_utc"], case["ended_at_utc"],
             str(record.get("schema_version")), str(record.get("software_version") or record.get("guard_version")),
             record_path, json.dumps(case), guard.utc_now_iso()))
    return case


def sync_from_files() -> dict:
    """Index every experiment JSON not yet in the store. Idempotent."""
    imported, skipped, errors = 0, 0, []
    with _db() as conn:
        known = {r[0] for r in conn.execute("SELECT id FROM experiments")}
    for path in sorted(glob.glob(os.path.join(config.EXPERIMENTS_DIR, "*.json"))):
        exp_id = os.path.basename(path)[:-5]
        if exp_id in known:
            skipped += 1
            continue
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
            if not isinstance(record, dict) or not record.get("id"):
                raise ValueError("not an experiment record")
            analysis = None
            apath = os.path.join(config.REPORTS_DIR, f"experiment_{record['id']}_analysis.json")
            if os.path.isfile(apath):
                with open(apath, encoding="utf-8") as f:
                    analysis = json.load(f)
            upsert_experiment(record, analysis if isinstance(analysis, dict) else None, path)
            imported += 1
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            errors.append({"file": os.path.basename(path), "error": f"{type(e).__name__}: {e}"[:200]})
    return {"imported": imported, "already_indexed": skipped, "errors": errors}


def history() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT case_json FROM experiments ORDER BY started_at_utc").fetchall()
    return [json.loads(r["case_json"]) for r in rows]


# ---------------------------------------------------------------- learning records
def save_learning(rec: dict) -> None:
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO learning VALUES (?,?,?,?)",
                     (rec["experiment_id"], rec["created_at_utc"], rec["signature"], json.dumps(rec)))


def learning_records(limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT record_json FROM learning ORDER BY created_at_utc DESC LIMIT ?",
                            (int(limit),)).fetchall()
    return [json.loads(r["record_json"]) for r in rows]


def get_learning(experiment_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT record_json FROM learning WHERE experiment_id=?", (experiment_id,)).fetchone()
    return json.loads(row["record_json"]) if row else None


# ---------------------------------------------------------------- recommendations
def upsert_recommendations(experiment_id: str, signature: str, items: list[dict]) -> None:
    """Insert new items as 'proposed'; never overwrite a status an operator or a later
    experiment already set."""
    now = guard.utc_now_iso()
    with _db() as conn:
        for i, item in enumerate(items):
            if item.get("category") == "A":
                continue   # already automated by rollback: nothing to track
            conn.execute(
                "INSERT INTO recommendations VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(experiment_id, idx) DO UPDATE SET item_json=excluded.item_json, "
                "title=excluded.title, severity=excluded.severity, category=excluded.category",
                (experiment_id, i, signature, item.get("title") or item.get("finding_title"),
                 item.get("severity"), item.get("category"), json.dumps(item), "proposed", None, None, now))


def set_recommendation_status(experiment_id: str, idx: int, status: str, validated_by: str | None = None,
                              note: str | None = None) -> None:
    if status not in REC_STATUSES:
        raise StoreError(f"invalid recommendation status '{status}'")
    with _db() as conn:
        conn.execute("UPDATE recommendations SET status=?, validated_by=?, validation_note=?, updated_at_utc=? "
                     "WHERE experiment_id=? AND idx=?",
                     (status, validated_by, note, guard.utc_now_iso(), experiment_id, int(idx)))


def recommendations(status: str | None = None) -> list[dict]:
    q = "SELECT * FROM recommendations"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    with _db() as conn:
        rows = conn.execute(q + " ORDER BY updated_at_utc DESC", args).fetchall()
    return [{"experiment_id": r["experiment_id"], "index": r["idx"], "signature": r["signature"],
             "title": r["title"], "severity": r["severity"], "category": r["category"],
             "status": r["status"], "validated_by": r["validated_by"],
             "validation_note": r["validation_note"], "updated_at_utc": r["updated_at_utc"],
             "item": json.loads(r["item_json"])} for r in rows]


def evaluate_recommendations(case: dict) -> list[dict]:
    """A new evidence run of signature S re-tests every recommendation an operator marked
    IMPLEMENTED after an earlier run of S: if the finding it addressed did not recur it
    becomes verified; if it recurred, not_effective. Returns what changed."""
    if not case.get("evidence"):
        return []
    changed = []
    titles = set(case.get("finding_titles") or [])
    for rec in recommendations("implemented"):
        if rec["signature"] != case["signature"] or rec["experiment_id"] == case["id"]:
            continue
        finding = rec["item"].get("finding_title") or rec["title"]
        recurred = finding in titles
        status = "not_effective" if recurred else "verified"
        note = (f"finding '{finding}' recurred in {case['id']}" if recurred else
                f"finding '{finding}' did not recur in {case['id']} (outcome '{case['outcome_class']}')")
        set_recommendation_status(rec["experiment_id"], rec["index"], status, case["id"], note)
        changed.append({**rec, "status": status, "validated_by": case["id"], "validation_note": note})
    return changed


def pending_validation_signatures() -> set[str]:
    return {r["signature"] for r in recommendations("implemented") if r["signature"]}


def get_recommendation(experiment_id: str, idx: int) -> dict | None:
    return next((r for r in recommendations() if r["experiment_id"] == experiment_id and r["index"] == idx), None)


# ---------------------------------------------------------------- agent runs / timeline
def save_agent_run(run: dict) -> None:
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (run["id"], run["agent"], run["version"], run.get("experiment_id"), run.get("proposal_id"),
                      run["status"], run.get("started_at_utc"), run.get("finished_at_utc"),
                      run.get("duration_ms"), json.dumps(run)))


def agent_runs(experiment_id: str | None = None, agent: str | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT run_json FROM agent_runs", []
    where = []
    if experiment_id:
        where.append("experiment_id=?")
        args.append(experiment_id)
    if agent:
        where.append("agent=?")
        args.append(agent)
    if where:
        q += " WHERE " + " AND ".join(where)
    with _db() as conn:
        rows = conn.execute(q + " ORDER BY rowid DESC LIMIT ?", (*args, int(limit))).fetchall()
    return [json.loads(r["run_json"]) for r in rows]


def add_timeline(type_: str, data: dict, experiment_id: str | None = None, proposal_id: str | None = None,
                 ts: float | None = None) -> None:
    import time
    with _db() as conn:
        conn.execute("INSERT INTO timeline (ts, experiment_id, proposal_id, type, data_json) VALUES (?,?,?,?,?)",
                     (ts or time.time(), experiment_id, proposal_id, type_, json.dumps(data)[:8000]))


def link_proposal(proposal_id: str, experiment_id: str) -> None:
    """Attach planning-phase rows (recorded before an experiment id existed) to the run."""
    with _db() as conn:
        conn.execute("UPDATE timeline SET experiment_id=? WHERE proposal_id=? AND experiment_id IS NULL",
                     (experiment_id, proposal_id))
        conn.execute("UPDATE agent_runs SET experiment_id=? WHERE proposal_id=? AND experiment_id IS NULL",
                     (experiment_id, proposal_id))


def timeline(experiment_id: str, limit: int = 500) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM timeline WHERE experiment_id=? ORDER BY ts, id LIMIT ?",
                            (experiment_id, int(limit))).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "type": r["type"], "data": json.loads(r["data_json"] or "{}")}
            for r in rows]


# ---------------------------------------------------------------- follow-up runs
def create_followup(followup_id: str, original_id: str, rec_index: int, recommendation_id: str | None,
                    confirmed_by: str, note: str) -> None:
    """Link a follow-up experiment to the original experiment and the remediation it tests."""
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO followups VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (followup_id, original_id, int(rec_index), recommendation_id, guard.utc_now_iso(),
                      confirmed_by, note, "running", None, None))


def complete_followup(followup_id: str, status: str, result: dict) -> None:
    with _db() as conn:
        conn.execute("UPDATE followups SET status=?, completed_at_utc=?, result_json=? WHERE followup_id=?",
                     (status, guard.utc_now_iso(), json.dumps(result), followup_id))


def followups(experiment_id: str | None = None) -> list[dict]:
    """Follow-ups where the experiment is either the original or the follow-up, newest first."""
    q, args = "SELECT * FROM followups", ()
    if experiment_id:
        q += " WHERE original_id=? OR followup_id=?"
        args = (experiment_id, experiment_id)
    with _db() as conn:
        rows = conn.execute(q + " ORDER BY created_at_utc DESC, rowid DESC", args).fetchall()
    return [{"followup_id": r["followup_id"], "original_id": r["original_id"], "rec_index": r["rec_index"],
             "recommendation_id": r["recommendation_id"], "created_at_utc": r["created_at_utc"],
             "confirmed_by": r["confirmed_by"], "confirmation_note": r["confirmation_note"],
             "status": r["status"], "completed_at_utc": r["completed_at_utc"],
             "result": json.loads(r["result_json"]) if r["result_json"] else None} for r in rows]


# ---------------------------------------------------------------- audit
def audit(actor: str, action: str, detail: dict | None = None) -> None:
    with _db() as conn:
        conn.execute("INSERT INTO audit (ts_utc, actor, action, detail_json) VALUES (?,?,?,?)",
                     (guard.utc_now_iso(), actor, action, json.dumps(detail or {})))


def audit_log(limit: int = 100) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [{"id": r["id"], "ts_utc": r["ts_utc"], "actor": r["actor"], "action": r["action"],
             "detail": json.loads(r["detail_json"] or "{}")} for r in rows]
