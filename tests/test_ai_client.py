"""Tests for ai_client.py. Every test mocks httpx -- none of this hits the real
Gemini API (no network, no cost, no flakiness from the model's own availability)."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_client  # noqa: E402
import config  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


def gemini_envelope(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_raises_when_ai_disabled(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    with pytest.raises(ai_client.AIUnavailable, match="AI_ENABLED"):
        ai_client.generate_json("anything")


def test_raises_when_no_api_key(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(ai_client.AIUnavailable, match="GEMINI_API_KEY"):
        ai_client.generate_json("anything")


def test_successful_call_returns_parsed_json(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-not-real")

    def fake_post(url, params=None, json=None, timeout=None):
        return FakeResponse(200, gemini_envelope('{"severity": "medium", "ok": true}'))
    monkeypatch.setattr(ai_client.httpx, "post", fake_post)

    result = ai_client.generate_json("analyze this")
    assert result == {"severity": "medium", "ok": True}


def test_http_error_status_raises_ai_unavailable(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-not-real")

    def fake_post(url, params=None, json=None, timeout=None):
        return FakeResponse(503, text='{"error": {"message": "overloaded"}}')
    monkeypatch.setattr(ai_client.httpx, "post", fake_post)

    with pytest.raises(ai_client.AIUnavailable, match="503"):
        ai_client.generate_json("analyze this")


def test_network_error_raises_ai_unavailable(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-not-real")

    def fake_post(*a, **kw):
        import httpx
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(ai_client.httpx, "post", fake_post)

    with pytest.raises(ai_client.AIUnavailable, match="request failed"):
        ai_client.generate_json("analyze this")


def test_non_json_model_output_raises_ai_unavailable_not_a_crash(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-not-real")

    def fake_post(url, params=None, json=None, timeout=None):
        return FakeResponse(200, gemini_envelope("not actually json"))
    monkeypatch.setattr(ai_client.httpx, "post", fake_post)

    with pytest.raises(ai_client.AIUnavailable, match="could not parse"):
        ai_client.generate_json("analyze this")


def test_malformed_envelope_raises_ai_unavailable(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-not-real")

    def fake_post(url, params=None, json=None, timeout=None):
        return FakeResponse(200, {"candidates": []})  # no content at all
    monkeypatch.setattr(ai_client.httpx, "post", fake_post)

    with pytest.raises(ai_client.AIUnavailable, match="could not parse"):
        ai_client.generate_json("analyze this")


def test_api_key_never_appears_in_error_text(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "super-secret-value-123")

    def fake_post(url, params=None, json=None, timeout=None):
        # Simulate a pathological error response that echoes the request, key included.
        return FakeResponse(400, text="bad request for ...&key=super-secret-value-123&foo=bar")
    monkeypatch.setattr(ai_client.httpx, "post", fake_post)

    with pytest.raises(ai_client.AIUnavailable) as exc_info:
        ai_client.generate_json("analyze this")
    assert "super-secret-value-123" not in str(exc_info.value)


def test_ai_enabled_helper_reflects_both_flag_and_key(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert ai_client.ai_enabled() is False
    monkeypatch.setattr(config, "GEMINI_API_KEY", "x")
    assert ai_client.ai_enabled() is True
    monkeypatch.setattr(config, "AI_ENABLED", False)
    assert ai_client.ai_enabled() is False
