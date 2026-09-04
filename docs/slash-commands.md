# Slash Commands

Slash commands are typed directly in the Odysseus chat input. Type `/` to open the autocomplete popup — it shows a curated list of commonly-used commands. Keep typing to filter, use arrow keys to navigate, and press Tab or Enter to insert. Subcommands (e.g. `/homelab disk`) expand when you complete the parent token.

Commands are defined in `static/js/slashCommands.js`; the autocomplete popup lives in `static/js/slashAutocomplete.js`.

---

## Homelab ops — `/homelab`

These run in the Odysseus web UI itself — the fetches are same-origin, authenticated by the normal session cookie, not a separate bridge URL or token. All endpoints are under `/api/openclaw/homelab/` unless noted.

| Command | Aliases | What it does | Endpoint |
|---------|---------|--------------|----------|
| `/homelab health` | | Bridge health check | `GET /api/openclaw/health` |
| `/homelab converge` | | Converge/Redmine connectivity check | `GET /api/openclaw/converge/health` |
| `/homelab events` | `ev`, `inc` | Open incidents and events | `GET /events?status=open&limit=20` |
| `/homelab disk` | `df` | Disk usage on Heimdal | `GET /ops/disk-usage` |
| `/homelab docker` | `ps` | Unhealthy Docker containers | `GET /ops/docker-unhealthy` |
| `/homelab ask <question>` | `q` | Ask a question about your homelab (AI-answered) | `POST /ask` |

Typing `/homelab` alone runs the `health` check.

---

## Tickets — `/tickets`

Reads Redmine/Converge ticket data via `CONVERGE_BASE_URL` + `CONVERGE_API_KEY`. Endpoints are under `/api/openclaw/`.

| Command | Aliases | What it does | Endpoint |
|---------|---------|--------------|----------|
| `/tickets digest` | | Open, stale, and assigned ticket digest | `GET /tickets/digest` |
| `/tickets search <query>` | `find`, `s` | Full-text ticket search | `POST /tickets/search` |
| `/tickets show <id>` | `get`, `view` | AI summary of a single ticket | `POST /tickets/{id}/summary` |

Typing `/tickets` alone runs the `digest`.

---

## n8n — `/n8n`

Reads n8n via `N8N_BASE_URL` + `N8N_API_KEY`. Write operations (`trigger`, `pause`) require the workflow name to appear in `N8N_WORKFLOW_ALLOWLIST` (unset or empty denies everything — fail-closed) and display a confirmation prompt. Endpoints are under `/api/openclaw/n8n/`. This is a different allowlist from `OPENCLAW_ALLOWED_WORKFLOWS` (see [openclaw-bridge.md](openclaw-bridge.md)), which gates the separate scheduled-workflow-trigger bridge route and fails open (empty disables enforcement) instead of closed.

| Command | Aliases | What it does | Endpoint |
|---------|---------|--------------|----------|
| `/n8n health` | | n8n connectivity check | `GET /health` |
| `/n8n failures` | `fail` | List recent failed executions | `GET /failures` |
| `/n8n workflows` | `wf`, `list` | List all workflows with status | `GET /workflows` |
| `/n8n trigger <name>` | `run` | Trigger/rerun a workflow (confirm-gated) | `POST /ops/n8n-rerun` |
| `/n8n pause <name>` | `stop` | Pause a workflow (confirm-gated) | `POST /ops/n8n-pause` |

---

## MCP servers — `/mcp`

Shows all connected MCP servers — both built-in servers and any user-added servers. Calls `GET /api/mcp/servers`. Built-in servers include `image_gen`, `memory`, `rag`, `email`, and `prometheus` (Python stdio) plus `builtin_browser` (npx, if cached). See the [README MCP section](../README.md#built-in-mcp-servers-optional-setup) for setup details.

---

## Other command families

These commands exist in the registry but are not OpenClaw-ops specific:

| Category | Commands |
|----------|----------|
| Chat / sessions | `/new`, `/chats`, `/fork`, `/rename`, `/export`, `/archive`, `/favorite` |
| Web & research | `/web` (toggle web search), `/research <topic>`, `/bash <cmd>` |
| Notes | `/note <text>`, `/notes` |
| Email | `/email` |
| Memory | `/memory`, `/memories`, `/forget` |
| Documents | `/doc` |
| To-do | `/todo` |
| Fun / misc | `/flip`, `/roll`, `/8ball`, `/fortune`, `/ascii`, `/demo` |

For the full registry with all aliases, see `static/js/slashCommands.js`.
