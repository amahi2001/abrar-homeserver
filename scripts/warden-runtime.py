"""Select the supported Warden runtime from its installed package manifest."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


def select_node(manifest, node22, node24):
    requirement = json.loads(manifest.read_text()).get("engines", {}).get("node")
    if not isinstance(requirement, str):
        raise RuntimeError("Unsupported installed Warden Node requirement")
    match = re.fullmatch(r"~22\.23\.([0-9]+)", requirement)
    if requirement == ">=24":
        node = node24
    elif match:
        node = node22
    else:
        raise RuntimeError("Unsupported installed Warden Node requirement")
    reported = subprocess.check_output([node, "--version"], text=True, timeout=10).strip()
    version = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)", reported)
    actual = tuple(map(int, version.groups())) if version else ()
    if not actual or (requirement == ">=24" and actual[0] < 24
                      or match and (actual[:2] != (22, 23) or actual[2] < int(match[1]))):
        raise RuntimeError("Selected Node does not satisfy installed Warden requirements")
    return node


if __name__ == "__main__":
    try:
        node22, node24, package, *arguments = sys.argv[1:]
        manifest = Path(package)
        node = select_node(manifest, node22, node24)
        os.execv(node, [node, str(manifest.parent / "bin/warden-mcp.js"), *arguments])
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Warden runtime selection failed: {error}") from None
