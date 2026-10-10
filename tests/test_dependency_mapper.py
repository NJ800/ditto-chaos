"""Tests for dependency_mapper.py -- fully deterministic, no AI, no real Docker call
(subprocess.run is monkeypatched)."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dependency_mapper  # noqa: E402

FAKE_PS_OUTPUT = (
    "docker-gateway-1\teclipse/ditto-gateway:latest\trunning\tgateway\n"
    "docker-things-1\teclipse/ditto-things:latest\trunning\tthings\n"
    "docker-mongodb-1\tmongo:7.0\trunning\tmongodb\n"
    "docker-nginx-1\tnginx:1.27-alpine\trunning\tnginx\n"
)


def fake_run_ok(*a, **kw):
    return SimpleNamespace(returncode=0, stdout=FAKE_PS_OUTPUT, stderr="")


def fake_run_fail(*a, **kw):
    return SimpleNamespace(returncode=1, stdout="", stderr="permission denied")


def test_build_dependency_map_computes_fan_in_without_hardcoding(monkeypatch):
    monkeypatch.setattr(dependency_mapper.subprocess, "run", fake_run_ok)
    m = dependency_mapper.build_dependency_map("docker")
    assert m["docker_query_error"] is None
    assert {s["name"] for s in m["services"]} == {"gateway", "things", "mongodb", "nginx"}
    # mongodb has one real incoming edge here (things->mongodb); gateway->things-search/policies/
    # connectivity edges still exist in the KNOWN_ARCHITECTURE list but those services aren't
    # "observed" in this fake docker ps output -- the mapper must still record the edge (it's a
    # documented fact about Ditto) while flagging both_ends_observed_running=False for it.
    mongo_edges = [e for e in m["edges"] if e["to"] == "mongodb"]
    assert any(e["from"] == "things" and e["both_ends_observed_running"] for e in mongo_edges)
    gateway_to_search = [e for e in m["edges"] if e["from"] == "gateway" and e["to"] == "things-search"]
    assert gateway_to_search and gateway_to_search[0]["both_ends_observed_running"] is False


def test_fan_in_is_calculated_not_hardcoded(monkeypatch):
    monkeypatch.setattr(dependency_mapper.subprocess, "run", fake_run_ok)
    m = dependency_mapper.build_dependency_map("docker")
    # mongodb is the target of 4 known-architecture edges total (things, things-search,
    # policies, connectivity all persist to it) -- this must come out of counting edges,
    # not from any special-cased "mongodb is critical" branch in the code.
    assert m["fan_in"]["mongodb"] == 4
    # Remove one known-architecture edge into mongodb and the count must drop by
    # exactly one -- proving fan_in is a live count over `edges`, not a constant.
    trimmed_edges = [e for e in dependency_mapper.KNOWN_ARCHITECTURE_EDGES if e[1] != "mongodb" or e[0] != "policies"]
    monkeypatch.setattr(dependency_mapper, "KNOWN_ARCHITECTURE_EDGES", trimmed_edges)
    m2 = dependency_mapper.build_dependency_map("docker")
    assert m2["fan_in"]["mongodb"] == 3


def test_docker_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(dependency_mapper.subprocess, "run", fake_run_fail)
    m = dependency_mapper.build_dependency_map("docker")
    assert m["docker_query_error"] is not None
    assert m["services"] == []
    # Known-architecture edges are still produced (they're documented facts, not
    # dependent on docker being reachable), but nothing can be "observed running".
    assert len(m["edges"]) == len(dependency_mapper.KNOWN_ARCHITECTURE_EDGES)
    assert all(not e["both_ends_observed_running"] for e in m["edges"])


def test_allowlist_cross_reference_uses_guards_real_allowlist(monkeypatch):
    monkeypatch.setattr(dependency_mapper.subprocess, "run", fake_run_ok)
    m = dependency_mapper.build_dependency_map("docker")
    by_name = {r["service"]: r for r in m["fault_target_recommendations"]}
    assert by_name["nginx"]["allowlisted_for_faults"] is False
    assert by_name["mongodb"]["allowlisted_for_faults"] is True
    assert "NOT in guard.py's ALLOWLIST" in by_name["nginx"]["reasoning"]


def test_save_writes_both_files_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(dependency_mapper.subprocess, "run", fake_run_ok)
    m = dependency_mapper.build_dependency_map("docker")
    jp, mp = dependency_mapper.save(m, out_dir=str(tmp_path))
    assert os.path.exists(jp) and os.path.exists(mp)
    assert not any(".tmp" in n for n in os.listdir(tmp_path))


def test_mermaid_output_is_well_formed(monkeypatch):
    monkeypatch.setattr(dependency_mapper.subprocess, "run", fake_run_ok)
    m = dependency_mapper.build_dependency_map("docker")
    mermaid = dependency_mapper.to_mermaid(m)
    assert mermaid.startswith("flowchart TD")
    assert "things_search" in mermaid  # hyphens sanitized to underscores for Mermaid node ids


def test_manual_overrides_are_loaded_and_labeled(tmp_path, monkeypatch):
    monkeypatch.setattr(dependency_mapper.subprocess, "run", fake_run_ok)
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        "edges:\n  - from: connectivity\n    to: things\n    note: observed via logs\n")
    monkeypatch.chdir(tmp_path)
    loaded = dependency_mapper._load_overrides("overrides.yaml")
    assert loaded == [{"from": "connectivity", "to": "things", "source": "manual-override",
                        "note": "observed via logs"}]


def test_missing_overrides_file_returns_empty_list():
    assert dependency_mapper._load_overrides("no-such-file.yaml") == []
