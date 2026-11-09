"""Tests for the record rules in transcripts.py that other scripts share."""

from datetime import datetime
from types import ModuleType


def test_paste_markers_are_removed_from_the_typed_words(transcripts: ModuleType) -> None:
    words = transcripts.typed_words(
        'See this. <pasted_content id="ab12">Use tabs.</pasted_content id="ab12"> Fix it.'
    )
    assert words == "See this. Use tabs. Fix it."


def test_days_with_until_counts_whole_days_ending_on_that_date(transcripts: ModuleType) -> None:
    window = transcripts.make_window(2, None, "2099-06-02")
    assert window.start == datetime.fromisoformat("2099-06-01").timestamp()
    assert window.end == datetime.fromisoformat("2099-06-03").timestamp()
    assert window.label == "2 days through 2099-06-02"
