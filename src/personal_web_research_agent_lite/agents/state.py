from dataclasses import dataclass, field


@dataclass(slots=True)
class Plan:
    original_query: str
    queries: list[str]
    title: str = ""  # short display name from the planner ("" -> the service falls back to the query)


@dataclass(slots=True)
class Source:
    """A page that was actually read. Its 1-based position in Report.sources is the [n]
    citation number used inside the report text."""
    url: str
    title: str | None = None
    snippet: str | None = None


@dataclass(slots=True)
class Report:
    report: str
    sources: list[Source] = field(default_factory=list)
    iterations: int = 0