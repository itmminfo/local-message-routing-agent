import itertools
import json
import subprocess
import sys
from collections import Counter

from app.agent.issue_boundary import grounded_issue_quotes
from app.infrastructure.ollama_client import OllamaChatError


def brute_force_assignment_exists(message: str, quotes: tuple[str, ...]) -> bool:
    """Independent oracle: enumerate every actual interval, then all combinations."""
    occurrences = [
        [
            (start, start + len(quote))
            for start in range(len(message) - len(quote) + 1)
            if message[start : start + len(quote)] == quote
        ]
        for quote in quotes
    ]
    for assignment in itertools.product(*occurrences):
        ordered = sorted(assignment)
        if all(left[1] <= right[0] for left, right in itertools.pairwise(ordered)):
            return True
    return False


def test_small_quote_assignments_match_exhaustive_interval_oracle() -> None:
    quote_pool = ("a", "b", "aa", "ab", "ba", "bb", "c")
    checked = 0
    for size in range(5):
        for letters in itertools.product("ab", repeat=size):
            message = "".join(letters)
            for quote_count in range(1, 4):
                for quotes in itertools.combinations(quote_pool, quote_count):
                    expected = brute_force_assignment_exists(message, quotes)
                    canonical_selected: list[str] | None = None
                    for supplied in (quotes, tuple(reversed(quotes))):
                        checked += 1
                        response = {"message": {"content": json.dumps({"issue_quotes": supplied})}}
                        try:
                            selected = grounded_issue_quotes(response, message)
                        except OllamaChatError:
                            assert not expected, (message, supplied)
                        else:
                            assert expected, (message, supplied, selected)
                            assert Counter(selected) == Counter(quotes)
                            if canonical_selected is None:
                                canonical_selected = selected
                            else:
                                assert selected == canonical_selected, (message, quotes)
                            # Source order must itself admit a disjoint placement.
                            end = 0
                            for quote in selected:
                                start = message.find(quote, end)
                                assert start >= end, (message, supplied, selected)
                                end = start + len(quote)
    assert checked == 3906


def test_5000_character_repetition_has_bounded_assignment_search() -> None:
    # Every quote has thousands of occurrences, but their total length cannot
    # fit. Enumerating positions/permutations would explode. A subprocess bounds
    # this regression without leaving a stuck worker in the pytest process.
    probe = """
import json, sys
from app.agent.issue_boundary import grounded_issue_quotes
from app.infrastructure.ollama_client import OllamaChatError
quotes = json.loads(sys.stdin.read())
try:
    grounded_issue_quotes({'message': {'content': json.dumps({'issue_quotes': quotes})}}, 'a'*5000)
except OllamaChatError:
    print('correctly refused')
else:
    raise AssertionError('Overlapping quotes exceeded the available source length')
"""
    quotes = ["a" * length for length in range(625, 633)]
    assert sum(map(len, quotes)) > 5000
    result = subprocess.run(
        [sys.executable, "-c", probe],
        input=json.dumps(quotes),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert result.stdout.strip() == "correctly refused"


def test_many_occurrences_can_still_produce_exact_source_quotes() -> None:
    quotes = ["a" * length for length in range(1, 9)]
    selected = grounded_issue_quotes(
        {"message": {"content": json.dumps({"issue_quotes": quotes})}}, "a" * 5000
    )
    assert Counter(selected) == Counter(quotes)
