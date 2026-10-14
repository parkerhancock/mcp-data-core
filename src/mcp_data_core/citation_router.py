"""Domain-free routing of identifier strings to handlers.

A :class:`CitationRouter` holds an ordered set of :class:`Route` rows. Each
row pairs a compiled regular expression with a ``normalize`` callable and a
``target`` label naming whatever should handle the match (a connector, a
tool, a client method). The router knows nothing about any citation system;
consumers register their own rows.

Ordering is explicit: routes are tried by descending ``priority`` and, within
a priority, in registration order. Patterns are applied with
``re.Pattern.search``, so anchoring (``^...$``, ``\\b``) is the pattern's
responsibility. Exceptions raised by ``normalize`` propagate unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from .exceptions import ValidationError


@dataclass(frozen=True)
class Route:
    """One routing row.

    Attributes:
        name: Unique, human-readable name for the row.
        pattern: Compiled regex applied with ``search``.
        normalize: Maps the regex match to the normalized value.
        target: Label for the handler that should receive the value.
        priority: Higher values are tried first.
        requires_prefix: If set, the stripped text must start with this
            prefix (case-insensitive) before the pattern is tried.
        gate: If set, called with the raw text; the route is skipped when it
            returns false.
    """

    name: str
    pattern: re.Pattern[str]
    normalize: Callable[[re.Match[str]], Any]
    target: str
    priority: int = 0
    requires_prefix: str | None = None
    gate: Callable[[str], bool] | None = None

    def match(self, text: str) -> re.Match[str] | None:
        """Return the pattern match for ``text`` if every precondition holds."""
        if self.requires_prefix is not None and not text.strip().lower().startswith(
            self.requires_prefix.lower()
        ):
            return None
        if self.gate is not None and not self.gate(text):
            return None
        return self.pattern.search(text)


class RouteMatch(NamedTuple):
    """A route that matched, with its normalized value."""

    route: Route
    value: Any


class NoRouteError(ValidationError):
    """No registered route matched the input."""

    def __init__(self, text: str) -> None:
        super().__init__(f"No route matches {text!r}")
        self.text = text


class AmbiguousCitationError(ValidationError):
    """More than one route matched at the highest matching priority."""

    def __init__(self, text: str, candidates: list[RouteMatch]) -> None:
        names = ", ".join(f"{c.route.name}->{c.route.target}" for c in candidates)
        super().__init__(f"Ambiguous input {text!r}; candidates: {names}")
        self.text = text
        self.candidates = candidates


class CitationRouter:
    """Ordered collection of :class:`Route` rows."""

    def __init__(self, routes: list[Route] | None = None) -> None:
        self._routes: list[Route] = []
        for route in routes or []:
            self.register(route)

    def register(self, route: Route) -> Route:
        """Add a route. Names must be unique. Returns the route."""
        if any(r.name == route.name for r in self._routes):
            raise ValueError(f"Route {route.name!r} is already registered")
        self._routes.append(route)
        return route

    @property
    def routes(self) -> tuple[Route, ...]:
        """Routes in evaluation order (priority desc, then registration)."""
        indexed = sorted(enumerate(self._routes), key=lambda pair: (-pair[1].priority, pair[0]))
        return tuple(route for _, route in indexed)

    def classify(self, text: str) -> list[RouteMatch]:
        """Return every matching route with its normalized value, in evaluation order."""
        results: list[RouteMatch] = []
        for route in self.routes:
            m = route.match(text)
            if m is not None:
                results.append(RouteMatch(route, route.normalize(m)))
        return results

    def route(
        self,
        text: str,
        *,
        on_ambiguous: Literal["error", "first"] = "error",
    ) -> RouteMatch:
        """Return the single best match for ``text``.

        Only matches at the highest matching priority compete. With
        ``on_ambiguous="error"`` more than one such match raises
        :class:`AmbiguousCitationError`; with ``"first"`` the earliest
        registered one wins.

        Raises:
            NoRouteError: Nothing matched.
            AmbiguousCitationError: Tie at the top priority and
                ``on_ambiguous="error"``.
        """
        if on_ambiguous not in ("error", "first"):
            raise ValueError(f"on_ambiguous must be 'error' or 'first', got {on_ambiguous!r}")
        matches = self.classify(text)
        if not matches:
            raise NoRouteError(text)
        top = matches[0].route.priority
        tied = [m for m in matches if m.route.priority == top]
        if len(tied) > 1 and on_ambiguous == "error":
            raise AmbiguousCitationError(text, tied)
        return tied[0]


__all__ = [
    "AmbiguousCitationError",
    "CitationRouter",
    "NoRouteError",
    "Route",
    "RouteMatch",
]
