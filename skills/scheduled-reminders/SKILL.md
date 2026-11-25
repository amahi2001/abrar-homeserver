---
name: scheduled-reminders
description: Set up one-off reminders, recurring digests, and persistent host checks from Codex with verified timing and delivery.
---

Resolve the timezone, next run, recurrence or end date, and delivery destination. Inspect existing schedules and update a match to avoid duplicate jobs.

Use a Codex automation when its scheduling and delivery tools are available for the chat. Use a NixOS systemd timer for persistent host checks. The weekly server check already runs as `codex-server-health.timer` and writes its report to the journal. For a personal reminder use the user's authorized reminder or calendar surface.

Give each scheduled job enough context to run independently. Verify its next run and destination after creation. Use the requested destination or keep server diagnostics in the local journal.
