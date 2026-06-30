"""Workspace confinement: file tools are hard-bounded to the workspace folder
(layered on upstream's sensitive-path policy); bash runs with cwd there."""
import os
import tempfile

import pytest

from src.tool_execution import _resolve_tool_path_in_workspace, _direct_fallback


def test_workspace_resolver_confines():
    ws = tempfile.mkdtemp()
    open(os.path.join(ws, "a.txt"), "w").write("x")
    real = os.path.realpath(os.path.join(ws, "a.txt"))
    # relative path resolves under the workspace
    assert _resolve_tool_path_in_workspace(ws, "a.txt") == real
    # absolute path inside the workspace is allowed
    assert _resolve_tool_path_in_workspace(ws, os.path.join(ws, "a.txt")) == real
    # absolute path outside is rejected (sibling temp dir, portable across OSes)
    outside = tempfile.mkdtemp()
    with pytest.raises(ValueError):
        _resolve_tool_path_in_workspace(ws, os.path.join(outside, "x.txt"))
    # parent-escape is rejected
    with pytest.raises(ValueError):
        _resolve_tool_path_in_workspace(ws, os.path.join("..", "..", "escape.txt"))


def test_workspace_resolver_blocks_sensitive():
    """Upstream's sensitive-file deny list still applies inside the workspace."""
    ws = tempfile.mkdtemp()
    os.makedirs(os.path.join(ws, ".ssh"), exist_ok=True)
    with pytest.raises(ValueError):
        _resolve_tool_path_in_workspace(ws, ".ssh/authorized_keys")


@pytest.mark.asyncio
async def test_read_write_confined_in_workspace():
    ws = tempfile.mkdtemp()
    # Write inside the workspace (relative path) succeeds.
    res = await _direct_fallback("write_file", "note.txt\nhello", workspace=ws)
    assert res["exit_code"] == 0
    assert os.path.isfile(os.path.join(ws, "note.txt"))
    # Read it back.
    res = await _direct_fallback("read_file", "note.txt", workspace=ws)
    assert res["exit_code"] == 0 and res["output"] == "hello"
    # Reading outside the workspace is rejected (sibling temp dir, portable).
    outside = tempfile.mkdtemp()
    outside_file = os.path.join(outside, "secret.txt")
    open(outside_file, "w").write("nope")
    res = await _direct_fallback("read_file", outside_file, workspace=ws)
    assert res["exit_code"] == 1 and "outside the workspace" in res["error"]
    # Writing outside is rejected (file must not be created).
    escape = os.path.join(outside, "_ws_escape.txt")
    res = await _direct_fallback("write_file", f"{escape}\nx", workspace=ws)
    assert res["exit_code"] == 1 and "outside the workspace" in res["error"]
    assert not os.path.exists(escape)


@pytest.mark.asyncio
async def test_subprocess_runs_with_workspace_cwd():
    """bash/python subprocesses run with cwd set to the workspace. Use the
    python tool for an OS-agnostic cwd probe (Windows cmd has no `pwd`)."""
    ws = tempfile.mkdtemp()
    res = await _direct_fallback("python", "import os; print(os.getcwd())", workspace=ws)
    assert res["exit_code"] == 0
    assert os.path.realpath(res["output"].strip()) == os.path.realpath(ws)


# --- Tools that landed after this PR, now wired into the workspace -----------

@pytest.mark.asyncio
async def test_edit_file_confined_in_workspace():
    import json
    ws = tempfile.mkdtemp()
    open(os.path.join(ws, "f.txt"), "w").write("foo bar")
    # Edit inside the workspace succeeds.
    res = await _direct_fallback("edit_file", json.dumps(
        {"path": "f.txt", "old_string": "foo", "new_string": "baz"}), workspace=ws)
    assert res["exit_code"] == 0
    assert open(os.path.join(ws, "f.txt")).read() == "baz bar"
    # Editing outside the workspace is rejected (sibling temp dir, portable).
    outside = tempfile.mkdtemp()
    outside_file = os.path.join(outside, "f.txt")
    open(outside_file, "w").write("a")
    res = await _direct_fallback("edit_file", json.dumps(
        {"path": outside_file, "old_string": "a", "new_string": "b"}), workspace=ws)
    assert res["exit_code"] == 1 and "outside the workspace" in res["error"]


