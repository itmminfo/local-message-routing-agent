"""Ordered persisted evidence requires actual lexical source occurrences."""

import json
from typing import Any

import pytest
from app.agent.issue_boundary import grounded_issue_quotes, grounded_ordered_issue_quotes
from app.infrastructure.ollama_client import OllamaChatError


def response(*quotes: str) -> dict[str, Any]:
    return {"message": {"content": json.dumps({"issue_quotes": quotes}, ensure_ascii=False)}}


def test_inside_word_assignment_is_rejected_even_when_each_quote_exists_elsewhere() -> None:
    original = "foo bar foobar"
    supplied = response("foo bar", "foo", "bar")
    assert grounded_issue_quotes(supplied, original) == ["foo bar", "foo", "bar"]
    with pytest.raises(OllamaChatError, match="lexical source assignment"):
        grounded_ordered_issue_quotes(supplied, original)


def test_illegal_earlier_occurrence_is_skipped_for_later_legal_source_order() -> None:
    # Starting with the substring in foobar would make "marker" unreachable.
    # Skipping to the legal foo makes the later exact assignment succeed.
    original = "foobar marker foo bar"
    supplied = response("foo", "bar")
    assert grounded_ordered_issue_quotes(supplied, original) == ["foo", "bar"]


def test_skipping_illegal_occurrences_does_not_skip_required_quote_order() -> None:
    original = "foobar marker foo bar"
    with pytest.raises(OllamaChatError, match="lexical source assignment"):
        grounded_ordered_issue_quotes(response("foo", "marker"), original)


@pytest.mark.parametrize(
    ("original", "quotes"),
    [
        ("foobarbaz", ["foo", "bar"]),
        ("foobar marker", ["foo", "marker"]),
        ("marker foobar", ["marker", "bar"]),
        ("zażółć", ["żółć"]),
        ("_foo foo2", ["foo"]),
        ("First. Second.", ["Second.", "First."]),
    ],
)
def test_nonlexical_or_out_of_order_evidence_is_rejected(original: str, quotes: list[str]) -> None:
    with pytest.raises(OllamaChatError):
        grounded_ordered_issue_quotes(response(*quotes), original)


@pytest.mark.parametrize(
    ("original", "quotes"),
    [
        ("foobar foo bar", ["foo", "bar"]),
        ("zażółć żółć", ["żółć"]),
        ("xHTTP HTTP 403", ["HTTP", "403"]),
        ("foo-bar", ["foo", "-", "bar"]),
        ("Foo.\r\n\tŻółć?", ["Foo.", "Żółć?"]),
        ("  Żółć\r\nHTTP 403.  ", ["  Żółć\r\nHTTP 403.  "]),
        ("? !", ["?", "!"]),
    ],
)
def test_legal_unicode_punctuation_and_whitespace_stay_literal(
    original: str, quotes: list[str]
) -> None:
    assert grounded_ordered_issue_quotes(response(*quotes), original) == quotes


def test_persisted_order_is_not_replaced_with_legacy_canonical_order() -> None:
    original = "OK. Ignore. Problem. OK."
    supplied = response("Problem.", "OK.")
    assert grounded_issue_quotes(supplied, original) == ["OK.", "Problem."]
    assert grounded_ordered_issue_quotes(supplied, original) == ["Problem.", "OK."]


def test_earliest_legal_assignment_preserves_later_required_quotes() -> None:
    original = "foobar foo bar foo marker"
    assert grounded_ordered_issue_quotes(response("foo", "bar", "marker"), original) == [
        "foo",
        "bar",
        "marker",
    ]


def test_legacy_duplicate_and_overlap_rejections_are_retained() -> None:
    for supplied in [response("OK.", "OK."), response("First issue.", "issue.")]:
        with pytest.raises(OllamaChatError):
            grounded_ordered_issue_quotes(supplied, "OK. OK. First issue.")


def test_legacy_grounder_still_accepts_literal_inside_word_for_legacy_callers() -> None:
    # Only the ordered persisted-evidence helper strengthens actual boundaries.
    assert grounded_issue_quotes(response("ółć"), "Żółć") == ["ółć"]
    with pytest.raises(OllamaChatError):
        grounded_ordered_issue_quotes(response("ółć"), "Żółć")
