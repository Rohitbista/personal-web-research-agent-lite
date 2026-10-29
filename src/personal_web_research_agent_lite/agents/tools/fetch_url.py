import re
import httpx
from bs4 import BeautifulSoup

_MAX_BYTES = 1_000_000  # never hold more than ~1 MB of a page in RAM
_client = httpx.Client(
    headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
    timeout=10,
    follow_redirects=True,
)

SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_url_content",
        "description": (
            "Fetches the full readable text from a webpage URL. Use this after web_search "
            "to get complete information from a promising page -- snippets are often too short."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch."},
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 4000).",
                },
            },
            "required": ["url"],
        },
    },
}


def fetch_url_content(url: str, max_chars: int = 4000) -> str:
    max_chars = max(500, min(int(max_chars), 8000))
    try:
        with _client.stream("GET", url) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if ctype and not any(t in ctype for t in ("html", "text", "xml")):
                return f"Error fetching {url}: unsupported content type '{ctype}'"

            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) >= _MAX_BYTES:
                    break
            _, encoding = resp.status_code, resp.encoding or "utf-8"

        html = bytes(buf).decode(encoding, errors="replace")
        del buf

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

        truncated = text[:max_chars]
        suffix = "..." if len(text) > max_chars else ""
        return f"[Source: {url}]\n\n{truncated}{suffix}"

    except Exception as exc:
        return f"Error fetching {url}: {exc}"