@pytest.mark.asyncio
async def test_grep_and_ls_confined_in_workspace():
    import json
    ws = tempfile.mkdtemp()
    open(os.path.join(ws, "doc.txt"), "w").write("hello workspace\n")
    # grep with no path searches the workspace root and finds the match.
    res = await _direct_fallback("grep", json.dumps({"pattern": "hello"}), workspace=ws)
    assert res["exit_code"] == 0 and "doc.txt" in res["output"]
    # grep pointed outside the workspace is rejected (sibling temp dir, portable).
    outside = tempfile.mkdtemp()
    res = await _direct_fallback("grep", json.dumps({"pattern": "x", "path": outside}), workspace=ws)
    assert res["exit_code"] == 1 and "outside the workspace" in res["error"]
    # ls of the workspace lists its files; ls outside is rejected.
    res = await _direct_fallback("ls", "", workspace=ws)
    assert res["exit_code"] == 0 and "doc.txt" in res["output"]
    res = await _direct_fallback("ls", outside, workspace=ws)
    assert res["exit_code"] == 1 and "outside the workspace" in res["error"]


@pytest.mark.asyncio
async def test_glob_confined_e2e(ws, admin):
    """glob's literal fast-path must stay inside the workspace. A pattern with
    ../ or an absolute path outside the root would otherwise leak the existence
    and full path of arbitrary host files (an oracle), even though read_file
    blocks reading them."""
    with open(os.path.join(ws, "found.py"), "w") as f:
        f.write("x")
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": "found.py"})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "found.py" in r["output"]

    # a secret outside the workspace must not be discoverable via glob
    outside = tempfile.mkdtemp()
    secret = os.path.join(outside, "secret.txt")
    with open(secret, "w") as f:
        f.write("nope")
    # An escaping pattern must come back as "No files" (the not-found message),
    # not as a match that returns the file's path. The not-found message echoes
    # the pattern the model supplied, so the signal is the absence of a match,
    # not the absence of the path string.
    rel = os.path.relpath(secret, os.path.realpath(ws))
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": rel})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "No files" in r["output"] and secret not in r["output"]
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": secret})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "No files" in r["output"]


@pytest.mark.asyncio
async def test_subprocess_cwd_is_workspace_e2e(ws, admin):
    """python tool runs with cwd = workspace (OS-agnostic probe)."""
    _, r = await execute_tool_block(_block("python", "import os; print(os.getcwd())"), owner="a", workspace=ws)
    assert r["exit_code"] == 0
    assert os.path.realpath(r["output"].strip()) == os.path.realpath(ws)


