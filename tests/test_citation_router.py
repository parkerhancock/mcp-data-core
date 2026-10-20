"""Tests for the domain-free citation router."""

from __future__ import annotations

import dataclasses
import re

import pytest

import mcp_data_core
from mcp_data_core import (
    AmbiguousCitationError,
    CitationRouter,
    McpDataCoreError,
    NoRouteError,
    Route,
    RouteMatch,
    ValidationError,
)


def _group(n: int = 0):
    return lambda m: m.group(n)


DIGITS = Route("digits", re.compile(r"^\d+$"), _group(), "numbers")
WORD = Route("word", re.compile(r"^[a-z]+$"), _group(), "words")
ANY_DIGIT = Route("any-digit", re.compile(r"\d"), _group(), "loose")


class TestRoute:
    def test_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            DIGITS.name = "x"  # type: ignore[misc]

    def test_defaults(self):
        assert DIGITS.priority == 0
        assert DIGITS.requires_prefix is None
        assert DIGITS.gate is None

    def test_match_uses_search(self):
        r = Route("unanchored", re.compile(r"\d+"), _group(), "t")
        assert r.match("abc 123 def").group(0) == "123"

    def test_requires_prefix_case_insensitive_and_stripped(self):
        r = Route("p", re.compile(r"\d+"), _group(), "t", requires_prefix="US")
        assert r.match("  us123") is not None
        assert r.match("EP123") is None

    def test_gate(self):
        r = Route("g", re.compile(r"\d+"), _group(), "t", gate=lambda s: len(s) > 3)
        assert r.match("12") is None
        assert r.match("1234") is not None

    def test_gate_not_called_when_prefix_fails(self):
        calls = []
        r = Route(
            "g",
            re.compile(r"\d+"),
            _group(),
            "t",
            requires_prefix="X",
            gate=lambda s: calls.append(s) or True,
        )
        assert r.match("123") is None
        assert calls == []


class TestRegister:
    def test_register_returns_route(self):
        router = CitationRouter()
        assert router.register(DIGITS) is DIGITS

    def test_duplicate_name_rejected(self):
        router = CitationRouter([DIGITS])
        with pytest.raises(ValueError, match="already registered"):
            router.register(dataclasses.replace(DIGITS, target="other"))

    def test_constructor_registers(self):
        router = CitationRouter([DIGITS, WORD])
        assert router.routes == (DIGITS, WORD)


class TestOrdering:
    def test_registration_order_within_priority(self):
        a = Route("a", re.compile(r"x"), _group(), "ta")
        b = Route("b", re.compile(r"x"), _group(), "tb")
        c = Route("c", re.compile(r"x"), _group(), "tc")
        router = CitationRouter([b, c, a])
        assert [m.route.name for m in router.classify("x")] == ["b", "c", "a"]

    def test_priority_desc_then_registration(self):
        low = Route("low", re.compile(r"x"), _group(), "t", priority=-1)
        mid1 = Route("mid1", re.compile(r"x"), _group(), "t")
        high = Route("high", re.compile(r"x"), _group(), "t", priority=5)
        mid2 = Route("mid2", re.compile(r"x"), _group(), "t")
        router = CitationRouter([low, mid1, high, mid2])
        assert [r.name for r in router.routes] == ["high", "mid1", "mid2", "low"]
        assert [m.route.name for m in router.classify("x")] == ["high", "mid1", "mid2", "low"]

    def test_routes_is_snapshot(self):
        router = CitationRouter([DIGITS])
        snap = router.routes
        router.register(WORD)
        assert snap == (DIGITS,)


