import time
from ddgs import DDGS

_CTX = "src/agents/tools/web_search_DDG"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Searches the live internet using DuckDuckGo. Use this whenever the user asks "
            "about current events, real-time data, or topics you do not have native "
            "knowledge about."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search engine text string to query."},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of snippet results to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
}


def web_search(query: str, max_results: int = 5) -> str:
    try:
        max_results = max(1, min(int(max_results), 10))
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))

        if not raw:
            return "No search results found for this query."

        return "\n\n".join(
            f"Title: {r.get('title')}\nLink: {r.get('href')}\nSnippet: {r.get('body')}\n---"
            for r in raw
        )
    except Exception as e:
        return f"An error occurred during the search: {e}"
