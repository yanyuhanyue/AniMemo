"""Exercise the workflow's target derivation against the v4 receipt contract."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import candidate_receipt_regression as regression
from scripts import candidate_vm_harness as harness
from scripts.tests.test_release_workflows import _bash_path

ROOT = Path(__file__).resolve().parents[2]


class PublishPreflightVersionTests(unittest.TestCase):
    def test_actual_qualify_shell_branch_writes_major_rc_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs.txt"
            reservations = Path(temporary) / "publication-reservations.json"
            fixture = json.loads((ROOT / "release/publication-reservations.json").read_bytes())
            fixture["reservations"] = [record for record in fixture["reservations"] if "kind" not in record]
            reservations.write_text(json.dumps(fixture), encoding="utf-8")
            env = {
                **os.environ,
                "OPERATION": "qualify",
                "VERSION_BUMP": "major",
                "RELEASE_CHANNEL": "rc",
                "RUNNER_TEMP": Path(temporary).as_posix(),
                "GITHUB_OUTPUT": output.as_posix(),
                "PYTHONUTF8": "1",
                "ANIMEMO_TEST_RESERVATIONS": reservations.as_posix(),
            }
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
            # Keep the actual YAML branch and CLI. Only tag and reservation
            # inputs are local fixtures; production journal verification stays
            # unchanged and must never be contacted by this unit test.
            script = (
                'git() { [[ "$1" == tag && "$2" == --list ]] || return 97; printf "%s\\n" v1.0.0; }\n'
                + 'python() {\n'
                + '  local args=("$@"); local count=0; local i\n'
                + '  for ((i=0; i<${#args[@]}; i++)); do\n'
                + '    if [[ "${args[i]}" == --publication-reservations-file ]]; then\n'
                + '      args[i+1]="$ANIMEMO_TEST_RESERVATIONS"; count=$((count+1))\n'
                + '    fi\n'
                + '  done\n'
                + '  [[ "$count" == 1 ]] || return 98\n'
                + '  command python "${args[@]}"\n'
                + '}\n'
                + self.step
            )
            result = subprocess.run(
                [_bash_path(), "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            fields = dict(
                line.split("=", 1) for line in output.read_text("utf-8").splitlines()
            )
            self.assertEqual(fields["target_version"], "v2.0.0")
            self.assertRegex(fields["release_tag"], r"^v2\.0\.0-rc\.[1-9][0-9]*$")

    def setUp(self):
        document = yaml.safe_load(
            (ROOT / ".github/workflows/release.yml").read_text("utf-8")
        )
        self.step = next(
            s
            for s in document["jobs"]["preflight"]["steps"]
            if s.get("name") == "Resolve deterministic pre-release version"
        )["run"]

    def derive(self, tag):
        match = re.search(
            r'''target_version="\$\(python -c '([^']+)' "\$release_tag"\)"''', self.step
        )
        self.assertIsNotNone(
            match, "Publication must derive the base from its validated RC tag"
        )
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-c", match.group(1), tag],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_v4_receipt_without_target_version_drives_publish_base(self):
        source = regression.load_fixture(
            ROOT / "scripts/tests/fixtures/candidate-wire-real-scale.json.xz"
        )
        aggregate = harness.build_candidate_aggregate(
            regression._scope(source),
            profile_results=source["profileResults"],
            receipts=source["profileReceipts"],
            candidate_prestate=source["candidatePrestate"],
            candidate_poststate=source["candidatePrestate"],
            r2_prestate_receipt=source["r2OriginPrestateReceipt"],
            r2_poststate_receipt=source["r2OriginPoststateReceipt"],
            plugin_origin=True,
        )
        self.assertNotIn("target_version", aggregate)
        before = json.dumps(aggregate, sort_keys=True)
        result = self.derive(aggregate["candidate_version"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), aggregate["candidate_version"].split("-rc.")[0]
        )
        self.assertEqual(json.dumps(aggregate, sort_keys=True), before)

    def test_rejects_noncanonical_or_non_rc_tag(self):
        for tag in (
            "v2.0.0",
            "v2.0.0-beta.1",
            "v2.0.0-rc.0",
            "v02.0.0-rc.1",
            "v2.0.0-rc.1;echo x",
        ):
            with self.subTest(tag=tag):
                self.assertNotEqual(self.derive(tag).returncode, 0)


if __name__ == "__main__":
    unittest.main()
