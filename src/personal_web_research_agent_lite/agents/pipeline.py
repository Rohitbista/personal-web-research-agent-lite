"""
plan -> research (tool loop) -> synthesize.
Replaces graph.py, edges/, nodes/ and the LangGraph checkpointer.

The old `interrupt_after=["planner_node"]` pause is now simply: call plan(),
show/edit the queries, then call research(). No server-side state needed.

Changes in this version
- plan() also returns a short `title` (same LLM call).
- research() accepts `on_progress(stage)` and `should_cancel()` callbacks (both optional).
  It raises PipelineCancelled at the next checkpoint once should_cancel() is True.
- Sources are structured (`Source`) and numbered; the synthesizer cites them as [1], [2], ...
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from personal_web_research_agent_lite.agents.llm import LLMConfig, chat
from personal_web_research_agent_lite.agents.state import Plan, Report, Source
from personal_web_research_agent_lite.agents.tools import TOOL_SCHEMAS, REGISTRY
from personal_web_research_agent_lite.agents.prompts.planner import PLANNER_SYSTEM_PROMPT
from personal_web_research_agent_lite.agents.prompts.researcher import RESEARCHER_SYSTEM_PROMPT
from personal_web_research_agent_lite.agents.prompts.synthesizer import SYNTHESIZER_SYSTEM_PROMPT
from personal_web_research_agent_lite.config.nepal_time import _nepal_now

_CTX = "src/agents/pipeline"

MAX_ITERATIONS = 6
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]


class PipelineCancelled(Exception):
    """Raised at a checkpoint when the caller's should_cancel() returns True."""


def _emit(on_progress: ProgressFn | None, stage: str) -> None:
    if on_progress is None:
        return
    try:
        on_progress(stage[:60])
    except Exception:  # noqa: BLE001 - progress reporting must never break a run
        pass


def _checkpoint(should_cancel: CancelFn | None) -> None:
    if should_cancel is not None and should_cancel():
        raise PipelineCancelled()


# ───────────────────────── planner ─────────────────────────

# Appended to the user message so a title comes back WITHOUT editing the planner prompt.
# The parser below also accepts the old bare-array format, so either prompt works.
_PLAN_FORMAT = (
    '\n\nReturn ONLY a JSON object of the form '
    '{"title": "<short title, max 60 characters>", "queries": ["<search query>", ...]}. '
    "This format replaces any other output format described earlier."
)


def _parse_plan(text: str) -> tuple[str | None, list[str] | None]:
    """Accepts {"title":..., "queries":[...]} or a bare [...] array. Returns (title, queries)."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    candidates = [text]
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, text, re.S)
        if m:
            candidates.append(m.group())
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        title = None
        if isinstance(data, dict):
            t = data.get("title")
            title = " ".join(t.split()) if isinstance(t, str) and t.strip() else None
            data = data.get("queries")
        if isinstance(data, list):
            qs = [q.strip() for q in data if isinstance(q, str) and q.strip()]
            if qs:
                return title, qs[:5]
    return None, None


def _parse_queries(text: str) -> list[str] | None:  # kept for backwards compatibility
    return _parse_plan(text)[1]


def plan(query: str, llm: LLMConfig, trace_id: str = "") -> Plan:
    now = _nepal_now() if callable(_nepal_now) else _nepal_now
    msg = chat(
        config=llm,
        model=llm.planner_model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT.format(current_datetime=now)},
            {"role": "user", "content": f"Research question: {query}{_PLAN_FORMAT}"},
        ],
        trace_id=trace_id,
        label="planner",
    )
    title, queries = _parse_plan(msg.get("content") or "")
    return Plan(original_query=query, queries=queries or [query], title=(title or "")[:80])


# ───────────────────────── researcher loop ─────────────────────────

def _assistant_msg(raw: dict) -> dict:
    """Keep only the fields the API accepts when echoing an assistant turn back."""
    msg = {"role": "assistant", "content": raw.get("content") or ""}
    if raw.get("tool_calls"):
        msg["tool_calls"] = raw["tool_calls"]
    return msg


def _run_tool(call: dict, trace_id: str) -> tuple[str, str, dict]:
    name = call["function"]["name"]
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        return name, "Error: tool arguments were not valid JSON. Retry with valid JSON.", {}
    if not isinstance(args, dict):
        return name, "Error: tool arguments must be a JSON object.", {}
    fn = REGISTRY.get(name)
    if fn is None:
        return name, f"Error: unknown tool '{name}'.", args
    try:
        return name, fn(**args), args
    except Exception as exc:  # bad argument names/types from the model must not kill the run
        return name, f"Error calling {name}: {exc}", args


_TITLE_LINE = re.compile(r"^\s*(?:title:\s*(.+?)|#\s+(.+?))\s*$", re.I)


def _guess_title(page_text: str) -> str | None:
    """Best effort: a 'Title: ...' line or a markdown H1 near the top of the fetched page."""
    for line in page_text.splitlines()[:15]:
        m = _TITLE_LINE.match(line)
        if m:
            return (m.group(1) or m.group(2)).strip()[:200] or None
    return None


def _source_number(sources: list[Source], url: str, page_text: str) -> int:
    """1-based citation number for `url`, adding it to `sources` the first time it's read."""
    for i, s in enumerate(sources, 1):
        if s.url == url:
            return i
    sources.append(Source(url=url, title=_guess_title(page_text)))
    return len(sources)


