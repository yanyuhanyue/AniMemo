from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
USES_LINE = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*(?P<value>\S+)")
REMOTE_ACTION = re.compile(
    r"(?P<owner>[^/@\s]+)/(?P<repository>[^@\s]+)@(?P<ref>[^\s#]+)"
)
FULL_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class ActionPinTests(unittest.TestCase):
    def test_every_remote_action_is_pinned_to_a_full_commit(self):
        violations = []
        for path in sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml"))):
            source = path.read_text(encoding="utf-8")
            for match in USES_LINE.finditer(source):
                value = match.group("value")
                if value.startswith(("./", "docker://")):
                    continue
                remote = REMOTE_ACTION.fullmatch(value)
                line = source.count("\n", 0, match.start()) + 1
                if remote is None or FULL_COMMIT.fullmatch(remote.group("ref")) is None:
                    violations.append(f"{path.relative_to(ROOT)}:{line}: {value}")

        self.assertEqual(
            violations,
            [],
            "remote GitHub Actions must use immutable 40-character commit pins",
        )


if __name__ == "__main__":
    unittest.main()
