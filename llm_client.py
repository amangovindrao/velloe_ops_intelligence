"""
Thin LLM wrapper used by the agents.

Backends (picked by config.LLM_PROVIDER / env vars):
  - openai_compatible: any /v1/chat/completions server -> OmniRoute, Ollama,
    LM Studio, OpenRouter, vLLM. Uses plain `requests`, no extra package.
  - anthropic: Claude via the official SDK (optional dependency).
  - mock: deterministic demo mode. Each agent passes a `fallback` answer that it
    computed itself, so mock mode is predictable and never invents content.

Provider chain for openai_compatible (first success wins):
  1. primary  LLM_BASE_URL / LLM_MODEL            (e.g. OmniRoute auto/best-free)
  2. fallback LLM_FALLBACK_BASE_URL / each model in LLM_FALLBACK_MODEL (e.g. Gemini free Flash-Lite)
  3. the caller's deterministic answer
A provider that fails is skipped for LLM_COOLDOWN_SECONDS (circuit breaker).

A failed real call (server offline, rate limit, bad key...) never crashes a
run: the caller's deterministic fallback is returned and meta["error"] is set.
API keys are only sent in the Authorization header and never logged.
"""
import re
import sys
import threading
import time
from urllib.parse import urlparse

import requests

from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_COOLDOWN_SECONDS,
    LLM_FALLBACK_API_KEY,
    LLM_FALLBACK_BASE_URL,
    LLM_FALLBACK_MODEL,
    LLM_FALLBACK_TIMEOUT_SECONDS,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TIMEOUT_SECONDS,
    MOCK_MODE,
)

_client = None
_cooldown = {}  # endpoint label -> monotonic time until which it is skipped
_cooldown_lock = threading.Lock()


def _label(base_url, model):
    host = urlparse(base_url).netloc or base_url
    name = "gemini" if "googleapis.com" in host else "omniroute" if host.endswith(":20128") else host
    return f"{name}:{model}"


def endpoints():
    """Ordered OpenAI-compatible endpoints: [(label, base_url, api_key, model, timeout)]. No keys exposed."""
    out = []
    if LLM_BASE_URL and LLM_MODEL:
        out.append((_label(LLM_BASE_URL, LLM_MODEL), LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT_SECONDS))
    if LLM_FALLBACK_BASE_URL:
        for model in filter(None, (m.strip() for m in LLM_FALLBACK_MODEL.split(","))):
            out.append((_label(LLM_FALLBACK_BASE_URL, model), LLM_FALLBACK_BASE_URL, LLM_FALLBACK_API_KEY, model,
                        LLM_FALLBACK_TIMEOUT_SECONDS))
    return out


def describe_chain():
    """Safe summary for the dashboard: labels, order and cooldown state only."""
    now = time.monotonic()
    with _cooldown_lock:
        return [{"label": label, "role": "primary" if i == 0 and LLM_BASE_URL and LLM_MODEL else "fallback",
                 "cooling_down_s": max(0, round(_cooldown.get(label, 0) - now))}
                for i, (label, *_rest) in enumerate(endpoints())]


def active_provider():
    """Which backend will serve calls: 'openai_compatible', 'anthropic' or 'mock'."""
    if MOCK_MODE == "on":
        return "mock"
    if LLM_PROVIDER in ("openai_compatible", "anthropic"):
        return LLM_PROVIDER
    if endpoints():
        return "openai_compatible"
    if ANTHROPIC_API_KEY:
        return "anthropic"
    return "anthropic" if MOCK_MODE == "off" else "mock"


def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic  # imported lazily so mock mode needs no dependency
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _call_anthropic(system, prompt, max_tokens):
    resp = _get_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    usage = getattr(resp, "usage", None)
    return text, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


def _post_chat(base_url, api_key, model, timeout, system, prompt, max_tokens):
    """POST /chat/completions to one OpenAI-compatible server (OmniRoute, Gemini, Ollama, LM Studio...)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }
    resp = requests.post(f"{base_url}/chat/completions", headers=headers, json=body, timeout=timeout)
    if not resp.ok:
        try:
            detail = resp.json().get("error", {})
            detail = detail.get("message") if isinstance(detail, dict) else detail
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"HTTP {resp.status_code}: {str(detail)[:160]}")
    try:
        data = resp.json()
        text = data["choices"][0]["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"malformed LLM response ({type(e).__name__})")
    usage = data.get("usage") or {}
    return text, usage.get("prompt_tokens"), usage.get("completion_tokens")


def _call_openai_compatible(system, prompt, max_tokens):
    """Try each endpoint in order; skip ones in cooldown. Returns (text, in_tok, out_tok, label, errors)."""
    chain = endpoints()
    if not chain:
        raise ValueError("no OpenAI-compatible endpoint configured (LLM_BASE_URL/LLM_MODEL or LLM_FALLBACK_*)")
    now = time.monotonic()
    with _cooldown_lock:
        ready = [e for e in chain if _cooldown.get(e[0], 0) <= now]
    errors = [f"{e[0]}: skipped (cooling down after a failure)" for e in chain if e not in ready]
    for label, base, key, model, timeout in ready or chain[-1:]:  # all cooling down: still try the last one
        try:
            text, in_tok, out_tok = _post_chat(base, key, model, timeout, system, prompt, max_tokens)
            text = _clean(text)
            if not text:
                raise RuntimeError("empty response")
            with _cooldown_lock:
                _cooldown.pop(label, None)
            return text, in_tok, out_tok, label, errors
        except Exception as e:  # noqa: BLE001 - any provider failure moves to the next endpoint
            reason = "timeout" if isinstance(e, requests.Timeout) else str(e)[:160]
            errors.append(f"{label}: {reason}")
            with _cooldown_lock:
                _cooldown[label] = time.monotonic() + LLM_COOLDOWN_SECONDS
            print(f"[llm_client] {label} failed ({reason}); trying next provider", file=sys.stderr)
    raise RuntimeError("all LLM providers failed: " + " | ".join(errors))


def _clean(text):
    """Remove <think>...</think> blocks that reasoning models (Qwen3, DeepSeek-R1) emit, including an
    unclosed block when the model was cut off at max_tokens (any case)."""
    return re.sub(r"<think>.*?(?:</think>|\Z)", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def call_llm(system, prompt, fallback, max_tokens=400):
    """Returns (text, meta). `fallback` is the deterministic answer used in mock mode or on failure."""
    provider = active_provider()
    if provider == "mock":
        return fallback, {"mock": True, "provider": "mock", "provider_call": False,
                          "input_tokens": 0, "output_tokens": 0}
    try:
        errors, label = [], provider
        if provider == "openai_compatible":
            text, in_tok, out_tok, label, errors = _call_openai_compatible(system, prompt, max_tokens)
        else:
            text, in_tok, out_tok = _call_anthropic(system, prompt, max_tokens)
        text = _clean(text)
        if not text:
            raise ValueError("empty LLM response")
        meta = {"mock": False, "provider": label, "provider_call": True,
                "input_tokens": in_tok or len(prompt) // 4, "output_tokens": out_tok or len(text) // 4}
        if errors:  # served by a fallback provider: keep why the earlier ones were not used
            meta["fallback_from"] = errors
        return text, meta
    except Exception as e:
        print(f"[llm_client] {provider} unavailable, using deterministic fallback: {str(e)[:300]}", file=sys.stderr)
        return fallback, {"mock": True, "provider": f"fallback (from {provider})", "provider_call": True,
                          "input_tokens": 0, "output_tokens": 0, "error": str(e)}
