"""ReDoS regression for the Gemma <|tool_call|> parser in tool_parsing.py.

`_GEMMA_TOOL_CALL_RE` ran a lazy `<\\|tool_call\\|>...(\\{[\\s\\S]*?\\})...<\\|tool_call\\|>`
over untrusted model output via `re.finditer` (parse) and `re.sub` (strip). When
the closing `}`/`<|tool_call|>` is absent, each opener's `[\\s\\S]*?` rescans to
end-of-string and backtracks; a flood of openers is O(n^2) — the same
py/polynomial-redos class the XML/args parsers were already hardened against
(test_redos_xml_tool_parsers.py), but this Gemma path is local-only so the
upstream fix never covered it.

Both sites are now forward-only (open/close paired via _iter_delimited /
_strip_delimited). Tests pin: correctness unchanged for real Gemma markup, and a
"many openers, no closer" flood completes promptly.
"""

import time

import src.agent_tools  # noqa: F401  (break agent_tools<->tool_parsing import cycle)
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks

_BUDGET_S = 4.0


def _timed(fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - start


# ── correctness is preserved ────────────────────────────────────────────────

def test_gemma_json_args_still_parse_and_strip():
    raw = '<|tool_call|>call:web_search{"query":"hello world"}<|tool_call|>'
    blocks = parse_tool_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "web_search"
    assert blocks[0].content == "hello world"
    assert strip_tool_blocks(raw).strip() == ""


def test_gemma_unquoted_args_still_parse():
    raw = '<|tool_call|>call:web_search{query: "hello world"}<|tool_call|>'
    blocks = parse_tool_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "web_search"
    assert blocks[0].content == "hello world"


def test_gemma_dash_tool_name_still_normalized():
    raw = '<|tool_call|>call:read-file{"path":"README.md"}<|tool_call|>'
    blocks = parse_tool_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "read_file"


# ── pathological "many openers, no closer" completes promptly ────────────────

def test_gemma_opener_flood_parse_is_linear():
    # Each opener starts a `{` body that never closes; the old lazy scan rescans
    # to end-of-string from every opener -> O(n^2).
    flood = "<|tool_call|>call:x{" * 40000
    blocks, elapsed = _timed(parse_tool_blocks, flood)
    assert elapsed < _BUDGET_S, f"parse took {elapsed:.2f}s (ReDoS?)"
    assert blocks == []


def test_gemma_opener_flood_strip_is_linear():
    flood = "<|tool_call|>call:x{" * 40000
    _, elapsed = _timed(strip_tool_blocks, flood)
    assert elapsed < _BUDGET_S, f"strip took {elapsed:.2f}s (ReDoS?)"
