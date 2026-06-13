# OpenClaw + Odysseus Ops Bot TODO

## Active Priorities

- [x] Inbox triage with Slack actions
  - [x] Reuse Odysseus `check_email_urgency` scanner instead of building a second classifier.
  - [x] Allow Slack to trigger the urgent inbox scan on demand.
  - [x] Expose urgent-email triage items to Slack with concise action commands.
  - [ ] Add Slack commands/actions:
    - [x] `ack`
    - [x] `create redmine ticket` draft
    - [x] `summarize thread`
    - [x] forwarding not needed for silo workflow
    - [x] `mute sender 2h`
  - [x] Store action state so repeated urgent-email alerts do not spam Slack.
  - [x] Gate Redmine creation behind explicit confirmation.

- [x] Slack-controlled homelab ops
  - [x] Add Slack command routing for homelab health, services, events, n8n health, and n8n failures.
  - [ ] Add safe read-only commands:
    - [x] `docker ps unhealthy`
    - [x] `tailscale status`
    - [x] `ping heimdal`
    - [x] `check grafana`
    - [x] `tail caddy logs`
    - [x] `disk usage`
  - [x] Add restart command support only for an explicit safe-service allowlist.
  - [x] Add backup command support only for explicit jobs such as `backup n8n`.

- [ ] Incident assistant
  - [x] Convert Grafana/Prometheus/container-down alerts into durable Odysseus events.
  - [x] On incident, pull recent container logs.
  - [x] Check Docker health.
  - [x] Check Caddy route.
  - [x] Post a short Slack diagnosis.
  - [x] Offer safe restart only for allowlisted services.
  - [x] Wire real Grafana/Prometheus webhook subscription on Heimdal.

## Backlog

- [ ] Redmine helper
  - [ ] Create Redmine tickets from Slack.
  - [ ] Create Redmine tickets from urgent email.
  - [ ] Summarize long Redmine updates.
  - [ ] Convert a Slack incident thread into a clean Redmine comment.
  - [ ] Daily digest: open issues, stale issues, assigned issues.

- [ ] Streamline sync watcher
  - [ ] Run sync jobs from Slack.
  - [ ] Post sync summaries.
  - [ ] Return created/updated/error counts.

- [ ] n8n mobile control layer
  - [x] Add Slack route coverage for n8n health and failed workflows.
  - [ ] Configure Heimdal n8n API env.
  - [ ] Show last execution.
  - [ ] Rerun workflow by allowlist.
  - [ ] Pause workflow by allowlist.
  - [ ] Backup n8n.

- [ ] Daily ops briefing
  - [ ] Unread urgent emails.
  - [ ] Failed cron jobs.
  - [ ] Docker unhealthy containers.
  - [ ] Disk usage.
  - [ ] Redmine tickets needing action.
  - [ ] NetBox sync errors.
  - [ ] Optional: weather, ADS-B, Telegram watcher status.

- [ ] Local Mac automation
  - [ ] Run allowlisted scripts from Slack.
  - [ ] Open apps/files.
  - [ ] Check local logs.
  - [ ] Trigger local backups.
  - [ ] Keep local-only tools private; no public inbound endpoint.

- [ ] GitHub repo assistant
  - [ ] Watch selected repos.
  - [ ] Notify on failed GitHub Actions.
  - [ ] Summarize PR changes.
  - [ ] Generate changelog notes.
  - [ ] Trigger local build/test scripts.

- [ ] Ask-my-homelab bot
  - [ ] Answer “why is immich slow?”
  - [ ] Answer “what containers restarted today?”
  - [ ] Answer “what changed in caddy logs?”
  - [ ] Answer “is tailscale healthy?”
  - [ ] Answer “show disk usage on pi.”

## Safety Defaults

- Read-only diagnostics may run directly when scoped to OpenClaw/Odysseus service tokens.
- Mutations require one of:
  - explicit allowlist plus confirmation,
  - single-purpose endpoint with constrained parameters,
  - or a pre-approved workflow name in `OPENCLAW_ALLOWED_WORKFLOWS`.
- No broad shell, Docker, Redmine write, n8n mutation, or Mac automation from Slack without an allowlist.
