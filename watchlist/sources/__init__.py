"""Source adapters for the watchlist scanner.

Adapters are registered at the edge of the feature.  The scanner only knows
how to ask a registered source for a :class:`SourceResult`; source-specific
HTML parsing and health classification stay in the adapter module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


SOURCE_STATUSES = frozenset({"ok", "partial", "empty", "http_error", "parse_error"})


@dataclass
class RawCandidate:
    """A source row before the scanner normalizes it for scoring."""

    source: str
    slug: str
    name: str
    ticker: str | None
    url: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceResult:
    """A source response, including health and detail-fetch accounting."""

    source: str
    status: str
    candidates: list[RawCandidate] = field(default_factory=list)
    error: str | None = None
    fetched_at: str | None = None
    candidate_count: int = 0
    detail_fetch_count: int = 0
    detail_failure_count: int = 0
    detail_skipped_count: int = 0
    detail_failures: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in SOURCE_STATUSES:
            raise ValueError(f"invalid source status: {self.status!r}")
        if not self.candidate_count:
            self.candidate_count = len(self.candidates)


class SourceAdapter(Protocol):
    """The narrow interface the scanner requires from a source."""

    name: str

    def fetch(self) -> SourceResult:
        ...


SOURCE_REGISTRY: dict[str, Any] = {}
# Short alias for callers that prefer the generic registry name.
registry = SOURCE_REGISTRY


def register_source(source: Any, name: str | None = None) -> Any:
    """Register an adapter instance, class, or factory and return it."""

    source_name = name or getattr(source, "name", None)
    if not source_name and isinstance(source, type):
        source_name = getattr(source, "name", None)
    if not isinstance(source_name, str) or not source_name.strip():
        raise ValueError("a source adapter must define a non-empty name")
    SOURCE_REGISTRY[source_name.strip()] = source
    return source


def get_source(name: str, config: Mapping[str, Any] | None = None) -> Any:
    """Return a configured source adapter from the registry.

    Classes and factories are constructed per scan so a run's configuration is
    not leaked into a later run.  Already-created adapter instances are
    returned as-is for lightweight test doubles and user adapters.
    """

    source = SOURCE_REGISTRY.get(name)
    if source is None:
        return None
    if isinstance(source, type):
        try:
            return source(config=config)
        except TypeError:
            return source()
    if hasattr(source, "fetch"):
        return source
    if callable(source):
        try:
            return source(config)
        except TypeError:
            return source()
    return source


def source_names() -> tuple[str, ...]:
    return tuple(SOURCE_REGISTRY)


# Importing the built-in module after the registry definitions avoids a cycle
# and makes ``import watchlist.sources`` sufficient to discover ICODrops.
from watchlist.sources import icodrops as _icodrops  # noqa: E402,F401


__all__ = [
    "SOURCE_STATUSES",
    "RawCandidate",
    "SourceResult",
    "SourceAdapter",
    "SOURCE_REGISTRY",
    "registry",
    "register_source",
    "get_source",
    "source_names",
]
