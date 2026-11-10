"""Tests for shell_tokens: the rules the shared shell readers ask."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import shell_tokens  # noqa: E402
from shell_tokens import (  # noqa: E402
    _LITERAL_DOLLAR,
    _LITERAL_TILDE,
    _assignment_value,
    _binding_text,
    _command_tokens,
)


@pytest.mark.parametrize(
    ("text", "binding"),
    [
        ('F="$HOME"x', "F=${HOME}x"),
        ("F=$HOME'x'", "F=${HOME}x"),
        ("F=$HOME\\x", "F=${HOME}x"),
        ('F="$HOME/x"', "F=$HOME/x"),
        ("F='$HOME'", f"F={_LITERAL_DOLLAR}HOME"),
        ("F=\\$HOME", f"F={_LITERAL_DOLLAR}HOME"),
        ('F="~/x"', f"F={_LITERAL_TILDE}/x"),
        ("F=~/x", "F=~/x"),
    ],
)
def test_a_word_keeps_what_its_quotes_said_for_a_binding(text: str, binding: str) -> None:
    # the quotes end a name or keep a `$` literal; the word's text
    # (quotes removed) stays what every other reader reads.
    [word] = _command_tokens(text)
    assert word == text.replace('"', "").replace("'", "").replace("\\", "")
    assert _binding_text(word) == binding


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('"$HOME"x', "${HOME}x"),
        ("\\$HOME/x", f"{_LITERAL_DOLLAR}HOME/x"),
        ("a b", None),
        ("'a", None),
    ],
)
def test_an_assignment_value_is_read_as_one_word_binds(text: str, value: str | None) -> None:
    # A binding's value is read as one word.
    assert _assignment_value(text) == value


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("cd x && # build\nmake", ["cd", "x", "&&", "make"]),
        ("cd x && \nmake", ["cd", "x", "&&", "make"]),
        ("a | \nb", ["a", "|", "b"]),
        ("a ||\t\nb", ["a", "||", "b"]),
        ("a &&\nb", ["a", "&&", "b"]),
        ("a\nb", ["a", ";", "b"]),
        ("(a)\nb", ["(", "a", ")", ";", "b"]),
        # a run that starts with the newline ends the word's command
        ("a\n(b)", ["a", ";", "(", "b", ")"]),
        ("a\n> f", ["a", ";", ">", "f"]),
    ],
)
def test_a_newline_after_a_list_operator_continues_the_list(text: str, tokens: list[str]) -> None:
    # A space or a comment between the operator and the newline keeps the newline in the
    # operator's run.
    assert shell_tokens._shell_tokens(text) == tokens
    assert _command_tokens(text) == tokens


def test_a_newline_after_a_process_substitution_opens_its_body() -> None:
    assert shell_tokens._shell_tokens("diff <( \n git show x) y") == [
        "diff",
        "<(",
        "git",
        "show",
        "x",
        ")",
        "y",
    ]


def test_quoted_dollars_are_marked_in_linear_time(assert_linear) -> None:
    assert_linear(shell_tokens, _command_tokens, lambda size: "F=\"$HOME\"x'$A'\\$B " * size, 4000)
