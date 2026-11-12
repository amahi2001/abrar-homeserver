"""Exercise relocation, compatibility, rollback, and native fleet recreation."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import account_rename_dev as migration


class DevelopmentRenameTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old = self.root / "buildfleet"
        self.new = self.root / "amahi"
        self.old.mkdir()
        self.backup = self.root / "private-backup"
        self.commands = []
        self.containers = {}
        self.identities = {}
        self.add_project("agent-config", True)
        self.add_project("token-eye", True, custom=True)
        self.add_project("prompt-optimizer", False)
        auth = self.old / ".local/state/dev/accounts/codex/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text("dummy private auth; must remain unchanged\n")

    def add_project(self, name, running, custom=False):
        source = self.old / "projects" / name
        state = self.old / ".local/state/dev/projects" / name
        (source / ".git/info").mkdir(parents=True)
        (source / ".git/info/exclude").write_text("# preserve this comment\n")
        (state / "devcontainers").mkdir(parents=True)
        (state / "devcontainers/old-native-state").write_text("preserve me\n")
        (state / "home").mkdir()
        (state / "home/browser-state").write_text("unchanged browser\n")
        identity = migration.compose_identity(name, source)
        metadata = {"repo": "owner/" + name, "privateDefault": False,
                    "source": str(source), "composeProject": identity,
                    "image": "base:existing-" + identity, "baseImage": "base:existing",
                    "configHash": "stale"}
        (state / "project.json").write_text(json.dumps(metadata))
        volumes = [{"type": "bind", "source": str(source), "target": "/workspace/" + name},
                   {"type": "bind", "source": str(state / "home"), "target": "/home/node"},
                   {"type": "bind", "source": "/nix/store/keep-runtime", "target": "/runtime", "read_only": True}]
        ports = {"3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32828"}]}
        if custom:
            ports["8787/tcp"] = [{"HostIp": "172.17.0.1", "HostPort": "8787"}]
            (source / ".dev").mkdir()
            (source / ".dev/compose.yaml").write_text(
                "services:\n  dev:\n    environment:\n"
                f"      TOKEN_EYE_CLIENT_ROOT: {self.old}/.claude\n"
                "    ports:\n      - \"172.17.0.1:8787:8787\"\n"
                "    # Keep this build comment.\n"
                "    build:\n      context: .\n")
            (source / "scripts").mkdir()
            script = source / "scripts/token-eye-dogfood-server"
            script.write_text(f"#!/bin/sh\n# Keep the recovery explanation.\nlabel={identity}\n")
            script.chmod(0o755)
            (source / "scripts/token-eye-dogfood-server.test.py").write_text(f"EXPECTED = {identity!r}\n")
        compose = {"name": identity, "networks": {"default": {"name": identity + "_default"}},
                   "services": {"dev": {"image": metadata["image"], "volumes": volumes,
                                        "environment": {"KEEP_THIS": "same"},
                                        "ports": [{"host_ip": "127.0.0.1", "target": 3000}],
                                        "build": {"context": str(source)},
                                        "labels": {"dev.managed": "true", "dev.project": name}}}}
        native = {"dockerComposeFile": "compose.json", "remoteUser": "node",
                  "containerEnv": {"KEEP_NATIVE": "yes"},
                  "remoteEnv": {"PATH": "/runtime:${containerEnv:PATH}"}}
        (state / "compose.json").write_text(json.dumps(compose))
        (state / "devcontainer.json").write_text(json.dumps(native))
        container = {"Image": "sha256:custom" if custom else "sha256:base",
                     "State": {"Running": running, "Health": {"Status": "healthy"}},
                     "NetworkSettings": {"Ports": ports if running else {}},
                     "Mounts": [{"Type": "bind", "Source": v["source"],
                                 "Destination": v["target"], "RW": not v.get("read_only", False)}
                                for v in volumes]}
        self.containers["old-" + name] = container
        self.identities[identity] = ["old-" + name]

    def fake_run(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == "git":
            source = Path(args[args.index("-C") + 1])
            if "--absolute-git-dir" in args:
                return str(source / ".git")
            if "status" in args:
                if source.name == "token-eye" and source.parent.parent == self.new:
                    return (" M .dev/compose.yaml\n M scripts/token-eye-dogfood-server\n"
                            " M scripts/token-eye-dogfood-server.test.py")
                return ""
            if "symbolic-ref" in args:
                return "main" if source.name == "agent-config" else "agent/existing"
            return "same-commit"
        if args[:3] == ["docker", "ps", "-aq"]:
            identity = next(arg.split("=", 2)[2] for arg in args if arg.startswith("label=com.docker.compose.project="))
            return "\n".join(self.identities.get(identity, []))
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([self.containers[args[2]]])
        if args[:3] == ["docker", "image", "tag"]:
            return ""
        if args[0] == "runuser":
            config_path = Path(args[args.index("--config") + 1])
            config = json.loads((config_path.parent / "compose.json").read_text())
            project = config_path.parent.name
            identity = config["name"]
            existing = self.identities.get(identity, [])
            if existing and "--remove-existing-container" not in args:
                return json.dumps({"containerId": existing[0]})
            container = copy.deepcopy(self.containers["old-" + project])
            container["State"]["Running"] = True
            container["Mounts"] = [{"Type": v["type"], "Source": v["source"],
                                    "Destination": v["target"], "RW": not v.get("read_only", False)}
                                   for v in config["services"]["dev"]["volumes"]]
            self.containers["new-" + project] = container
            self.identities[identity] = ["new-" + project]
            return json.dumps({"containerId": "new-" + project})
        raise AssertionError(args)

    def capture(self):
        return migration.capture(self.old, self.backup, run=self.fake_run)

    def move_and_migrate(self, manifest):
        for container in self.containers.values():
            container["State"]["Running"] = False
        self.old.rename(self.new)
        migration.migrate(manifest, self.new, run=self.fake_run)

    def test_full_migration_preserves_images_ports_data_and_branches(self):
        manifest = self.capture()
        self.move_and_migrate(manifest)
        migration.start(manifest, self.new, run=self.fake_run)
        result = migration.verify(manifest, self.new, run=self.fake_run)
        self.assertEqual(result["running"], 2)
        self.assertEqual(result["ports"], 3)
        self.assertTrue(result["credentials_unchanged"])
        self.assertEqual(self.containers["new-token-eye"]["Image"], "sha256:custom")
        self.assertFalse(any("checkout" in command or "clone" in command for command in self.commands))
        self.assertFalse(any(command[:2] == ["dev", "start"] for command in self.commands))
        native = [command for command in self.commands if command[0] == "runuser"]
        self.assertEqual(Path(native[0][native[0].index("--config") + 1]).parent.name, "token-eye")
        self.assertFalse(any("prompt-optimizer" in " ".join(command) for command in native))
        self.assertEqual((self.new / ".local/state/dev/projects/token-eye/home/browser-state").read_text(), "unchanged browser\n")
        metadata = json.loads((self.new / ".local/state/dev/projects/token-eye/project.json").read_text())
        self.assertNotIn("configHash", metadata)
        self.assertEqual(metadata["repo"], "owner/token-eye")
        script = self.new / "projects/token-eye/scripts/token-eye-dogfood-server"
        self.assertIn(metadata["composeProject"], script.read_text())
        self.assertNotIn(manifest["projects"][2]["metadata"]["composeProject"], script.read_text())
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)
        self.assertIn("# Keep the recovery explanation.", script.read_text())

    def test_override_preserves_existing_build_and_excludes_new_local_config(self):
        manifest = self.capture()
        self.move_and_migrate(manifest)
        token = (self.new / "projects/token-eye/.dev/compose.yaml").read_text()
        self.assertIn("ports: !override", token)
        self.assertIn("    build:\n      context: .\n", token)
        self.assertIn("# Keep this build comment.", token)
        self.assertIn(str(self.new) + "/.claude", token)
        self.assertNotIn(str(self.old), token)
        self.assertIn("/.dev/compose.yaml", (self.new / "projects/agent-config/.git/info/exclude").read_text())
        self.assertFalse((self.new / "projects/prompt-optimizer/.dev/compose.yaml").exists())

    def test_retry_and_rollback_restore_original_bytes_and_native_state(self):
        manifest = self.capture()
        original = {relative: Path(item["backup"]).read_bytes()
                    for relative, item in manifest["files"].items() if item["existed"]}
        self.move_and_migrate(manifest)
        migration.migrate(manifest, self.new, run=self.fake_run)
        migration.rollback_files(manifest, self.new)
        migration.rollback_files(manifest, self.new)
        for relative, content in original.items():
            self.assertEqual((self.new / relative).read_bytes(), content)
        self.assertFalse((self.new / "projects/agent-config/.dev/compose.yaml").exists())
        self.assertEqual((self.new / ".local/state/dev/projects/token-eye/devcontainers/old-native-state").read_text(), "preserve me\n")

    def test_partial_failure_is_reversible(self):
        manifest = self.capture()
        self.old.rename(self.new)
        calls = 0

        def fail_second_image(args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected image-tag failure")
            return self.fake_run(args, **kwargs)

        with self.assertRaisesRegex(RuntimeError, "injected"):
            migration.migrate(manifest, self.new, run=fail_second_image)
        migration.rollback_files(manifest, self.new)
        self.assertFalse((self.new / "projects/agent-config/.dev/compose.yaml").exists())
        metadata = json.loads((self.new / ".local/state/dev/projects/agent-config/project.json").read_text())
        self.assertEqual(metadata["source"], str(self.old / "projects/agent-config"))

    def test_changed_image_or_credentials_fail_verification(self):
        manifest = self.capture()
        self.move_and_migrate(manifest)
        migration.start(manifest, self.new, run=self.fake_run)
        self.containers["new-token-eye"]["Image"] = "sha256:wrong-base"
        with self.assertRaisesRegex(RuntimeError, "invariant"):
            migration.verify(manifest, self.new, run=self.fake_run)
        self.containers["new-token-eye"]["Image"] = "sha256:custom"
        (self.new / ".local/state/dev/accounts/codex/auth.json").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "credentials"):
            migration.verify(manifest, self.new, run=self.fake_run)

    def test_retry_recreates_stale_containers_with_preserved_ports(self):
        manifest = self.capture()
        self.move_and_migrate(manifest)
        migration.start(manifest, self.new, run=self.fake_run)
        stale = self.containers["new-agent-config"]
        stale["NetworkSettings"]["Ports"]["3000/tcp"][0]["HostPort"] = "39999"
        migration.start(manifest, self.new, run=self.fake_run)
        result = migration.verify(manifest, self.new, run=self.fake_run)
        self.assertEqual(result["ports"], 3)

    def test_unrelated_recovery_script_edit_fails_verification(self):
        manifest = self.capture()
        self.move_and_migrate(manifest)
        migration.start(manifest, self.new, run=self.fake_run)
        script = self.new / "projects/token-eye/scripts/token-eye-dogfood-server"
        script.write_text(script.read_text() + "echo unexpected-edit\n")
        with self.assertRaisesRegex(RuntimeError, "Unexpected edits"):
            migration.verify(manifest, self.new, run=self.fake_run)

    def test_rejects_identity_drift_and_nonempty_backup(self):
        path = self.old / ".local/state/dev/projects/token-eye/project.json"
        metadata = json.loads(path.read_text())
        metadata["source"] = "/unexpected"
        path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.capture()
        with self.assertRaisesRegex(RuntimeError, "empty"):
            self.capture()

    def test_rejects_symlinked_home_and_nonbind_mounts(self):
        link = self.root / "old-home-alias"
        link.symlink_to(self.old, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "canonical"):
            migration.capture(link, self.backup, run=self.fake_run)
        self.containers["old-token-eye"]["Mounts"].append({"Type": "volume"})
        with self.assertRaisesRegex(RuntimeError, "bind-mounted"):
            self.capture()

    def test_rebase_keeps_branding_and_unrelated_paths(self):
        value = {"repo": "owner/buildfleet-dev-sandbox", "path": "/home/buildfleet/a",
                 "preserve": "/home/buildfleet-other/a", "json": '[{"root":"/home/buildfleet/.claude"}]'}
        actual = migration.rebase(value, "/home/buildfleet", "/home/amahi")
        self.assertEqual(actual["repo"], value["repo"])
        self.assertEqual(actual["preserve"], value["preserve"])
        self.assertEqual(actual["path"], "/home/amahi/a")
        self.assertIn("/home/amahi/.claude", actual["json"])


if __name__ == "__main__":
    unittest.main()
