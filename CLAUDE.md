# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Odysseus is a self-hosted AI workspace (FastAPI + Python backend, vanilla JS frontend). It provides chat/agent, deep research, documents, memory, email, calendar, notes, and model-serving (Cookbook). All user data lives in `data/` (gitignored).

## Running the app

**Native (dev):**
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python setup.py
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

**macOS (Metal GPU / default dev port 7860):**
```bash
./start-macos.sh
```

**Docker:**
```bash
cp .env.example .env
docker compose up -d --build
docker compose logs --tail=120 odysseus
```

## Running tests

Always use the project venv interpreter — `python3` may be missing pinned deps:

```bash
# Full suite
.venv/bin/python -m pytest

# Focused by taxonomy area
python3 tests/run_focus.py --area security
python3 tests/run_focus.py --area services --sub-area cookbook
python3 tests/run_focus.py --area routes

# Fast lane (excludes slow-marked tests)
python3 tests/run_focus.py --fast

# Single file
.venv/bin/python -m pytest tests/test_llm_core_streaming.py

# With duration reporting
python3 tests/run_focus.py --area services --durations 25
```

**Taxonomy areas:** `security`, `routes`, `services`, `cli`, `js`, `helpers`, `unit`. Tests are auto-tagged from filename patterns; `area_*` and `sub_*` markers select them.

**JS tests** run via Node subprocess — they skip cleanly when `node` is absent.

## Checking syntax

```bash
python -m py_compile app.py routes/*.py src/*.py
node --check static/js/<file-you-changed>.js
```

## Architecture

```
app.py              FastAPI entry point — middleware, lifespan, route registration
core/               Shared infrastructure (auth, DB, middleware, session_manager, exceptions)
src/                Business logic — the brain of the app
  constants.py      Single source of truth for all paths and config (core/constants.py re-exports this)
  llm_core.py       LLM HTTP client, streaming, dead-host cooldown, response cache
  agent_loop.py     Multi-round tool-execution loop wrapping stream_llm()
  chat_handler.py   Chat endpoint orchestration (vision, YouTube, uploads)
  chat_processor.py Message pipeline
  agent_tools/      Tool facade — re-exports from subprocess_tools, web_tools, filesystem_tools, document_tools
  tool_registry.py  Tool registration
  tool_execution.py Tool dispatch and output truncation
  mcp_manager.py    MCP server lifecycle
  builtin_mcp.py    Registers built-in stdio MCP servers (image_gen, memory, rag, email, prometheus)
  memory.py         Memory extract/recall
  deep_research.py  Multi-step research pipeline
mcp_servers/        Built-in stdio MCP server scripts (one per server key in builtin_mcp.py)
routes/             HTTP layer — one file per feature domain (~45 files)
  openclaw_bridge_routes.py   OpenClaw external API — tickets, bridge health, alert ingest
  openclaw_homelab_routes.py  Homelab ops — events, disk, docker, ping, Grafana, services …
  openclaw_n8n_routes.py      n8n monitoring + workflow control (rerun/pause)
services/           External integrations (TTS, STT, search, memory, hwfit/Cookbook, shell, YouTube)
static/             Frontend — index.html + style.css + app.js + modular static/js/*.js
tests/              Flat test suite (~400 files); phased migration to subdirs in progress
```

## Critical conventions

**Paths — never derive locally.** Every persisted file/dir has a named constant in `src/constants.py` (e.g. `AUTH_FILE`, `SETTINGS_FILE`, `CHROMA_DIR`, `TTS_CACHE_DIR`). Import and use that constant. `DATA_DIR` is the single place that reads `ODYSSEUS_DATA_DIR`; `core/constants.py` is just a re-export shim.

**Internal API URLs — never hardcode `localhost:7000`.** Use `internal_api_base()` from `src.constants` (respects `ODYSSEUS_INTERNAL_BASE` / `APP_PORT`).

**Commit style:** Conventional Commits — `type(scope): summary` (e.g. `fix(search): ...`, `feat(notes): ...`). Imperative subject, "why" in body when non-obvious.

**PRs target `dev`**, not `main`. `main` is the curated stable branch.

## Frontend constraints

Before any visual change: run the app locally, view it in a browser, attach a screenshot or clip to the PR.

- Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, …). No new color values or spacing units.
- No Unicode emoji in UI or code. Use inline SVG matching the monochrome icon style in `static/index.html`.
- Primary font is `Fira Code` (monospaced). Don't override.
- Dark theme is the default. Light-mode goes through the existing theme system.
- Extend existing widgets; don't create parallel components.

## Testing rules

- Use `monkeypatch.setenv`/`delenv` for env vars, never raw `os.environ` mutations that outlive the test.
- Never mutate `sys.modules` at module scope without `tests.helpers.import_state.preserve_import_state`.
- Tests needing a real file-backed DB must opt in via `tests.helpers.sqlite_db.make_temp_sqlite`; the root conftest defaults to in-memory SQLite.
- Mark a test `@pytest.mark.slow` only with duration evidence from `--durations`; never to skip coverage.
- Behavioral assertions over source-text assertions — call the function/route and assert the outcome, not that a string appears in source.
