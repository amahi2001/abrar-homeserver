"""Exercise gateway rule installation with fake iptables and a private test lock."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class TokenEyeNetworkGuardTests(unittest.TestCase):
    def test_both_authenticated_ports_use_local_only_rules_without_duplicates(self):
        source = Path(__file__).with_name("token-eye-network-guard.sh").read_text()
        with tempfile.TemporaryDirectory(prefix="token-eye-guard-test-") as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            iptables = binary / "iptables"
            iptables.write_text(f'''#!{sys.executable}
import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
with Path(os.environ["RULES_LOG"]).open("a") as log:
    log.write(" ".join(args) + "\\n")
if args[1] == "-C":
    key = " ".join(args[2:])
    state = Path(os.environ["RULES_STATE"])
    installed = json.loads(state.read_text()) if state.exists() else []
    if key in installed:
        raise SystemExit(0)
    if args[2] == "DOCKER-USER":
        port = args[args.index("--ctorigdstport") + 1]
        if port in os.environ["EXISTING_PORTS"].split(","):
            raise SystemExit(0)
    raise SystemExit(1)
if args[1] == "-I":
    state = Path(os.environ["RULES_STATE"])
    installed = json.loads(state.read_text()) if state.exists() else []
    installed.append(" ".join([args[2], *args[4:]]))
    state.write_text(json.dumps(installed))
''')
            iptables.chmod(0o700)
            guard = source.replace("/run/lock/token-eye-network-guard.lock", str(root / "guard.lock"))
            log = root / "rules"
            state = root / "installed.json"
            for existing in ("", "8787", "8787,8788"):
                with self.subTest(existing=existing):
                    log.write_text("")
                    state.unlink(missing_ok=True)
                    environment = {**os.environ, "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
                                   "RULES_LOG": str(log), "RULES_STATE": str(state), "EXISTING_PORTS": existing}
                    for _ in range(2):
                        result = subprocess.run(["bash", "-ec", guard], env=environment, capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr.decode())
                    rules = log.read_text().splitlines()
                    for port in ("8787", "8788"):
                        insertion = f"-w -I DOCKER-USER 1 -p tcp -m conntrack --ctorigdst 172.17.0.1 --ctorigdstport {port} -j TOKEN-EYE-PROXY"
                        self.assertEqual(rules.count(insertion), 0 if port in existing.split(",") else 1)
                    self.assertIn("-w -A TOKEN-EYE-PROXY -i docker0 -j RETURN", rules)
                    self.assertIn("-w -A TOKEN-EYE-PROXY -i br+ -j RETURN", rules)
                    self.assertIn("-w -A TOKEN-EYE-PROXY -j DROP", rules)
                    accepted = [rule for rule in rules if rule.startswith(("-w -I", "-w -A")) and "ACCEPT" in rule]
                    expected = {f"-w -I INPUT 1 -i {interface} -d 172.17.0.1 -p tcp --dport {port} -j ACCEPT"
                                for interface in ("docker0", "br+") for port in (8787, 8788)}
                    self.assertEqual(set(accepted), expected)
                    self.assertEqual(len(accepted), len(expected))


if __name__ == "__main__":
    unittest.main()
