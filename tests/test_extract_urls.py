"""extract_urls must keep a *balanced* trailing ')' while still trimming
prose-glued punctuation.

The old cleanup stripped any trailing ')' unconditionally, which corrupted URLs
that legitimately end in one (Wikipedia disambiguation links being the common
case). The fix only drops an *unbalanced* ')'.
"""
from src.chat_helpers import extract_urls


def test_keeps_balanced_trailing_paren():
    text = "see https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert extract_urls(text) == [
        "https://en.wikipedia.org/wiki/Python_(programming_language)"
    ]


def test_strips_unbalanced_trailing_paren_from_prose():
    # The closing paren belongs to the sentence, not the URL.
    assert extract_urls("(see https://example.com)") == ["https://example.com"]


def test_strips_trailing_sentence_punctuation():
    assert extract_urls("go to https://example.com.") == ["https://example.com"]
    assert extract_urls("https://example.com, then continue") == [
        "https://example.com"
    ]


def test_strips_trailing_punctuation_after_balanced_close():
    # Keep the balanced ')' but drop the sentence period after it.
    text = "ref https://en.wikipedia.org/wiki/Foo_(bar)."
    assert extract_urls(text) == ["https://en.wikipedia.org/wiki/Foo_(bar)"]


def test_nested_balanced_parens_preserved():
    text = "https://example.com/a_(b_(c))"
    assert extract_urls(text) == ["https://example.com/a_(b_(c))"]


def test_alternating_trailing_parens_and_punctuation():
    # Each unbalanced ')' is dropped together with the punctuation before it.
    assert extract_urls("(see https://example.com/x).)") == ["https://example.com/x"]


def test_long_trailing_punctuation_run_is_linear():
    # A chat message is user input. The trailing-punctuation trim used to
    # rescan the run from every start offset, so a long '!' run that does not
    # reach the end of the URL took seconds (20k chars: ~2.6s).
    import time

    text = "see https://example.com/" + "!" * 50_000 + "x"
    start = time.perf_counter()
    assert extract_urls(text) == ["https://example.com/" + "!" * 50_000 + "x"]
    assert time.perf_counter() - start < 1.0


def test_long_unbalanced_paren_run_is_linear():
    # Each dropped ')' used to recount the whole URL (80k chars: ~52s).
    import time

    text = "see https://example.com/" + ")" * 50_000
    start = time.perf_counter()
    assert extract_urls(text) == ["https://example.com/"]
    assert time.perf_counter() - start < 1.0
