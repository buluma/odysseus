# Odysseus Docs

Topic guides for running and extending Odysseus. For installation and setup see the [main README](../README.md).

| Guide | Summary |
|-------|---------|
| [setup.md](setup.md) | Comprehensive install, deployment, GPU passthrough, and troubleshooting guide |
| [deployment-homelab-openclaw.md](deployment-homelab-openclaw.md) | Production topology: Pi + Converge/Redmine, OpenClaw and Slack reaching Odysseus over Tailscale/Caddy |
| [backup-restore.md](backup-restore.md) | Backup and restore guide for `data/` snapshots and rollback |
| [agent-migration.md](agent-migration.md) | Agent migration manifests & normalization helper (`scripts/agent_migration_manifest.py`) |
| [slash-commands.md](slash-commands.md) | All `/command` shortcuts — homelab ops, tickets, n8n control, MCP, and more |
| [homelab-ops.md](homelab-ops.md) | Homelab operations layer — discovery, Prometheus/Grafana, scoped write operations |
| [events.md](events.md) | Homelab Events API — durable incident tracking stored in `data/homelab_events.json` |
| [n8n-monitoring.md](n8n-monitoring.md) | n8n read-only workflow monitoring; failures surfaced as durable events |
| [openclaw-bridge.md](openclaw-bridge.md) | OpenClaw Bridge scoped API for external clients (OpenClaw, Slack bots, scripts) |
| [email-outlook.md](email-outlook.md) | Outlook / Office 365 IMAP+SMTP account setup |
| [security-ci.md](security-ci.md) | Automated security CI checks per PR and push |
| [pr-blocker-audit.md](pr-blocker-audit.md) | Maintainer triage helper script (`scripts/pr_blocker_audit.py`) |
