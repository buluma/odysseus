import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(source):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_bulk_verbs_match_the_per_card_buttons():
    verbs = _node_eval(
        """
        const { BULK_VERBS } = await import('./static/js/eventsBulk.js');
        console.log(JSON.stringify(BULK_VERBS));
        """
    )
    assert [(v["verb"], v["label"]) for v in verbs] == [
        ("ack", "Ack"),
        ("investigate", "Work"),
        ("resolve", "Done"),
        ("ignore", "Ignore"),
    ]


def test_run_bulk_verb_applies_the_verb_to_every_id_and_reports_partial_failure():
    out = _node_eval(
        """
        const { runBulkVerb } = await import('./static/js/eventsBulk.js');
        const calls = [];
        const action = async (id, verb) => {
          calls.push([id, verb]);
          if (id === 'b') throw new Error('HTTP 409');
          return { id, status: 'acknowledged' };
        };
        const res = await runBulkVerb(['a', 'b', 'c'], 'ack', action);
        console.log(JSON.stringify({ calls, res }));
        """
    )
    assert out["calls"] == [["a", "ack"], ["b", "ack"], ["c", "ack"]]
    assert out["res"]["succeeded"] == ["a", "c"]
    assert out["res"]["failed"] == ["b"]
    assert [e["id"] for e in out["res"]["updated"]] == ["a", "c"]


def test_run_bulk_verb_with_no_ids_does_nothing():
    out = _node_eval(
        """
        const { runBulkVerb } = await import('./static/js/eventsBulk.js');
        let called = false;
        const res = await runBulkVerb([], 'ack', async () => { called = true; });
        console.log(JSON.stringify({ called, res }));
        """
    )
    assert out["called"] is False
    assert out["res"] == {"updated": [], "succeeded": [], "failed": []}
