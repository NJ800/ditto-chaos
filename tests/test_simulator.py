"""Tests for simulator.py's write_state() -- specifically the retry-on-transient-
PermissionError behavior, added after a real crash observed on Windows: os.replace()
can raise PermissionError/WinError 5 when probe.py happens to have state/sent.json
open at the exact moment simulator.py tries to atomically replace it. No real file
I/O race is reproduced here (that's inherently timing-dependent) -- os.replace is
monkeypatched to simulate the transient failure deterministically.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import simulator  # noqa: E402


class FakeCar:
    def __init__(self, cid):
        self.id, self.acked_seq, self.acked_at = cid, 1, 0.0


def test_write_state_retries_past_transient_permission_error(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SENT_FILE", str(tmp_path / "sent.json"))
    monkeypatch.setattr(simulator.time, "sleep", lambda s: None)  # don't actually wait in tests

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("WinError 5: simulated concurrent reader")
        return real_replace(src, dst)
    monkeypatch.setattr(simulator.os, "replace", flaky_replace)

    simulator.write_state([FakeCar("car-1")])  # must not raise

    assert calls["n"] == 3
    assert os.path.isfile(config.SENT_FILE)


def test_write_state_raises_after_exhausting_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SENT_FILE", str(tmp_path / "sent.json"))
    monkeypatch.setattr(simulator.time, "sleep", lambda s: None)

    def always_fails(src, dst):
        raise PermissionError("WinError 5: simulated persistent lock")
    monkeypatch.setattr(simulator.os, "replace", always_fails)

    try:
        simulator.write_state([FakeCar("car-1")])
        assert False, "should have raised after exhausting retries -- a persistent "\
                      "lock must not be silently swallowed"
    except PermissionError:
        pass


def test_write_state_succeeds_immediately_when_no_contention(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SENT_FILE", str(tmp_path / "sent.json"))
    simulator.write_state([FakeCar("car-1")])
    assert os.path.isfile(config.SENT_FILE)
