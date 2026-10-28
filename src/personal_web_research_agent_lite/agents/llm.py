"""Minimal Groq client (OpenAI-compatible endpoint) -- replaces langchain_groq."""
import time
import httpx

from personal_web_research_agent_lite.config.settings import GROQ_API_KEY

_URL = "https://api.groq.com/openai/v1/chat/completions"
_MAX_RETRIES = 3

# One pooled client for the whole process (keep-alive, no per-call TLS handshake)
_client = httpx.Client(
    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
    timeout=httpx.Timeout(60.0, connect=10.0),
)


class LLMError(RuntimeError):
    pass


def chat(
    *,
    model: str,
    messages: list[dict],
    temperature: float,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    label: str = "llm",
    trace_id: str = "",
) -> dict:
    """Returns the raw assistant message dict ({'role','content','tool_calls'?})."""
    payload: dict = {"model": model, "temperature": temperature, "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"

    last_error = "unknown error"
    for attempt in range(_MAX_RETRIES + 1):
        delay = float(2 ** attempt)
        try:
            resp = _client.post(_URL, json=payload)
        except httpx.HTTPError as exc:
            last_error = str(exc)
        else:
            if resp.status_code == 200:
                data = resp.json()
                choice = data["choices"][0]
                msg = choice["message"]
                return msg

            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            retryable = resp.status_code in (429, 500, 502, 503, 504) or (
                resp.status_code == 400 and "tool_use_failed" in resp.text
            )
            if not retryable:
                break
            try:
                delay = float(resp.headers.get("retry-after", delay))
            except ValueError:
                pass

        if attempt < _MAX_RETRIES:
            time.sleep(min(delay, 30))
    raise LLMError(f"{label} failed" + (f" [{trace_id}]" if trace_id else "") + f": {last_error}")