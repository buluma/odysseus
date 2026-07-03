import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_html_code_runner_uses_sandboxed_iframe_without_same_origin():
    # runHTML used to open a same-origin popup (window.open('') + document.write),
    # detaching opener but still executing the code block with the app's own
    # origin -- a malicious/prompt-injected block could reach cookies/localStorage/
    # session on Run. It now renders in a sandboxed iframe with no
    # allow-same-origin, so the code gets an opaque origin instead.
    src = _source("static/js/codeRunner.js")
    match = re.search(
        r"export function runHTML\(code, panel\) \{(?P<body>.*?)\n\}",
        src,
        re.S,
    )

    assert match
    body = match.group("body")
    assert "window.open(" not in body
    assert "iframe.srcdoc = code" in body

    sandbox_match = re.search(r"iframe\.sandbox\s*=\s*'([^']*)'", body)
    assert sandbox_match
    tokens = sandbox_match.group(1).split()
    assert "allow-same-origin" not in tokens
    assert "allow-scripts" in tokens


def test_compare_print_popup_detaches_opener_before_document_write():
    src = _source("static/js/compare/index.js")
    match = re.search(
        r"function _exportPrint\(\) \{(?P<body>.*?)w\.document\.close\(\);",
        src,
        re.S,
    )

    assert match
    body = match.group("body")
    assert "w.opener = null" in body
    assert body.index("w.opener = null") < body.index("w.document.write(html)")
