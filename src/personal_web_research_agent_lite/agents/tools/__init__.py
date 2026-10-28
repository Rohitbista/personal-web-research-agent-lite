from personal_web_research_agent_lite.agents.tools.web_search_DDG import web_search, SCHEMA as _SEARCH
from personal_web_research_agent_lite.agents.tools.fetch_url import fetch_url_content, SCHEMA as _FETCH

TOOL_SCHEMAS = [_SEARCH, _FETCH]
REGISTRY = {"web_search": web_search, "fetch_url_content": fetch_url_content}