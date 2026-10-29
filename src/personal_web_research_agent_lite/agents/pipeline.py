"""
plan -> research (tool loop) -> synthesize.
Replaces graph.py, edges/, nodes/ and the LangGraph checkpointer.
 
The old `interrupt_after=["planner_node"]` pause is now simply: call plan(),
show/edit the queries, then call research(). No server-side state needed.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
 
from personal_web_research_agent_lite.agents.llm import LLMConfig, chat
from personal_web_research_agent_lite.agents.state import Plan, Report
from personal_web_research_agent_lite.agents.tools import TOOL_SCHEMAS, REGISTRY
from personal_web_research_agent_lite.agents.prompts.planner import PLANNER_SYSTEM_PROMPT
from personal_web_research_agent_lite.agents.prompts.researcher import RESEARCHER_SYSTEM_PROMPT
from personal_web_research_agent_lite.agents.prompts.synthesizer import SYNTHESIZER_SYSTEM_PROMPT
from personal_web_research_agent_lite.config.nepal_time import _nepal_now
 
_CTX = "src/agents/pipeline"
 
MAX_ITERATIONS = 6
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")
 
 
# ───────────────────────── planner ─────────────────────────
 
def _parse_queries(text: str) -> list[str] | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    candidates = [text]
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        candidates.append(m.group())
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            qs = [q.strip() for q in data if isinstance(q, str) and q.strip()]
            if qs:
                return qs[:5]
    return None
 
 
def plan(query: str, llm: LLMConfig, trace_id: str = "") -> Plan:
    now = _nepal_now() if callable(_nepal_now) else _nepal_now
    msg = chat(
        config=llm,
        model=llm.planner_model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT.format(current_datetime=now)},
            {"role": "user", "content": f"Research question: {query}"},
        ],
        trace_id=trace_id,
        label="planner",
    )
    queries = _parse_queries(msg.get("content") or "") or [query]
    return Plan(original_query=query, queries=queries)
 
 
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
 
 
def research(plan_: Plan, llm: LLMConfig, trace_id: str = "",
             max_iterations: int = MAX_ITERATIONS) -> Report:
    system = RESEARCHER_SYSTEM_PROMPT.format(
        original_query=plan_.original_query,
        research_queries="\n".join(f"  - {q}" for q in plan_.queries),
    )
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": plan_.original_query},
    ]
    sources: list[str] = []
    iterations = 0
 
    # Every turn is a normal tool turn. We deliberately do NOT send tool_choice="none" on the
    # last turn: Groq rejects it with a 400 (tool_use_failed) whenever the model still tries
    # a tool call. The final written report comes from synthesize(), which reads the
    # gathered tool results as plain text, so no forced researcher summary is needed.
    for iterations in range(1, max_iterations + 1):
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
 
        # Tool calls in one turn are independent -> run them in parallel
        futures = [_pool.submit(_run_tool, c, trace_id) for c in calls]
        for call, fut in zip(calls, futures):
            name, output, args = fut.result()
            if name == "fetch_url_content" and args.get("url") and args["url"] not in sources:
                sources.append(args["url"])
            messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": output})
 
    report = synthesize(plan_.original_query, messages, llm, trace_id)
    return Report(report=report, sources=sources, iterations=iterations)
 
 
# ───────────────────────── synthesizer ─────────────────────────
 
def _messages_to_text(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role, content = m["role"], m.get("content") or ""
        if role == "user":
            lines.append(f"USER: {content}")
        elif role == "assistant" and content:   # pure tool-call turns have no content
            lines.append(f"RESEARCHER: {content}")
        elif role == "tool":
            lines.append(f"TOOL RESULT [{m.get('name')}]:\n{content}")
    return "\n\n".join(lines)
 
 
def synthesize(original_query: str, messages: list[dict], llm: LLMConfig, trace_id: str = "") -> str:
    human = (
        f"Here is the research gathered:\n\n{_messages_to_text(messages)}\n\n"
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
        approve: Callable[[Plan], Plan] | None = None) -> Report:
    """One-shot: plan -> (optional approve/edit hook) -> research -> synthesize."""
    p = plan(query, llm, trace_id)
    if approve:
        p = approve(p)
    return research(p, llm, trace_id)