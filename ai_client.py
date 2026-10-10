"""Thin wrapper around Google's Gemini API -- plain HTTPS via httpx, no extra SDK
dependency (the project already depends on httpx for everything else).

Every AI-touching module in this project (resilience_agent.py, ...) MUST catch
AIUnavailable and fall back to a deterministic, clearly-labeled analysis -- never
crash, and never silently fabricate a result when AI is disabled, unconfigured, or
the model call fails for any reason.
"""
from __future__ import annotations

import json
import re

import httpx

import config


class AIUnavailable(Exception):
    """Raised whenever an AI call can't be made or didn't return usable JSON.
    Callers must catch this and use their own deterministic fallback."""


def _redact(text: str) -> str:
    """Defense in depth: strip anything that looks like an API key from error text
    before it's raised/logged, even though the key is normally a query param kept
    out of response bodies."""
    return re.sub(r"key=[^&\s\"']+", "key=***", text)


def ai_enabled() -> bool:
    return bool(config.AI_ENABLED and config.GEMINI_API_KEY)


def provider_name() -> str:
    return "gemini"


def model_info() -> dict:
    return {"provider": provider_name(), "model": config.GEMINI_MODEL, "enabled": ai_enabled()}


def generate_json(prompt: str, timeout: float | None = None) -> dict:
    """Ask Gemini for a JSON object. Returns the parsed dict, or raises
    AIUnavailable -- never returns a partially-parsed or guessed result.
    """
    if not config.AI_ENABLED:
        raise AIUnavailable("AI_ENABLED is false")
    if not config.GEMINI_API_KEY:
        raise AIUnavailable("no GEMINI_API_KEY configured")

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{config.GEMINI_MODEL}:generateContent")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    try:
        r = httpx.post(url, params={"key": config.GEMINI_API_KEY}, json=body,
                        timeout=timeout or config.AI_REQUEST_TIMEOUT_S)
    except httpx.HTTPError as e:
        raise AIUnavailable(f"request failed: {_redact(str(e))}") from e

    if r.status_code != 200:
        raise AIUnavailable(f"HTTP {r.status_code}: {_redact(r.text[:300])}")

    try:
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise AIUnavailable(f"could not parse model response as JSON: {e}") from e
