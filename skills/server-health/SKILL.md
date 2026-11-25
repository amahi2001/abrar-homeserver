---
name: server-health
description: Check BuildFleet's NixOS services, disk usage, shopping research backups, and GitHub Actions runners.
---

Run `codex-server-health` for the current read-only overview. To read scheduled reports, run `journalctl -u codex-server-health.service -n 80 --no-pager`.

Inspect relevant unit logs with a bounded query such as `journalctl -u UNIT --since today -n 100 --no-pager`. For the shopping research backup, check the last execution timestamp and result together. For a runner, systemd reports the local service state. Use GitHub's runner API for the specific repository when you need to confirm GitHub sees it online.

Report the affected service or mount, the last observed outcome, and any missing evidence. Keep report output free of credentials and personal log details.
