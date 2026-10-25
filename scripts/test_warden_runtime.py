import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("warden_runtime", Path(__file__).with_name("warden-runtime.py"))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class WardenRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manifest = Path(temporary.name) / "package.json"

    def select(self, requirement, actual):
        self.manifest.write_text(json.dumps({"engines": {"node": requirement}}))
        with patch.object(runtime.subprocess, "check_output", return_value=actual + "\n") as probe:
            result = runtime.select_node(self.manifest, "/node22/bin/node", "/node24/bin/node")
            probe.assert_called_once_with([result, "--version"], text=True, timeout=10)
            return result

    def test_upgrade_and_directory_rollback_select_the_installed_contract(self):
        for requirement, actual, expected in ((">=24", "v24.22.1", "/node24/bin/node"),
                                               ("~22.23.4", "v22.23.4", "/node22/bin/node"),
                                               (">=24", "v24.22.1", "/node24/bin/node")):
            with self.subTest(requirement=requirement):
                self.assertEqual(self.select(requirement, actual), expected)

    def test_compatible_patch_releases_remain_supported(self):
        self.assertEqual(self.select("~22.23.4", "v22.23.5"), "/node22/bin/node")

    def test_incompatible_actual_runtimes_fail_before_launch(self):
        for requirement, actual in ((">=24", "v22.23.4"), ("~22.23.4", "v22.23.3"),
                                    ("~22.23.4", "v22.24.0"), ("~22.23.4", "v24.22.1"),
                                    ("~22.23.4", "unrecognized")):
            with self.subTest(requirement=requirement, actual=actual):
                with self.assertRaisesRegex(RuntimeError, "does not satisfy"):
                    self.select(requirement, actual)

    def test_unknown_manifest_contracts_fail_without_probing_or_launching(self):
        for requirement in (None, ">=22", "~22.24.0", "~22.23.4 || >=24"):
            with self.subTest(requirement=requirement):
                self.manifest.write_text(json.dumps({"engines": {"node": requirement}}))
                with patch.object(runtime.subprocess, "check_output") as probe:
                    with self.assertRaisesRegex(RuntimeError, "Unsupported installed Warden"):
                        runtime.select_node(self.manifest, "/node22/bin/node", "/node24/bin/node")
                    probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
