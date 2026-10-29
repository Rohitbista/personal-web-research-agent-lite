from dataclasses import dataclass, field


@dataclass(slots=True)
class Plan:
    original_query: str
    queries: list[str]


@dataclass(slots=True)
class Report:
    report: str
    sources: list[str] = field(default_factory=list)
    iterations: int = 0