def research(plan_: Plan, llm: LLMConfig, trace_id: str = "",
             max_iterations: int = MAX_ITERATIONS, *,
             on_progress: ProgressFn | None = None,
             should_cancel: CancelFn | None = None) -> Report:
    system = RESEARCHER_SYSTEM_PROMPT.format(
        original_query=plan_.original_query,
        research_queries="\n".join(f"  - {q}" for q in plan_.queries),
    )
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": plan_.original_query},
    ]
    sources: list[Source] = []
    call_source: dict[str, int] = {}  # tool_call_id -> citation number of the page it returned
    iterations = 0

    # Every turn is a normal tool turn. We deliberately do NOT send tool_choice="none" on the
    # last turn: Groq rejects it with a 400 (tool_use_failed) whenever the model still tries
    # a tool call. The final written report comes from synthesize(), which reads the
    # gathered tool results as plain text, so no forced researcher summary is needed.
    for iterations in range(1, max_iterations + 1):
        _checkpoint(should_cancel)
        _emit(on_progress, f"researching (step {iterations}/{max_iterations})")
        raw = chat(
            config=llm,
            model=llm.researcher_model,
            temperature=0.5,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            trace_id=trace_id,
            label="researcher",
        )
        messages.append(_assistant_msg(raw))

        calls = raw.get("tool_calls")
        if not calls:
            break

        fetching = any(c["function"]["name"] == "fetch_url_content" for c in calls)
        _emit(on_progress,
              f"{'reading sources' if fetching else 'searching the web'} (step {iterations}/{max_iterations})")

        # Tool calls in one turn are independent -> run them in parallel
        # (they can't be interrupted mid-flight; cancellation is checked right after).
        futures = [_pool.submit(_run_tool, c, trace_id) for c in calls]
        for call, fut in zip(calls, futures):
            name, output, args = fut.result()
            url = args.get("url") if name == "fetch_url_content" else None
            # Only pages that were actually read become citable sources.
            if isinstance(url, str) and url and not output.lstrip().startswith("Error"):
                call_source[call["id"]] = _source_number(sources, url, output)
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": output})

    _checkpoint(should_cancel)
    _emit(on_progress, "writing report")
    report = synthesize(plan_.original_query, messages, llm, trace_id,
                        sources=sources, call_source=call_source)
    return Report(report=report, sources=sources, iterations=iterations)


# ───────────────────────── synthesizer ─────────────────────────

def _messages_to_text(messages: list[dict], call_source: dict[str, int] | None = None) -> str:
    lines = []
    for m in messages:
        role, content = m["role"], m.get("content") or ""
        if role == "user":
            lines.append(f"USER: {content}")
        elif role == "assistant" and content:   # pure tool-call turns have no content
            lines.append(f"RESEARCHER: {content}")
        elif role == "tool":
            n = (call_source or {}).get(m.get("tool_call_id"))
            tag = f"{m.get('name')} | SOURCE [{n}]" if n else f"{m.get('name')}"
            lines.append(f"TOOL RESULT [{tag}]:\n{content}")
    return "\n\n".join(lines)


def _citation_instructions(sources: list[Source]) -> str:
    if not sources:
        return ""
    listing = "\n".join(f"[{i}] {s.title or s.url} - {s.url}" for i, s in enumerate(sources, 1))
    return (
        f"\n\nNUMBERED SOURCES (pages that were actually read):\n{listing}\n\n"
        "Cite claims inline with these numbers, like [1] or [2][3]. Use only the numbers above, "
        "and only for facts that source supports. Do NOT add a Sources/References section at the "
        "end: the app renders it from this list."
    )


def synthesize(original_query: str, messages: list[dict], llm: LLMConfig, trace_id: str = "",
               sources: list[Source] | None = None, call_source: dict[str, int] | None = None) -> str:
    human = (
        f"Here is the research gathered:\n\n{_messages_to_text(messages, call_source)}"
        f"{_citation_instructions(sources or [])}\n\n"
        f"Now write the final research report for: {original_query}"
    )
    msg = chat(
        config=llm,
        model=llm.synthesizer_model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SYNTHESIZER_SYSTEM_PROMPT},
            {"role": "user", "content": human},
        ],
        trace_id=trace_id,
        label="synthesizer",
    )
    content = msg.get("content") or ""
    return content


# ───────────────────────── convenience ─────────────────────────

def run(query: str, llm: LLMConfig, trace_id: str = "",
        approve: Callable[[Plan], Plan] | None = None, **research_kwargs) -> Report:
    """One-shot: plan -> (optional approve/edit hook) -> research -> synthesize.
    Extra kwargs (on_progress, should_cancel) are passed to research()."""
    p = plan(query, llm, trace_id)
    if approve:
        p = approve(p)
    return research(p, llm, trace_id, **research_kwargs)