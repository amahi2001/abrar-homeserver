#!/usr/bin/env python3
"""Read-only log inventory for the daily server review; never run production jobs."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


ROOT = Path("/var/lib/hermes/.hermes/cron/service-log-review")
SUDO = "/run/wrappers/bin/sudo"
SYSTEM = "/run/current-system/sw/bin/"
WARNING = re.compile(r"\b(warn(?:ing)?|wrn|err(?:or)?|fatal|exception|traceback|failed|failure)\b", re.I)
USER_BUS = [SUDO, "-n", "-u", "buildfleet", SYSTEM + "env", "XDG_RUNTIME_DIR=/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"]


def flagged(message, priority=6, identifier=""):
    if int(priority) <= 4:
        return True
    if identifier == "sudo" or message.startswith("$ "):
        return False
    try:
        value = json.loads(message)
    except ValueError:
        value = None
    if isinstance(value, dict) and type(value.get("level")) is int:
        return value["level"] >= 2
    return bool(WARNING.search(message))


def redact(message):
    message = re.sub(r"https?://\S+", "[URL]", message)
    message = re.sub(r'''(?i)\b(Bearer|Basic)\s+[^\s,;"']+''', r"\1 [REDACTED]", message)
    return re.sub(
        r'''(?i)(["']?(?:password|token|secret|authorization|api[_-]?key)["']?\s*[:=]\s*)'''
        r'''(?:"[^"\n]*"|'[^'\n]*'|[^\s,}]+)''',
        r"\1[REDACTED]", message,
    )


class Collector:
    def __init__(self, events):
        self.events = events
        self.counts = defaultdict(Counter)
        self.gaps = []

    def command(self, *args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(redact(result.stderr)[:500])
            return result.stdout
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            self.gaps.append({"command": list(args), "error": redact(str(error))})
            return ""

    def record(self, source, message, *, priority=6, identifier="", stamp=None):
        self.counts[source]["entries"] += 1
        if flagged(message, priority, identifier):
            self.counts[source]["flagged"] += 1
            self.events.write(json.dumps({"source": source, "time": stamp,
                                          "message": redact(message)}) + "\n")

    def scan_journal(self, args):
        try:
            with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, errors="replace") as process:
                # Journalctl writes records to stdout and control errors to stderr.
                for line in process.stdout:
                    try:
                        entry = json.loads(line)
                        message = entry.get("MESSAGE", "")
                        if not isinstance(message, str):
                            continue
                        unit = entry.get("_SYSTEMD_USER_UNIT")
                        key = (f"user:{entry.get('_UID', '?')}:{unit}" if unit else
                               "system:" + entry.get("_SYSTEMD_UNIT", entry.get("SYSLOG_IDENTIFIER", "unknown")))
                        self.record(key, message, priority=entry.get("PRIORITY", 6),
                                    identifier=entry.get("SYSLOG_IDENTIFIER", ""),
                                    stamp=entry.get("__REALTIME_TIMESTAMP"))
                    except (ValueError, TypeError):
                        self.gaps.append({"source": "journal", "error": "Malformed journal record"})
                error = process.stderr.read()
                if process.wait():
                    self.gaps.append({"source": "journal", "error": redact(error)[:500]})
        except OSError as error:
            self.gaps.append({"source": "journal", "error": redact(str(error))})


def properties(text):
    return [dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            for block in text.strip().split("\n\n") if block.strip()]


def main():
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    directory = Path(tempfile.mkdtemp(prefix=end.strftime("%Y%m%dT%H%M%SZ-"), dir=ROOT))
    with (directory / "events.jsonl.tmp").open("w") as events:
        collector = Collector(events)
        inventory = collector.command(
            SUDO, "-n", SYSTEM + "systemd-run", "--wait", "--pipe", "--collect", "--uid=buildfleet", "--",
            SYSTEM + "nix", "eval", "--json", "--no-write-lock-file",
            "/etc/nixos#nixosConfigurations.buildfleet-server.config.systemd.services", "--apply",
            "services: builtins.filter (name: (builtins.getAttr name services).enable) (builtins.attrNames services)",
        )
        names = json.loads(inventory) if inventory else []
        names = [name + ".service" for name in names if not name.endswith("@")]
        # Modules can install vendor units through systemd.packages (e.g. Cockpit).
        packaged = collector.command(SYSTEM + "systemctl", "list-unit-files", "--type=service",
                                     "--no-legend", "--no-pager")
        names = sorted(set(names) | {line.split()[0] for line in packaged.splitlines()
                                    if line.split() and not line.split()[0].endswith('@.service')})
        fields = "Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,ExecMainExitTimestamp"
        system = properties(collector.command(SYSTEM + "systemctl", "show", "--no-pager", "--property=" + fields, *names)) if names else []
        user_names = collector.command(*USER_BUS, SYSTEM + "systemctl", "--user", "list-unit-files",
                                       "--type=service", "--no-legend", "--no-pager")
        user_names = [line.split()[0] for line in user_names.splitlines()
                      if line.split() and "@" not in line.split()[0]]
        user = properties(collector.command(*USER_BUS, SYSTEM + "systemctl", "--user", "show", "--no-pager",
                                           "--property=" + fields, *user_names)) if user_names else []
        collector.scan_journal([SUDO, "-n", SYSTEM + "journalctl", "--since", start.isoformat(),
                                "--until", end.isoformat(), "--no-pager", "-o", "json"])
        containers = collector.command(SUDO, "-n", SYSTEM + "docker", "ps", "-a", "--format", "{{.Names}}")
        for name in containers.splitlines():
            # Docker emits application stderr on stderr even with exit 0.
            source = "container:" + name
            collector.counts[source]  # Keep silent containers in the coverage inventory.
            with (directory / "container-stderr.tmp").open("w+", errors="replace") as error:
                with subprocess.Popen([SUDO, "-n", SYSTEM + "docker", "logs", "--timestamps", "--since",
                                       start.isoformat(), "--until", end.isoformat(), name],
                                      stdout=subprocess.PIPE, stderr=error, text=True, errors="replace") as process:
                    for line in process.stdout:
                        collector.record(source, line.rstrip())
                    code = process.wait()
                error.seek(0)
                for line in error:
                    if code:
                        collector.gaps.append({"source": source, "error": redact(line)[:500]})
                    else:
                        collector.record(source, line.rstrip())
        (directory / "container-stderr.tmp").unlink(missing_ok=True)
        units = []
        for scope, rows in (("system", system), ("user:1000", user)):
            for row in rows:
                key = scope + ":" + row["Id"]
                units.append({"source": key, **row, **collector.counts[key]})
        summary = {"window": {"start": start.isoformat(), "end": end.isoformat()},
                   "configured_units": units, "log_counts": dict(collector.counts),
                   "coverage_gaps": collector.gaps,
                   "remaining_work": "Discover and inspect configured application log files; investigate flagged events. Counts do not establish application health.",
                   "events_file": str(directory / "events.jsonl")}
    (directory / "events.jsonl.tmp").replace(directory / "events.jsonl")
    (directory / "summary.json.tmp").write_text(json.dumps(summary, indent=2) + "\n")
    (directory / "summary.json.tmp").replace(directory / "summary.json")
    configured = {row["source"] for row in units}
    print(json.dumps({
        "summary_file": str(directory / "summary.json"), "events_file": summary["events_file"],
        "window": summary["window"],
        "unit_columns": ["source", "state", "result", "last_exit", "entries", "flagged"],
        "units": [[row["source"], row.get("ActiveState"), row.get("Result"), row.get("ExecMainStatus"),
                   row.get("entries", 0), row.get("flagged", 0)] for row in units],
        "other_flagged_sources": {source: counts for source, counts in collector.counts.items()
                                  if source not in configured and counts["flagged"]},
        "containers": {source: counts for source, counts in collector.counts.items()
                       if source.startswith("container:")},
        "coverage_gaps": collector.gaps, "remaining_work": summary["remaining_work"],
    }))
    return summary


if __name__ == "__main__":
    main()