class TestClassify:
    def test_empty_router(self):
        assert CitationRouter().classify("anything") == []

    def test_returns_all_matches_with_values(self):
        router = CitationRouter([DIGITS, WORD, ANY_DIGIT])
        result = router.classify("123")
        assert result == [RouteMatch(DIGITS, "123"), RouteMatch(ANY_DIGIT, "1")]
        route, value = result[0]
        assert route is DIGITS and value == "123"

    def test_no_match(self):
        assert CitationRouter([DIGITS]).classify("abc") == []

    def test_normalize_receives_match(self):
        seen = []

        def norm(m):
            seen.append(m)
            return {"vol": int(m["vol"]), "page": int(m["page"])}

        r = Route("vp", re.compile(r"(?P<vol>\d+) U\.S\. (?P<page>\d+)"), norm, "cases")
        assert CitationRouter([r]).classify("see 410 U.S. 113 (1973)") == [
            RouteMatch(r, {"vol": 410, "page": 113})
        ]
        assert isinstance(seen[0], re.Match)

    def test_normalize_may_return_none(self):
        r = Route("n", re.compile(r"x"), lambda m: None, "t")
        assert CitationRouter([r]).classify("x") == [RouteMatch(r, None)]

    def test_normalize_exception_propagates(self):
        class Boom(Exception):
            pass

        def norm(m):
            raise Boom("bad")

        r = Route("boom", re.compile(r"x"), norm, "t")
        with pytest.raises(Boom, match="bad"):
            CitationRouter([r]).classify("x")
        with pytest.raises(Boom):
            CitationRouter([r]).route("x")

    def test_anchoring_is_left_to_patterns(self):
        anchored = Route("anchored", re.compile(r"^\d+$"), _group(), "a")
        loose = Route("loose", re.compile(r"\d+"), _group(), "l")
        router = CitationRouter([anchored, loose])
        assert [m.route.name for m in router.classify("123")] == ["anchored", "loose"]
        assert [m.route.name for m in router.classify("no. 123 here")] == ["loose"]

    def test_pattern_flags_respected(self):
        r = Route("ci", re.compile(r"^usc$", re.IGNORECASE), _group(), "t")
        assert len(CitationRouter([r]).classify("USC")) == 1


class TestRouteMethod:
    def test_single_match(self):
        router = CitationRouter([DIGITS, WORD])
        assert router.route("abc") == RouteMatch(WORD, "abc")

    def test_no_route_error(self):
        router = CitationRouter([DIGITS])
        with pytest.raises(NoRouteError) as exc:
            router.route("abc")
        assert exc.value.text == "abc"
        assert "abc" in str(exc.value)

    def test_ambiguous_error_lists_candidates(self):
        router = CitationRouter([DIGITS, ANY_DIGIT])
        with pytest.raises(AmbiguousCitationError) as exc:
            router.route("42")
        err = exc.value
        assert err.text == "42"
        assert [c.route.name for c in err.candidates] == ["digits", "any-digit"]
        assert [c.value for c in err.candidates] == ["42", "4"]
        assert "digits->numbers" in str(err)
        assert "any-digit->loose" in str(err)

    def test_ambiguous_first(self):
        router = CitationRouter([DIGITS, ANY_DIGIT])
        assert router.route("42", on_ambiguous="first") == RouteMatch(DIGITS, "42")

    def test_higher_priority_is_not_ambiguous(self):
        loose = dataclasses.replace(ANY_DIGIT, priority=-1)
        router = CitationRouter([loose, DIGITS])
        assert router.route("42") == RouteMatch(DIGITS, "42")

    def test_tie_only_at_top_priority(self):
        a = Route("a", re.compile(r"x"), _group(), "ta", priority=1)
        b = Route("b", re.compile(r"x"), _group(), "tb", priority=1)
        c = Route("c", re.compile(r"x"), _group(), "tc")
        router = CitationRouter([c, a, b])
        with pytest.raises(AmbiguousCitationError) as exc:
            router.route("x")
        assert [m.route.name for m in exc.value.candidates] == ["a", "b"]
        assert router.route("x", on_ambiguous="first").route is a

    def test_invalid_on_ambiguous(self):
        with pytest.raises(ValueError, match="on_ambiguous"):
            CitationRouter([DIGITS]).route("1", on_ambiguous="last")  # type: ignore[arg-type]

    def test_gated_route_excluded_from_ambiguity(self):
        gated = dataclasses.replace(ANY_DIGIT, gate=lambda s: False)
        router = CitationRouter([DIGITS, gated])
        assert router.route("42").route is DIGITS


class TestExceptionHierarchy:
    @pytest.mark.parametrize("cls", [NoRouteError, AmbiguousCitationError])
    def test_subclasses(self, cls):
        assert issubclass(cls, ValidationError)
        assert issubclass(cls, McpDataCoreError)
        assert issubclass(cls, ValueError)


def test_exported_from_package():
    for name in ("AmbiguousCitationError", "CitationRouter", "NoRouteError", "Route", "RouteMatch"):
        assert name in mcp_data_core.__all__
        assert hasattr(mcp_data_core, name)
