"""Sentence splitting across arbitrary streaming boundaries."""

import pytest
from familiar.pipeline.sentences import SentenceSplitter


@pytest.mark.parametrize("punctuation", [".", "!", "?", "…", "。", "！", "？", "\n"])
def test_terminators(punctuation):
    splitter = SentenceSplitter()
    expected = f"Hello{punctuation}".strip()
    assert splitter.feed(f"Hello{punctuation} Next") == [expected]
    assert splitter.flush() == "Next"
    assert splitter.flush() is None


def test_decimals_abbreviations():
    splitter = SentenceSplitter()
    text = "Mr. Smith met Dr. Jones, e.g. at 3.5 vs. 2.0. Next"
    assert splitter.feed(text) == [text.removesuffix(" Next")]


def test_japanese_without_spaces():
    splitter = SentenceSplitter()
    assert splitter.feed("こんにちは。元気？はい！") == ["こんにちは。", "元気？", "はい！"]


def test_stream_boundaries():
    splitter = SentenceSplitter()
    assert splitter.feed("Dr.") == []
    assert splitter.feed(" Smith has 3.") == []
    assert splitter.feed("5 cats. Ne") == ["Dr. Smith has 3.5 cats."]
    assert splitter.feed("xt.") == []
    assert splitter.flush() == "Next."


def test_max_chars():
    splitter = SentenceSplitter(max_chars=12)
    assert splitter.feed("one two three four") == ["one two"]
    assert splitter.flush() == "three four"
    assert SentenceSplitter(4).feed("abcdef") == ["abcd"]


def test_empty_flush():
    assert SentenceSplitter().flush() is None
