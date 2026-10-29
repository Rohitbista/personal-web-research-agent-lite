"""Multi-provider chat client. Credentials and models come from the user's saved settings.

All four providers are called through OpenAI-compatible /chat/completions endpoints,
so the pipeline's message/tool format is identical everywhere.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    # Anthropic's OpenAI-compatibility layer (they describe it as best for evaluation/testing;
    # switch to the native /v1/messages API if you want it as a first-class provider).
    "anthropic": "https://api.anthropic.com/v1/chat/completions",
}
_MAX_RETRIES = 3

# One pooled client for the whole process; the API key is sent per request.
_client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0),
                       limits=httpx.Limits(max_connections=20))


@dataclass(frozen=True, slots=True)
class LLMConfig:
    provider: str
    api_key: str = field(repr=False)  # repr=False: never leaks into logs/tracebacks
    planner_model: str = ""
    researcher_model: str = ""
    synthesizer_model: str = ""


class LLMError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)


def chat(
    *,
    config: LLMConfig,
    model: str,
    messages: list[dict],
    temperature: float,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    max_tokens: int | None = None,
    label: str = "llm",
    trace_id: str = "",
) -> dict:
    """Returns the raw assistant message dict ({'role','content','tool_calls'?})."""
    url = _ENDPOINTS.get(config.provider)
    if url is None:
        raise LLMError(f"{label}: unsupported provider '{config.provider}'")
    tag = f"{label}" + (f" [{trace_id}]" if trace_id else "")

    payload: dict = {"model": model, "temperature": temperature, "messages": messages}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    headers = {"Authorization": f"Bearer {config.api_key}"}

    last_error, last_status = "unknown error", None
    for attempt in range(_MAX_RETRIES + 1):
        delay = float(2 ** attempt)
        try:
            resp = _client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()["choices"][0]["message"]
                except (ValueError, KeyError, IndexError, TypeError):
                    raise LLMError(f"{tag}: unexpected response from {config.provider}")

            last_status = resp.status_code
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"

            if resp.status_code in (401, 403):
                raise LLMError(f"{tag}: {config.provider} rejected the API key "
                               f"(HTTP {resp.status_code})", resp.status_code)
            # Some models (e.g. OpenAI reasoning models) only accept the default temperature.
            if (resp.status_code == 400 and "temperature" in payload
                    and "temperature" in resp.text.lower()):
                payload.pop("temperature")
                continue

            retryable = resp.status_code in (429, 500, 502, 503, 504) or (
                resp.status_code == 400 and "tool_use_failed" in resp.text)
            if not retryable:
                break
            try:
                delay = float(resp.headers.get("retry-after", delay))
            except ValueError:
                pass

        if attempt < _MAX_RETRIES:
            time.sleep(min(delay, 30))
    raise LLMError(f"{tag} failed ({config.provider}): {last_error}", last_status)