# ── get_workspace tool ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_workspace_tool(ws, admin):
    _, r = await execute_tool_block(_block("get_workspace", ""), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and r["output"].startswith(ws) and "not sandboxed" in r["output"]
    _, r = await execute_tool_block(_block("get_workspace", ""), owner="a")  # none active
    assert r["exit_code"] == 0 and "No workspace" in r["output"]


# ── no leak across calls ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_binding_does_not_leak(ws, admin):
    await execute_tool_block(_block("ls", ""), owner="a", workspace=ws)
    assert get_active_workspace() is None


# ── tool selection: an active workspace is the file-work signal ─────────
# A vague ("low-signal") message like "look at the local project" matches no
# domain keywords, so retrieval is normally skipped. When a workspace is set it
# must still surface the file tools, otherwise the agent says it has no file
# access (the bug this guards against).

def _sent_tool_names(monkeypatch, *, workspace):
    import asyncio
    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    # Isolate the selection logic from owner gating (tested separately).
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    captured = []

    async def _fake_stream(_candidates, messages, **kwargs):
        captured.append(kwargs.get("tools"))
        yield "data: " + json.dumps({"delta": "ok"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _run():
        gen = al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-test",
            [{"role": "user", "content": "look at the local project"}],
            max_rounds=1, relevant_tools=None, owner="admin", workspace=workspace,
        )
        return [c async for c in gen]

    asyncio.run(_run())
    schemas = captured[0] or []
    return {t["function"]["name"] for t in schemas if isinstance(t, dict) and "function" in t}


def test_low_signal_with_workspace_surfaces_readonly_file_tools(monkeypatch):
    names = _sent_tool_names(monkeypatch, workspace="/tmp")
    # read-only nav tools surface so the agent can explore
    assert "read_file" in names
    assert "get_workspace" in names
    assert "grep" in names
    # write/shell tools do NOT surface on a vague message
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "bash" not in names
    assert "python" not in names


def test_low_signal_without_workspace_excludes_file_tools(monkeypatch):
    names = _sent_tool_names(monkeypatch, workspace=None)
    assert "read_file" not in names
    assert "get_workspace" not in names


# ── browse route is admin-gated ─────────────────────────────────────────

def test_browse_is_admin_gated(monkeypatch):
    from fastapi import HTTPException
    import routes.workspace_routes as wr

    router = wr.setup_workspace_routes()
    browse = next(r.endpoint for r in router.routes if r.path == "/api/workspace/browse")

    monkeypatch.setattr(wr, "get_current_user", lambda req: "bob")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    with pytest.raises(HTTPException) as ei:
        browse(request=object(), path="/")
    assert ei.value.status_code == 403

    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    out = browse(request=object(), path=os.path.expanduser("~"))
    assert "dirs" in out and "path" in out
    assert all("name" in d and "path" in d for d in out["dirs"])


# ── bind-time vetting of the workspace root ─────────────────────────────

def test_vet_workspace_accepts_normal_dir(ws):
    from src.tool_execution import vet_workspace
    assert vet_workspace(ws) == os.path.realpath(ws)


def test_vet_workspace_rejects_sensitive_root(tmp_path):
    # The resolver deny-lists sensitive paths inside the workspace, but the
    # empty-path search root is the workspace itself - a sensitive root must
    # be rejected before it is bound or `ls` with no path would list it.
    from src.tool_execution import vet_workspace
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    assert vet_workspace(str(ssh_dir)) is None


def test_vet_workspace_rejects_nondir_and_empty(ws):
    from src.tool_execution import vet_workspace
    assert vet_workspace(os.path.join(ws, "a.txt")) is None  # file, not dir
    assert vet_workspace("/nonexistent/path/xyz") is None
    assert vet_workspace("") is None
    assert vet_workspace("   ") is None


def test_vet_workspace_rejects_filesystem_root():
    # Binding / would make every absolute path "inside" the workspace,
    # collapsing confinement into host-wide file access.
    from src.tool_execution import vet_workspace
    assert vet_workspace("/") is None


def test_browse_marks_root_unselectable_and_vet_endpoint(monkeypatch):
    import routes.workspace_routes as wr

    router = wr.setup_workspace_routes()
    browse = next(r.endpoint for r in router.routes if r.path == "/api/workspace/browse")
    vet = next(r.endpoint for r in router.routes if r.path == "/api/workspace/vet")

    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)

    out = browse(request=object(), path="/")
    assert out["selectable"] is False
    out = browse(request=object(), path=os.path.expanduser("~"))
    assert out["selectable"] is True

    assert vet(request=object(), path="/") == {"ok": False, "path": None}
    home = os.path.realpath(os.path.expanduser("~"))
    assert vet(request=object(), path="~") == {"ok": True, "path": home}

    from fastapi import HTTPException
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    with pytest.raises(HTTPException) as ei:
        vet(request=object(), path="/tmp")
    assert ei.value.status_code == 403


# ── send-time privilege gate (no path oracle for non-admins) ────────────

def test_request_workspace_gate(ws, monkeypatch):
    """Non-admin chat callers must get a uniform drop with no vetting: the
    workspace_rejected signal would otherwise reveal which host paths exist."""
    import routes.chat_routes as cr

    monkeypatch.setattr(cr, "get_current_user", lambda req: "bob")
    vet_calls = []
    import src.tool_execution as te
    real_vet = te.vet_workspace
    monkeypatch.setattr(te, "vet_workspace", lambda p: vet_calls.append(p) or real_vet(p))

    import src.tool_security as ts
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: False)
    # Valid and invalid paths are indistinguishable for a non-admin: both
    # drop silently, and the path never reaches the filesystem.
    assert cr._resolve_request_workspace(object(), ws) == ("", "")
    assert cr._resolve_request_workspace(object(), "/nonexistent/xyz") == ("", "")
    assert vet_calls == []

    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: True)
    assert cr._resolve_request_workspace(object(), ws) == (os.path.realpath(ws), "")
    assert cr._resolve_request_workspace(object(), "/nonexistent/xyz") == ("", "/nonexistent/xyz")
>>>>>>> ba43c73 (fix(agent): confine glob literal lookups to the search root (#5010))
