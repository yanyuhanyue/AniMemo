from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError

from scripts.ci_classify import classify_paths

ROOT = Path(__file__).resolve().parents[2]

HARDENED_WORKFLOWS = (
    "ci.yml",
    "dr-rehearsal.yml",
    "performance.yml",
    "pre-merge-full.yml",
    "promote-release.yml",
    "release-gate.yml",
    "release-metadata-freshness.yml",
    "release-mirror.yml",
    "release.yml",
)

PINNED_RELEASE_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "actions/setup-node": "820762786026740c76f36085b0efc47a31fe5020",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
}

PINNED_GH_VERSION = "2.97.0"
PINNED_GH_LINUX_AMD64_SHA256 = (
    "a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112"
)
PINNED_GH_ACTION = "./.github/actions/setup-pinned-gh-cli"


def _bash_path() -> str | None:
    if os.name == "nt":
        candidates = (
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        )
        return next((str(path) for path in candidates if path.is_file()), None)
    return shutil.which("bash")


class UniqueKeyLoader(yaml.BaseLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key ({key})",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def workflow(name):
    # PyYAML parses the YAML 1.1 word `on` as bool; BaseLoader preserves keys.
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"),
        Loader=UniqueKeyLoader,
    )


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_release_mirror_is_closed_read_only_and_default_branch_bound(self):
        mirror = workflow("release-mirror.yml")
        source = (ROOT / ".github" / "workflows" / "release-mirror.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(mirror["name"], "Release Mirror")
        self.assertEqual(set(mirror["on"]), {"release", "workflow_dispatch"})
        self.assertEqual(mirror["on"]["release"]["types"], ["published"])
        self.assertEqual(
            set(mirror["on"]["workflow_dispatch"]["inputs"]), {"release_tag"}
        )
        self.assertEqual(
            mirror["permissions"],
            {"contents": "read", "actions": "read", "attestations": "read"},
        )
        self.assertEqual(
            mirror["concurrency"]["group"],
            "release-mirror-${{ github.event.release.tag_name || inputs.release_tag }}",
        )
        self.assertEqual(mirror["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(set(mirror["jobs"]), {"mirror"})
        self.assertEqual(mirror["jobs"]["mirror"]["runs-on"], "ubuntu-24.04")
        self.assertNotIn("env", mirror)
        self.assertIn("验证固定 GitHub CLI 安全基线", source)
        self.assertIn(f"uses: {PINNED_GH_ACTION}", source)
        self.assertIn(
            "yanyuhanyue/AniMemo/.github/workflows/release-mirror.yml@refs/heads/main",
            source,
        )
        self.assertIn('test "$GITHUB_REF" = "refs/tags/$release_tag"', source)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', source)
        self.assertIn(
            'test "$(git rev-parse HEAD)" = "$(git rev-parse refs/remotes/origin/main)"',
            source,
        )
        self.assertIn(
            'test "$GITHUB_WORKFLOW_SHA" = "$(git rev-parse HEAD)"',
            source,
        )

    def test_release_mirror_has_fixed_uploader_and_three_secret_boundary(self):
        mirror = workflow("release-mirror.yml")
        source = (ROOT / ".github" / "workflows" / "release-mirror.yml").read_text(
            encoding="utf-8"
        )
        steps = mirror["jobs"]["mirror"]["steps"]
        publisher = next(
            step for step in steps if step.get("name") == "发布并匿名回读精确镜像"
        )
        secret_names = {
            "ANIMEMO_RELEASE_MIRROR_ACCOUNT_ID",
            "ANIMEMO_RELEASE_MIRROR_ACCESS_KEY_ID",
            "ANIMEMO_RELEASE_MIRROR_SECRET_ACCESS_KEY",
        }

        self.assertEqual(
            {name for name in publisher["env"] if name.startswith("ANIMEMO_")},
            secret_names,
        )
        self.assertEqual(
            {value for name, value in publisher["env"].items() if name in secret_names},
            {f"${{{{ secrets.{name} }}}}" for name in secret_names},
        )
        self.assertIn(
            "python -m release.mirror --release-tag \"$RELEASE_TAG\"", publisher["run"]
        )
        self.assertNotIn("ANIMEMO_RELEASE_MIRROR_", publisher["run"])
        upload = next(
            step
            for step in steps
            if step.get("uses", "").startswith("actions/upload-artifact@")
        )
        self.assertEqual(
            upload["uses"],
            f"actions/upload-artifact@{PINNED_RELEASE_ACTIONS['actions/upload-artifact']}",
        )
        self.assertEqual(
            upload["with"]["name"],
            "release-mirror-${{ steps.release.outputs.release_id }}",
        )
        for forbidden in (
            "contents: write",
            "packages: write",
            "id-token: write",
            "allow_overwrite",
            "fallback",
            "aws-actions/",
            "wrangler",
            "npx",
        ):
            self.assertNotIn(forbidden, source)

    def test_release_mirror_shell_steps_are_syntactically_valid(self):
        bash = _bash_path()
        self.assertIsNotNone(bash, "Release Mirror contract requires bash")
        mirror = workflow("release-mirror.yml")
        scripts = [
            step["run"]
            for step in mirror["jobs"]["mirror"]["steps"]
            if step.get("shell") == "bash" and "run" in step
        ]
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            with self.subTest(first_line=script.splitlines()[0]):
                syntax = subprocess.run(
                    [bash, "-n"],
                    input=script,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_dynamic_candidate_buildx_jobs_cannot_write_shared_caches(self):
        candidate_buildx_jobs = set()
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            document = yaml.load(
                path.read_text(encoding="utf-8"),
                Loader=UniqueKeyLoader,
            )
            for job_name, job in document.get("jobs", {}).items():
                steps = job.get("steps", [])
                checks_out_dynamic_candidate = any(
                    step.get("uses", "").startswith("actions/checkout@")
                    and step.get("with", {}).get("ref")
                    in (
                        "${{ inputs.candidate_sha }}",
                        "${{ github.sha }}",
                    )
                    for step in steps
                )
                buildx_steps = [
                    step
                    for step in steps
                    if step.get("uses", "").startswith(
                        "docker/setup-buildx-action@"
                    )
                ]
                if not checks_out_dynamic_candidate or not buildx_steps:
                    continue

                identity = f"{path.name}:{job_name}"
                candidate_buildx_jobs.add(identity)
                for step in buildx_steps:
                    inputs = step.get("with", {})
                    self.assertEqual(
                        inputs.get("cache-binary"),
                        "false",
                        identity,
                    )
                    self.assertIn(inputs.get("keep-state"), (None, "false"), identity)
                    self.assertIn(inputs.get("cleanup"), (None, "true"), identity)

                for step in steps:
                    action = step.get("uses", "").split("@", 1)[0]
                    self.assertFalse(
                        action == "actions/cache"
                        or action.startswith("actions/cache/"),
                        identity,
                    )
                    for key, value in step.get("with", {}).items():
                        if key == "cache-to":
                            self.fail(f"{identity} enables cache export: {value}")
                        if key == "keep-state":
                            self.assertNotEqual(value, "true", identity)

        self.assertEqual(
            candidate_buildx_jobs,
            {
                "performance.yml:isolated-resource-load",
                "performance.yml:isolated-long-operation-capacity",
                "release.yml:dry-run",
            },
        )

    def test_release_producer_is_built_once_with_a_commit_bound_epoch(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(source.count("--provenance=false"), 1)
        self.assertEqual(
            source.count('--build-arg "SOURCE_DATE_EPOCH=$source_date_epoch"'),
            1,
        )
        self.assertEqual(source.count('git show -s --format=%ct "$GITHUB_SHA"'), 1)
        self.assertIn("dry-run", release["jobs"])
        self.assertNotIn("qualification-evidence", release["jobs"])
        self.assertEqual(
            release["jobs"]["dry-run"]["env"]["ANIMEMO_CANDIDATE_BUILD_AUTHORITY"],
            "AUTHORITATIVE_CANDIDATE_BYTE_PRODUCER",
        )

    def test_release_producer_import_readiness_is_real_unique_and_earliest(self):
        release = workflow("release.yml")
        steps = release["jobs"]["dry-run"]["steps"]
        names = [step.get("name", "") for step in steps]
        readiness_name = "Verify release Producer repository import readiness"

        self.assertEqual(names.count(readiness_name), 1)
        build = names.index("Build the digest-pinned release byte producer")
        readiness = names.index(readiness_name)
        api = names.index("Build API OCI artifact without publishing")
        web = names.index("Build Web OCI artifact without publishing")
        rehearsal = names.index(
            "Start and accept the exact locally built API and Web images"
        )
        close = names.index("Close and verify all four Candidate OCI layouts")
        self.assertEqual(readiness, build + 1)
        self.assertLess(readiness, api)
        self.assertLess(api, web)
        self.assertLess(web, rehearsal)
        self.assertLess(rehearsal, close)

        command = steps[readiness]["run"]
        self.assertIn("scripts/run-in-release-producer.sh", command)
        self.assertIn("python -P -B -m release.cli --help", command)
        self.assertNotIn("continue-on-error", steps[readiness])

    def test_phase_a_python_invocation_inventory_includes_early_readiness(self):
        release = workflow("release.yml")
        producer_commands = []
        for step in release["jobs"]["dry-run"]["steps"]:
            command = step.get("run", "")
            marker = "scripts/run-in-release-producer.sh"
            if marker in command:
                producer_commands.append(command[command.index(marker) :])
        commands = "\n".join(producer_commands)
        invocations = re.findall(
            r'(?<![A-Za-z0-9_])python3?(?=(?:\s|"))|bin/python(?=")',
            commands,
        )
        package_modules = re.findall(
            r'(?:python3?|bin/python")\s+(?:-[A-Za-z]+\s+)*-m\s+'
            r"([A-Za-z0-9_.]+)",
            commands,
        )
        direct_scripts = re.findall(
            r"(?m)^\s*python(?:\s+-[A-Za-z]+)*\s+scripts/[^\s]+\.py(?:\s|$)",
            commands,
        )

        self.assertEqual(len(invocations), 30)
        self.assertEqual(len(package_modules), 24)
        self.assertEqual(len(direct_scripts), 2)
        repository_families = {
            module
            for module in (
                "release.cli",
                "release.producer_toolchain",
                "scripts.formal_windows_pretrust",
                "scripts.release_authority",
            )
            if module in commands
        }
        entrypoint = (
            ROOT / "scripts" / "release-producer-entrypoint.sh"
        ).read_text(encoding="utf-8")
        expected_provenance = {
            "release.cli": "release/cli.py",
            "release.producer_toolchain": "release/producer_toolchain.py",
            "scripts.formal_windows_pretrust": "scripts/formal_windows_pretrust.py",
            "scripts.release_authority": "scripts/release_authority.py",
        }
        inventory_blocks = re.findall(
            r"expected_modules = \{\n(.*?)\n\}", entrypoint, flags=re.DOTALL
        )
        self.assertEqual(len(inventory_blocks), 2)
        validated_inventories = [
            dict(
                re.findall(
                    r'^\s+"([A-Za-z0-9_.]+)": "([^"\n]+)",$',
                    block,
                    flags=re.MULTILINE,
                )
            )
            for block in inventory_blocks
        ]
        self.assertEqual(
            validated_inventories,
            [expected_provenance, expected_provenance],
        )
        self.assertEqual(repository_families, set(expected_provenance))

    def test_trusted_premerge_cannot_route_around_real_producer_test(self):
        premerge = workflow("pre-merge-full.yml")
        ci = workflow("ci.yml")
        plugins = ci["jobs"]["plugins"]
        producer_tests = (ROOT / "scripts" / "tests" / "test_producer_toolchain.py").read_text(
            encoding="utf-8"
        )

        self.assertEqual(premerge["jobs"]["full-regression"]["with"]["force_full"], "true")
        self.assertEqual(plugins["runs-on"], "ubuntu-24.04")
        self.assertIn("needs.classify.outputs.run_plugins == 'true'", plugins["if"])
        checkout = next(step for step in plugins["steps"] if "uses" in step)
        self.assertEqual(
            checkout["with"]["ref"],
            "${{ inputs.candidate_sha || (github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha) }}",
        )
        self.assertIn(
            'python -m unittest discover -s scripts/tests -p "test_*.py" -v',
            [step.get("run", "") for step in plugins["steps"]],
        )
        self.assertIn('os.environ.get("GITHUB_ACTIONS") == "true"', producer_tests)
        self.assertIn("trusted Linux CI must run the real Producer image test", producer_tests)

    def test_release_producer_binds_host_visible_output_staging(self):
        producer = (ROOT / "scripts" / "run-in-release-producer.sh").read_text(
            encoding="utf-8"
        )
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        qualification = source[
            source.index("  dry-run:\n") : source.index("  publish:\n")
        ]

        self.assertIn(
            'producer_release_output="$RUNNER_TEMP/animemo-release-producer-output"',
            producer,
        )
        self.assertIn(
            'producer_qualification_output="$RUNNER_TEMP/'
            'animemo-release-qualification-output"',
            producer,
        )
        self.assertIn(
            '--mount "type=bind,src=$producer_release_output,'
            'dst=$GITHUB_WORKSPACE/release-output"',
            producer,
        )
        self.assertIn(
            '--mount "type=bind,src=$producer_qualification_output,'
            'dst=$GITHUB_WORKSPACE/release-qualification"',
            producer,
        )
        self.assertIn(
            '> "$RUNNER_TEMP/animemo-release-producer-output/'
            'dry-run-authority-receipt.json"',
            qualification,
        )
        self.assertIn(
            "path: ${{ runner.temp }}/animemo-release-producer-output/"
            "dry-run-authority-receipt.json",
            qualification,
        )
        self.assertIn(
            "path: ${{ runner.temp }}/animemo-release-qualification-output/",
            qualification,
        )

    def test_release_producer_forwards_heredoc_stdin_into_the_container(self):
        producer = (ROOT / "scripts" / "run-in-release-producer.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("docker run --rm --init --interactive --read-only", producer)

    def test_candidate_large_bytes_have_one_authoritative_producer_and_one_upload(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        qualification = source[source.index("  dry-run:\n") : source.index("  publish:\n")]

        self.assertNotIn("qualification-evidence", release["jobs"])
        self.assertEqual(qualification.count("Build API OCI artifact without publishing"), 1)
        self.assertEqual(qualification.count("Build Web OCI artifact without publishing"), 1)
        self.assertEqual(qualification.count("Build the digest-pinned release byte producer"), 1)
        self.assertEqual(
            qualification.count(
                "path: ${{ runner.temp }}/animemo-release-qualification-output/"
            ),
            1,
        )
        self.assertNotIn("path: release-output/\n", qualification)
        self.assertIn(
            "path: ${{ runner.temp }}/animemo-release-producer-output/"
            "dry-run-authority-receipt.json",
            qualification,
        )
        self.assertIn('large_byte_payloads:[]', qualification)
        self.assertNotIn("Rebuild the exact qualification byte producer", source)
        self.assertNotIn("release-producer-toolchain-${{ github.run_id }}", source)

    def test_qualification_emits_exact_remote_controller_authority(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        authority = source[
            source.index("  dry-run:\n") : source.index("  publish:\n")
        ]
        controller = authority[
            authority.index("- name: Build the exact remote controller authority") :
            authority.index("- name: Upload the small remote controller authority")
        ]
        self.assertIn("build-prepublication-controller-authority", authority)
        self.assertIn(
            "QUALIFICATION_ARTIFACT_ID: "
            "${{ needs.dry-run.outputs.artifact_id }}",
            authority,
        )
        self.assertIn(
            "QUALIFICATION_ARTIFACT_DIGEST: "
            "${{ needs.dry-run.outputs.artifact_digest }}",
            authority,
        )
        self.assertIn(
            '"repos/${GITHUB_REPOSITORY}/actions/artifacts/'
            '${QUALIFICATION_ARTIFACT_ID}/zip"',
            authority,
        )
        self.assertIn(
            "name: Gate exact GitHub CLI security baseline for controller authority",
            authority,
        )
        self.assertIn(f"uses: {PINNED_GH_ACTION}", authority)
        self.assertLess(
            authority.index(
                "name: Gate exact GitHub CLI security baseline for controller authority"
            ),
            authority.index("- name: Build the exact remote controller authority"),
        )
        self.assertIn("--archive-stdin", controller)
        self.assertIn('--expected-archive-size "$artifact_size"', controller)
        self.assertNotIn("--root", controller)
        self.assertNotIn('> "$qualification_archive"', controller)
        self.assertIn(".size_in_bytes", controller)
        self.assertIn(".workflow_run.head_sha", controller)
        self.assertIn("needs: [dry-run]", authority)
        self.assertIn("actions: read", authority)
        self.assertIn("candidate-input.json", authority)
        self.assertIn("verified-candidate.json", authority)
        self.assertIn(
            "name: controller-authority-${{ github.run_id }}", authority
        )
        self.assertIn("path: controller-authority/", authority)
        self.assertEqual(authority.count("name: controller-authority-"), 1)
        self.assertEqual(authority.count("path: controller-authority/"), 1)

        for workflow_name, job_names in {
            "performance.yml": (
                "isolated-resource-load",
                "isolated-long-operation-capacity",
            ),
            "release-gate.yml": ("docker",),
        }.items():
            document = workflow(workflow_name)
            for job_name in job_names:
                self.assertEqual(
                    document["jobs"][job_name]["env"][
                        "ANIMEMO_CANDIDATE_BUILD_AUTHORITY"
                    ],
                    "NON_AUTHORITATIVE_ISOLATED_TEST_ONLY",
                )

    def test_release_resolver_requires_the_candidate_bound_reservation_ledger(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        preflight_steps = release["jobs"]["preflight"]["steps"]
        resolver = next(
            step
            for step in preflight_steps
            if step.get("name") == "Resolve deterministic pre-release version"
        )
        ledger_argument = (
            "--publication-reservations-file "
            "release/publication-reservations.json"
        )
        self.assertEqual(source.count(ledger_argument), 1)
        self.assertIn(ledger_argument, resolver["run"])
        self.assertNotIn("if", resolver)
        self.assertTrue((ROOT / "release" / "publication-reservations.json").is_file())
        self.assertIn("--publication-reservations-file", source)
        self.assertNotIn("publication-reservations.json ||", source)

        version_step = next(
            step
            for step in release["jobs"]["preflight"]["steps"]
            if step.get("name") == "Resolve deterministic pre-release version"
        )
        self.assertIn('if [[ "$OPERATION" = "publish" ]]', version_step["run"])
        self.assertIn("decode-candidate-acceptance-receipt", version_step["run"])
        self.assertIn(".candidate_version", version_step["run"])
        self.assertIn(".target_version", version_step["run"])
        reject_step = next(
            step
            for step in release["jobs"]["preflight"]["steps"]
            if step.get("name") == "Reject existing tag or GitHub Release"
        )
        self.assertEqual(reject_step["if"], "${{ inputs.operation == 'qualify' }}")

        with tempfile.TemporaryDirectory() as directory:
            tags = Path(directory) / "tags.txt"
            tags.write_text("v1.0.0\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    os.sys.executable,
                    "-m",
                    "release.cli",
                    "resolve-version",
                    "--tags-file",
                    str(tags),
                    "--publication-reservations-file",
                    str(ROOT / "release" / "publication-reservations.json"),
                    "--bump",
                    "minor",
                    "--channel",
                    "rc",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout)["releaseTag"], "v1.1.0-rc.6"
            )
            missing = subprocess.run(
                [
                    os.sys.executable,
                    "-m",
                    "release.cli",
                    "resolve-version",
                    "--tags-file",
                    str(tags),
                    "--publication-reservations-file",
                    str(Path(directory) / "missing-reservations.json"),
                    "--bump",
                    "minor",
                    "--channel",
                    "rc",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 2)
            self.assertEqual(
                json.loads(missing.stderr)["code"], "release_contract_invalid"
            )

    def test_release_crane_is_exactly_pinned_asserted_and_normalized_before_packaging(
        self,
    ):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish_steps = release["jobs"]["publish"]["steps"]
        setup_steps = [
            step
            for step in publish_steps
            if str(step.get("uses", "")).startswith("imjasonh/setup-crane@")
        ]
        self.assertEqual(len(setup_steps), 1)
        self.assertEqual(setup_steps[0].get("with"), {"version": "v0.21.9"})
        self.assertNotIn("latest-release", source)
        self.assertEqual(release["env"].get("CRANE_REQUIRED_VERSION"), "0.21.9")
        assertion = next(
            step
            for step in publish_steps
            if step.get("name") == "Assert exact crane version"
        )
        self.assertIn(
            'test "$(crane version)" = "$CRANE_REQUIRED_VERSION"', assertion["run"]
        )
        publish = next(
            step
            for step in publish_steps
            if step.get("name")
            == "Publish the four exact Candidate registry keys through the durable controller"
        )["run"]
        self.assertNotIn("crane pull", publish)
        self.assertNotIn("normalize-oci-layout", publish)
        self.assertNotIn("docker build", publish)
        self.assertIn("transaction-run", publish)
        self.assertIn("--phase registry", publish)
        self.assertNotIn("crane push", publish)
        remote = (ROOT / "release" / "publication_remote.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('(\"crane\", \"push\", str(self.source_layout), self.target)', remote)
        self.assertIn('(\"crane\", \"digest\", reference)', remote)
        self.assertIn("self._digest_observation(self.target)", remote)
        self.assertIn("source_layout=layout", remote)
        portable = next(
            step
            for step in publish_steps
            if step.get("name")
            == "Assemble the portable transport from accepted OCI layouts"
        )["run"]
        self.assertIn(
            'cp -a "$ANIMEMO_ACCEPTED_CANDIDATE_ROOT/candidate-runtime/oci"',
            portable,
        )
        self.assertNotIn("crane pull", portable)
        self.assertNotIn("normalize-oci-layout", portable)

    def test_oci_layout_function_has_no_same_command_local_dependency(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dependencies = []
        for line_number, line in enumerate(source.splitlines(), start=1):
            match = re.match(r"^\s*local\s+(.+)$", line)
            if not match:
                continue
            assigned = []
            for assignment in re.finditer(
                r"\b([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)",
                match.group(1),
            ):
                value = assignment.group(2)
                referenced = [
                    name
                    for name in assigned
                    if f"${name}" in value or f"${{{name}}}" in value
                ]
                if referenced:
                    dependencies.append((line_number, assignment.group(1), referenced))
                assigned.append(assignment.group(1))
        self.assertEqual(dependencies, [])
        for declaration in ("local role", "local reference"):
            self.assertIn(declaration, source)
        self.assertNotIn('archive="$RUNNER_TEMP/${role}-oci.tar"', source)
        self.assertIn('layout="$portable_source/oci/$role"', source)
        self.assertNotIn("normalize-oci-layout", source[source.index("  publish:\n") :])
        remote = (ROOT / "release" / "publication_remote.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('Path(candidate_root) / "candidate-runtime" / "oci" / role', remote)
        self.assertNotIn("shell=True", remote)

    def test_oci_layout_function_runs_under_bash_nounset_for_every_role(self):
        release = workflow("release.yml")
        publish = next(
            step
            for step in release["jobs"]["publish"]["steps"]
            if step.get("name")
            == "Publish the four exact Candidate registry keys through the durable controller"
        )["run"]
        self.assertIn("set -euo pipefail", publish)
        self.assertIn("python -m scripts.release_publication transaction-run", publish)
        self.assertIn("--candidate-root", publish)
        self.assertNotIn("crane push", publish)
        self.assertNotIn("crane digest", publish)
        self.assertNotIn("crane pull", publish)
        self.assertNotIn("normalize-oci-layout", publish)

    def test_partial_rc_is_never_deleted_overwritten_or_hardcoded_for_push(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "crane delete",
            "docker manifest rm",
            "git tag --force",
            "git push --force",
            "docker push ghcr.io/yanyuhanyue/animemo-api:v1.1.0-rc.1",
            "docker push ghcr.io/yanyuhanyue/animemo-web:v1.1.0-rc.1",
            "docker push ghcr.io/yanyuhanyue/animemo-api:v1.1.0-rc.2",
            "docker push ghcr.io/yanyuhanyue/animemo-web:v1.1.0-rc.2",
        ):
            self.assertNotIn(forbidden, source)

    def test_release_workflows_pin_every_external_action_to_a_commit(self):
        for name in (
            "release.yml",
            "release-metadata-freshness.yml",
            "promote-release.yml",
        ):
            source = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
            references = re.findall(
                r"uses:\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([^\s#]+)",
                source,
            )
            self.assertTrue(references)
            for action, reference in references:
                with self.subTest(workflow=name, action=action):
                    self.assertRegex(reference, r"^[0-9a-f]{40}$")

    def test_core_github_actions_are_exactly_pinned_and_checkout_credentials_are_explicit(self):
        credentialed_checkouts = set()
        checkout_count = 0

        for name in HARDENED_WORKFLOWS:
            document = workflow(name)
            for job_name, job in document["jobs"].items():
                for step in job.get("steps", []):
                    action = step.get("uses", "")
                    if action.startswith("actions/checkout@"):
                        checkout_count += 1
                        expected = f"actions/checkout@{PINNED_RELEASE_ACTIONS['actions/checkout']}"
                        self.assertEqual(action, expected)
                        settings = step.get("with", {})
                        self.assertIn("persist-credentials", settings)
                        if settings["persist-credentials"] == "true":
                            credentialed_checkouts.add((name, job_name))
                        else:
                            self.assertEqual(settings["persist-credentials"], "false")
                    elif action.startswith("actions/setup-node@"):
                        self.assertEqual(
                            action,
                            f"actions/setup-node@{PINNED_RELEASE_ACTIONS['actions/setup-node']}",
                        )
                    elif action.startswith("actions/setup-python@"):
                        expected = f"actions/setup-python@{PINNED_RELEASE_ACTIONS['actions/setup-python']}"
                        self.assertEqual(action, expected)

        self.assertGreater(checkout_count, 0)
        self.assertEqual(
            credentialed_checkouts,
            {
                ("promote-release.yml", "publish"),
                ("release.yml", "publish"),
            },
        )

    def test_dependabot_groups_only_minor_patch_version_updates(self):
        dependabot = yaml.load(
            (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        updates = {
            update["package-ecosystem"]: update for update in dependabot["updates"]
        }

        self.assertEqual(
            set(updates["npm"]["groups"]),
            {"frontend-production", "frontend-development"},
        )
        self.assertEqual(set(updates["pip"]["groups"]), {"backend-minor-patch"})
        self.assertEqual(
            set(updates["github-actions"]["groups"]),
            {"github-actions-minor-patch"},
        )

        for update in updates.values():
            for group in update.get("groups", {}).values():
                self.assertEqual(group["applies-to"], "version-updates")
                self.assertEqual(set(group["update-types"]), {"minor", "patch"})
                self.assertNotIn("major", group["update-types"])

    def test_security_policy_uses_private_reporting_for_preproduction(self):
        source = (ROOT / ".github" / "SECURITY.md").read_text(encoding="utf-8")

        self.assertIn("pre-production", source)
        self.assertIn("GitHub Private Vulnerability Reporting", source)
        self.assertIn("不要在公开 Issue", source)
        self.assertIn("tokens", source)
        self.assertIn("private user data", source)
        self.assertIn("exploit details", source)
        self.assertNotIn("v1.0 currently supported", source)

    def test_candidate_workflows_never_save_dependency_caches(self):
        for name in ("ci.yml", "performance.yml", "release.yml"):
            with self.subTest(workflow=name):
                document = workflow(name)
                setup_steps = [
                    step
                    for job in document["jobs"].values()
                    for step in job.get("steps", [])
                    if step.get("uses", "").startswith(
                        (
                            "actions/setup-node@",
                            "actions/setup-python@",
                            "actions/setup-go@",
                        )
                    )
                ]
                self.assertTrue(setup_steps)
                for step in setup_steps:
                    settings = step.get("with", {})
                    if step["uses"].startswith("actions/setup-go@"):
                        self.assertEqual(settings.get("cache"), "false")
                    else:
                        self.assertNotIn("cache", settings)
                    self.assertNotIn("cache-dependency-path", settings)

    def test_dr_rehearsal_has_no_cache_artifact_secret_or_write_authority(self):
        document = workflow("dr-rehearsal.yml")
        source = (ROOT / ".github" / "workflows" / "dr-rehearsal.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(document["permissions"], {"contents": "read"})
        self.assertNotIn("actions/cache@", source)
        self.assertNotIn("cache:", source)
        self.assertNotIn("actions/upload-artifact@", source)
        self.assertNotIn("secrets.", source)

    def test_astrbot_packaging_uses_only_the_canonical_dist_output(self):
        source = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("package-astrbot-bridge.py --output", source)
        self.assertNotIn(
            "${{ runner.temp }}/astrbot_plugin_animemo_bridge-0.1.3.zip",
            source,
        )
        self.assertGreaterEqual(
            source.count("dist/astrbot_plugin_animemo_bridge-0.1.3.zip"),
            2,
        )
        self.assertNotIn("ASTRBOT_ROOT:", source)

    def test_qualification_evidence_paths_are_runner_scoped_and_validated(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[
            source.index("      - name: Download and verify Phase A qualification evidence") :
            source.index("      - name: Stage the validated release input")
        ]
        stage = source[
            source.index("      - name: Stage the validated release input") :
            source.index("      - uses: actions/upload-artifact@", source.index("      - name: Stage the validated release input"))
        ]

        run_id_guard = '[[ "$QUALIFICATION_RUN_ID" =~ ^[1-9][0-9]*$ ]]'
        evidence_path = (
            'evidence_file="$RUNNER_TEMP/qualification/'
            'release-qualification-$QUALIFICATION_RUN_ID.json"'
        )
        authority_path = (
            "QUALIFICATION_ARTIFACT_PATH: ${{ runner.temp }}/qualification/"
            "release-qualification-${{ inputs.qualification_run_id }}.json"
        )
        self.assertIn(
            "QUALIFICATION_RUN_ID: ${{ inputs.qualification_run_id }}",
            stage,
        )
        self.assertIn(run_id_guard, publish)
        self.assertIn(evidence_path, publish)
        self.assertIn(authority_path, publish)
        self.assertLess(publish.index(run_id_guard), publish.index(evidence_path))
        self.assertLess(publish.index(evidence_path), publish.index(authority_path))
        self.assertIn(
            "QUALIFICATION_ARTIFACT_PATH: ${{ runner.temp }}/"
            "release-qualification-${{ github.run_id }}.json",
            source,
        )
        self.assertEqual(source.count("QUALIFICATION_ARTIFACT_PATH:"), 2)

    def test_all_workflows_reject_duplicate_mapping_keys(self):
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            with self.subTest(workflow=path.name):
                yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)

    def test_ci_and_release_gate_are_reusable_full_gates(self):
        ci = workflow("ci.yml")
        gate = workflow("release-gate.yml")
        self.assertIn("workflow_call", ci["on"])
        self.assertIn("workflow_call", gate["on"])
        self.assertIn("candidate_sha", ci["on"]["workflow_call"]["inputs"])
        self.assertIn("comparison_base_sha", ci["on"]["workflow_call"]["inputs"])
        self.assertIn("force_full", ci["on"]["workflow_call"]["inputs"])
        self.assertIn("candidate_sha", gate["on"]["workflow_call"]["inputs"])
        self.assertIn("upgrade_base_sha", gate["on"]["workflow_call"]["inputs"])
        self.assertIn("force_full", gate["on"]["workflow_call"]["inputs"])
        ci_source = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        gate_source = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8")
        self.assertIn("inputs.force_full && 'workflow_call'", ci_source)
        self.assertIn("inputs.force_full && 'workflow_call'", gate_source)
        self.assertIn("--base \"${{ inputs.upgrade_base_sha || '' }}\"", gate_source)
        self.assertEqual(
            gate_source.count(
                "--release-graph-contract animemo.release-gate.jobs/v2"
            ),
            1,
        )
        self.assertNotIn("--release-graph-contract", ci_source)

    def test_release_gate_requires_pinned_gh_official_output_contract(self):
        gate = workflow("release-gate.yml")
        updater_steps = gate["jobs"]["updater-isolated"]["steps"]
        commands = {step.get("run") for step in updater_steps if "run" in step}

        self.assertIn(
            "python -m unittest discover -s installer/tests -p 'test_*.py' -v",
            commands,
        )
        self.assertEqual(
            classify_paths(["installer/bootstrap.py"])["run_release_updater"],
            "true",
        )
        self.assertIn(
            "updater-isolated",
            gate["jobs"]["selection-authority"]["needs"],
        )

    def test_ci_and_release_gate_publish_complete_classifier_contract(self):
        expected_outputs = {
            "schema_version",
            "risk_level",
            "risk_rank",
            "execution_force_full",
            "classification_json",
            "authority_scalars_json",
            "docs_only",
            "mixed",
            "run_frontend",
            "run_backend",
            "run_bootstrap",
            "run_plugins",
            "run_bridge",
            "run_postgres",
            "run_runtime",
            "run_release_full",
            "run_release_updater",
            "run_release_docker",
            "run_release_stateful",
            "full_gate",
            "critical_gate",
        }
        for name in ("ci.yml", "release-gate.yml"):
            with self.subTest(workflow=name):
                outputs = set(workflow(name)["jobs"]["classify"]["outputs"])
                self.assertEqual(outputs, expected_outputs)

    def test_selection_authority_jobs_are_always_run_and_exhaustive(self):
        ci = workflow("ci.yml")
        gate = workflow("release-gate.yml")

        ci_authority = ci["jobs"]["selection-authority"]
        self.assertEqual(ci_authority["if"], "${{ always() }}")
        self.assertEqual(
            set(ci_authority["needs"]),
            {
                "classify",
                "fast-fail",
                "docs-only",
                "frontend",
                "backend",
                "bootstrap-smoke",
                "postgres",
                "plugins",
                "astrbot-bridge",
                "astrbot-runtime",
            },
        )
        self.assertEqual(
            ci["jobs"]["pr-fast-gate"]["needs"],
            ["selection-authority", "primary-category"],
        )

        release_authority = gate["jobs"]["selection-authority"]
        self.assertEqual(release_authority["if"], "${{ always() }}")
        self.assertEqual(
            set(release_authority["needs"]),
            {
                "classify",
                "post-merge-sanity",
                "updater-isolated",
                "docker",
                "stateful-upgrade",
                "dr-rehearsal",
            },
        )

    def test_release_workflow_is_manual_and_never_builds_stable(self):
        release = workflow("release.yml")
        self.assertEqual(set(release["on"]), {"workflow_dispatch"})
        inputs = release["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["channel"]["options"], ["beta", "rc"])
        self.assertEqual(inputs["operation"]["options"], ["qualify", "publish"])
        self.assertNotIn("dry_run", inputs)
        self.assertIn("candidate_sha", inputs)
        self.assertEqual(inputs["candidate_sha"]["required"], "false")
        self.assertIn("upgrade_base_sha", inputs)
        self.assertNotIn("target_version_override", inputs)
        self.assertIn("metadata_freshness_run_id", inputs)
        self.assertEqual(inputs["metadata_freshness_run_id"]["required"], "false")
        self.assertEqual(release["jobs"]["full-ci"]["uses"], "./.github/workflows/ci.yml")
        self.assertEqual(release["jobs"]["full-release-gate"]["uses"], "./.github/workflows/release-gate.yml")
        self.assertEqual(
            release["jobs"]["full-ci"]["with"]["candidate_sha"],
            "${{ needs.preflight.outputs.candidate_sha }}",
        )
        self.assertEqual(release["jobs"]["full-ci"]["with"]["comparison_base_sha"], "${{ inputs.upgrade_base_sha }}")
        self.assertTrue(release["jobs"]["full-ci"]["with"]["force_full"])
        self.assertEqual(
            release["jobs"]["full-release-gate"]["with"]["candidate_sha"],
            "${{ needs.preflight.outputs.candidate_sha }}",
        )
        self.assertEqual(release["jobs"]["full-release-gate"]["with"]["upgrade_base_sha"], "${{ inputs.upgrade_base_sha }}")
        self.assertTrue(release["jobs"]["full-release-gate"]["with"]["force_full"])

    def test_full_release_workflow_cannot_forward_bootstrap_override(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        header = source[: source.index("jobs:")]

        self.assertNotIn("target_version_override:", header)
        self.assertNotIn("inputs.target_version_override", source)
        self.assertNotIn("TARGET_VERSION_OVERRIDE", source)
        self.assertNotIn("--target-version-override", source)

    def test_performance_workflow_is_reusable_and_binds_every_runner_to_exact_main(self):
        performance = workflow("performance.yml")
        self.assertEqual(performance["on"], {"workflow_call": {}})

        source = (ROOT / ".github" / "workflows" / "performance.yml").read_text(encoding="utf-8")
        self.assertNotIn("workflow_dispatch:", source)
        self.assertNotIn("pull_request:", source)
        self.assertNotIn("merge_group:", source)
        self.assertNotIn("candidate_sha", source)
        self.assertEqual(source.count("ref: ${{ github.sha }}"), 5)
        self.assertEqual(source.count("&trusted_main_binding"), 1)
        self.assertEqual(source.count("*trusted_main_binding"), 4)
        for name, job in performance["jobs"].items():
            self.assertEqual(job["steps"][1]["id"], "trusted_main", name)
            self.assertEqual(
                job["steps"][1]["env"]["TRUSTED_SHA"],
                "${{ github.sha }}",
                name,
            )
        self.assertIn("services:", source)
        self.assertIn("postgres:", source)
        self.assertIn("redis:", source)
        self.assertIn("POSTGRESQL_AUTHORITATIVE", source)
        self.assertIn("for dataset in small medium large", source)
        self.assertIn('--dataset "$dataset"', source)
        self.assertIn("CONCURRENCY_LEVELS", source)
        self.assertIn("--duration-seconds 1500", source)
        self.assertIn("provision_performance_load_identities", source)
        self.assertIn("--identities-file", source)
        self.assertNotIn("--username perf-v1-owner", source)
        self.assertIn("$RUNNER_TEMP/animemo-performance-identities-", source)
        self.assertIn('chmod 600 "$identities_file"', source)
        self.assertNotIn('tee artifacts/seed.json', source)
        self.assertIn("SESSION_COOKIE_SECURE=false", source)
        self.assertIn("CSRF_COOKIE_SECURE=false", source)
        self.assertIn("REFRESH_COOKIE_SECURE=false", source)
        self.assertIn("ALLOW_INSECURE_PRODUCTION_COOKIES=true", source)
        self.assertGreaterEqual(
            source.count(
                "actions/upload-artifact@"
                f"{PINNED_RELEASE_ACTIONS['actions/upload-artifact']}"
            ),
            4,
        )
        self.assertIn("scripts/perf/regression_gate.py", source)
        self.assertIn("Require every performance evidence producer to succeed", source)
        self.assertIn("toJSON(needs)", source)
        self.assertIn('job["result"] != "success"', source)
        self.assertIn("FRONTEND_PERF_COMMIT: ${{ github.sha }}", source)
        self.assertNotIn("${{ inputs.", source)
        self.assertIn("isolated-long-operation-capacity:", source)
        self.assertIn("name: performance-long-operation-capacity", source)
        self.assertIn("ANIMEMO_ISOLATED_CAPACITY_PROBE=true", source)
        self.assertIn("ANIMEMO_ISOLATED_PROVIDER_LATENCY_MS=1200", source)
        self.assertIn("THROTTLE_USER_RATE=300/min", source)
        self.assertIn("--count 60", source)
        self.assertIn("scripts/perf/long_operation_capacity.py", source)
        capacity = source[
            source.index("  isolated-long-operation-capacity:") : source.index(
                "  regression-gate:"
            )
        ]
        self.assertIn("--iterations-per-user 4", capacity)
        self.assertIn(
            "needs: [frontend, backend, isolated-resource-load, isolated-long-operation-capacity]",
            source,
        )
        self.assertIn("name: performance-long-operation-capacity", source)
        self.assertIn("path: artifacts/capacity", source)

    def test_hosted_browser_gates_avoid_apt_and_launch_the_downloaded_chromium(self):
        for name in ("ci.yml", "performance.yml"):
            with self.subTest(workflow=name):
                source = (ROOT / ".github" / "workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("playwright install-deps", source)
                self.assertNotIn("--with-deps", source)
                self.assertIn("playwright install chromium", source)
                self.assertIn("chromium.launch", source)
                self.assertIn("browser.close()", source)
                self.assertIn("BROWSER_RUNTIME_VERIFICATION", source)
                self.assertLess(
                    source.index("playwright install chromium"),
                    source.index("chromium.launch"),
                )
                self.assertLess(
                    source.index("chromium.launch"),
                    source.index("browser.close()"),
                )

    def test_fresh_docker_gates_complete_the_real_one_time_setup_api(self):
        release_gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8")
        performance = (ROOT / ".github" / "workflows" / "performance.yml").read_text(encoding="utf-8")

        for source in (release_gate, performance):
            self.assertIn("scripts/ci_first_run.py", source)
            self.assertIn("--confirm-isolated", source)
            self.assertIn("--code-stdin", source)
            self.assertIn("sudo cat", source)
            self.assertIn(".example.test", source)
        self.assertIn("RELEASE_GATE_DATA_ROOT", release_gate)
        self.assertIn("PERF_DATA_ROOT", performance)
        self.assertIn("CSRF_COOKIE_SECURE=false", release_gate)
        self.assertNotIn("private/setup-code | tee", release_gate)
        self.assertNotIn("private/setup-code | tee", performance)

    def test_release_performance_is_rc_only_but_beta_dependencies_remain_live(self):
        release = workflow("release.yml")
        performance = release["jobs"]["performance"]
        self.assertEqual(performance["uses"], "./.github/workflows/performance.yml")
        self.assertEqual(performance["needs"], "preflight")
        self.assertEqual(performance["if"], "${{ inputs.operation == 'qualify' && inputs.channel == 'rc' }}")
        self.assertNotIn("with", performance)

        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        promote_source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        authority = release["jobs"]["release-authority"]
        self.assertEqual(
            authority["needs"],
            [
                "preflight",
                "full-ci",
                "full-release-gate",
                "performance",
                "platform-qualification",
            ],
        )
        self.assertEqual(authority["if"], "${{ always() }}")
        authority_source = source[source.index("  release-authority:\n") : source.index("  dry-run:\n")]
        self.assertIn("toJSON(needs)", authority_source)
        self.assertNotIn("ref: ${{ needs.preflight.outputs.candidate_sha }}", authority_source)
        self.assertIn("ref: ${{ github.sha }}", authority_source)
        self.assertIn("python -m scripts.release_authority", authority_source)
        self.assertEqual(source.count("python -m scripts.release_authority"), 4)
        self.assertNotIn("python scripts/release_authority.py", source)
        self.assertEqual(promote_source.count("python -m scripts.release_authority"), 1)
        self.assertNotIn("python scripts/release_authority.py", promote_source)
        self.assertEqual(
            release["jobs"]["dry-run"]["needs"],
            [
                "preflight",
                "full-ci",
                "full-release-gate",
                "performance",
                "platform-qualification",
                "release-authority",
            ],
        )
        self.assertEqual(
            release["jobs"]["publish"]["needs"],
            ["preflight", "release-authority", "metadata-freshness-authority"],
        )
        self.assertIn("performance", release["jobs"]["dry-run"]["needs"])
        self.assertNotIn("performance", release["jobs"]["publish"]["needs"])
        self.assertNotIn("performance.yml", (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.assertNotIn("performance.yml", (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8"))
        self.assertIn(
            "needs: [preflight, full-ci, full-release-gate, performance, platform-qualification]",
            authority_source,
        )
        self.assertEqual(authority["permissions"], {"contents": "read", "actions": "read"})
        self.assertIn("run_attempt=\"$(jq -r '.run_attempt // empty'", authority_source)
        self.assertIn('test "$run_attempt" = "1"', authority_source)

    def test_phase_b_publish_scheduling_is_skip_safe_and_fail_closed(self):
        release = workflow("release.yml")
        publish = release["jobs"]["publish"]
        condition = publish["if"]
        expected_condition = (
            "${{ !cancelled() && inputs.operation == 'publish' "
            "&& needs.preflight.result == 'success' "
            "&& needs.release-authority.result == 'success' "
            "&& needs.metadata-freshness-authority.result == 'success' }}"
        )

        self.assertEqual(
            publish["needs"],
            ["preflight", "release-authority", "metadata-freshness-authority"],
        )
        self.assertEqual(condition, expected_condition)
        for required_guard in (
            "!cancelled()",
            "inputs.operation == 'publish'",
            "needs.preflight.result == 'success'",
            "needs.release-authority.result == 'success'",
            "needs.metadata-freshness-authority.result == 'success'",
        ):
            self.assertIn(required_guard, condition)
        self.assertNotIn("always()", condition)

        cases = (
            ("qualify", False, "success", "success", "success", False),
            ("publish", False, "success", "success", "success", True),
            ("publish", False, "failure", "success", "success", False),
            ("publish", False, "success", "failure", "success", False),
            ("publish", False, "success", "success", "failure", False),
            ("publish", False, "success", "success", "skipped", False),
            ("publish", True, "success", "success", "success", False),
        )
        for operation, cancelled, preflight, authority, freshness, expected in cases:
            with self.subTest(
                operation=operation,
                cancelled=cancelled,
                preflight=preflight,
                authority=authority,
                freshness=freshness,
            ):
                eligible = (
                    not cancelled
                    and operation == "publish"
                    and preflight == "success"
                    and authority == "success"
                    and freshness == "success"
                )
                self.assertEqual(eligible, expected)

    def test_dry_run_is_read_only_and_publish_permissions_are_minimal(self):
        release = workflow("release.yml")
        dry_permissions = release["jobs"]["dry-run"]["permissions"]
        self.assertEqual(
            dry_permissions,
            {"actions": "read", "contents": "read"},
        )
        publish_permissions = release["jobs"]["publish"]["permissions"]
        self.assertEqual(publish_permissions["contents"], "write")
        self.assertEqual(publish_permissions["packages"], "write")
        self.assertEqual(publish_permissions["id-token"], "write")
        self.assertEqual(publish_permissions["attestations"], "write")
        self.assertNotIn("write-all", str(release))

    def test_candidate_override_is_dry_run_only_and_fail_closed(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        preflight = source[source.index("  preflight:\n") : source.index("  full-ci:\n")]
        publish = source[source.index("  publish:\n") :]

        for guard in (
            'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"',
            'test "$INTENDED_MAIN_SHA" = "$main_sha"',
            'elif [[ -n "$REQUESTED_CANDIDATE_SHA" ]]',
            'test "$REQUESTED_CANDIDATE_SHA" = "$GITHUB_SHA"',
            'test "$(git rev-parse HEAD)" = "$REQUESTED_CANDIDATE_SHA"',
            'test -z "$REQUESTED_CANDIDATE_SHA"',
            'test "$GITHUB_REF" = "refs/heads/main"',
            'test "$GITHUB_SHA" = "$main_sha"',
            'test "$INTENDED_MAIN_SHA" = "$GITHUB_SHA"',
            '[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]]',
            'git merge-base --is-ancestor "$UPGRADE_BASE_SHA" "$GITHUB_SHA"',
            'echo "candidate_sha=$GITHUB_SHA" >> "$GITHUB_OUTPUT"',
        ):
            self.assertIn(guard, preflight)
        self.assertNotIn('candidate_sha="$REQUESTED_CANDIDATE_SHA"', preflight)
        self.assertNotIn("DRY_RUN", preflight)
        self.assertNotIn("ref", release["jobs"]["preflight"]["steps"][0]["with"])
        self.assertNotIn('ref: ${{ steps.candidate.outputs.candidate_sha }}', preflight)
        self.assertIn('ref: ${{ github.sha }}', preflight)
        self.assertNotIn(
            "ref: ${{ needs.preflight.outputs.candidate_sha }}",
            source,
        )
        self.assertNotIn(
            "ref: ${{ steps.candidate.outputs.candidate_sha }}",
            source,
        )
        for job_name in ("full-ci", "full-release-gate", "performance"):
            uses = release["jobs"][job_name]["uses"]
            self.assertTrue(uses.startswith("./.github/workflows/"))
            self.assertNotIn("@main", uses)
        self.assertNotIn("inputs.candidate_sha", publish)
        self.assertIn("ref: main", publish)
        self.assertIn('test "$(git rev-parse origin/main)" = "$GITHUB_SHA"', publish)
        self.assertIn('test "$INTENDED_MAIN_SHA" = "$GITHUB_SHA"', publish)

    def test_candidate_dry_run_uses_exact_candidate_and_has_no_external_mutation(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        dry_run = source[source.index("  dry-run:\n") : source.index("  publish:\n")]

        self.assertNotIn("ref: ${{ needs.preflight.outputs.candidate_sha }}", dry_run)
        self.assertIn("ref: ${{ github.sha }}", dry_run)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"', dry_run)
        self.assertEqual(dry_run.count("ANIMEMO_COMMIT=${{ needs.preflight.outputs.candidate_sha }}"), 2)
        self.assertNotIn("ANIMEMO_COMMIT=${{ github.sha }}", dry_run)
        self.assertIn('--commit "${{ needs.preflight.outputs.candidate_sha }}"', dry_run)
        self.assertIn('RC_COMMIT == STABLE_COMMIT', dry_run)
        self.assertIn('RC_API_DIGEST == STABLE_API_DIGEST', dry_run)
        self.assertIn('RC_WEB_DIGEST == STABLE_WEB_DIGEST', dry_run)
        self.assertIn('RC_DEPLOYMENT == STABLE_DEPLOYMENT', dry_run)
        self.assertIn(".artifacts.deploymentContract", dry_run)
        for mutation in (
            "docker push",
            "git push",
            "git tag --annotate",
            "gh release create",
            "actions/attest",
            "docker/login-action",
        ):
            self.assertNotIn(mutation, dry_run)

    def test_release_notes_start_at_previous_stable_or_the_bootstrap_baseline(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        preflight = source[
            source.index("  preflight:\n") : source.index("  full-ci:\n")
        ]

        self.assertIn("UPGRADE_BASE_SHA: ${{ inputs.upgrade_base_sha }}", preflight)
        self.assertIn('release_notes_base="$UPGRADE_BASE_SHA"', preflight)
        self.assertIn('if [[ -n "$PREVIOUS_STABLE" ]]', preflight)
        self.assertIn(
            'release_notes_base="$(git rev-parse "$PREVIOUS_STABLE^{commit}")"',
            preflight,
        )
        self.assertIn(
            'git merge-base --is-ancestor "$release_notes_base" "$CANDIDATE_SHA"',
            preflight,
        )
        self.assertIn('--range-start "$release_notes_base"', preflight)
        self.assertIn('--comparison-base-sha "$release_notes_base"', preflight)
        self.assertNotIn('--range-start "$UPGRADE_BASE_SHA"', preflight)
        self.assertNotIn('test -n "$PREVIOUS_STABLE"', preflight)

    def test_release_images_receive_the_same_runtime_identity_as_the_manifest(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        publish_section = source[source.index("  publish:\n") :]
        self.assertGreaterEqual(source.count("ANIMEMO_VERSION=${{ needs.preflight.outputs.release_tag }}"), 2)
        self.assertEqual(source.count("ANIMEMO_COMMIT=${{ needs.preflight.outputs.candidate_sha }}"), 2)
        self.assertNotIn("ANIMEMO_COMMIT=${{ github.sha }}", publish_section)
        self.assertNotIn("VITE_TURNSTILE_SITE_KEY", source)
        self.assertIn(
            "Publish the four exact Candidate registry keys through the durable controller",
            publish_section,
        )
        self.assertIn("verify-publish-candidate-input", publish_section)
        self.assertIn("release-output/release-manifest.json", publish_section)
        self.assertNotIn("docker/build-push-action@", publish_section)
        self.assertNotIn("docker build", publish_section)
        self.assertNotIn("generate-manifest", publish_section)
        self.assertNotIn("scripts/rehearse-release-images.sh", publish_section)
        self.assertIn('test "$(jq -er \'.release.version\'', publish_section)
        self.assertIn('test "$(jq -er \'.release.commit\'', publish_section)

    def test_immutable_release_admin_read_credential_is_isolated_and_fail_closed(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        preflight = source[source.index("  preflight:\n") : source.index("  full-ci:\n")]
        publish = source[source.index("  publish:\n") :]
        endpoint = 'repos/$GITHUB_REPOSITORY/immutable-releases'
        secret = "${{ secrets.ANIMEMO_RELEASE_ADMIN_READ_TOKEN }}"
        early_name = "Verify immutable-release administration-read credential"
        final_name = "Recheck immutable release setting immediately before publishing"

        self.assertEqual(source.count(endpoint), 2)
        self.assertEqual(source.count(secret), 2)
        self.assertIn(early_name, preflight)
        self.assertIn(final_name, publish)

        preflight_steps = release["jobs"]["preflight"]["steps"]
        publish_steps = release["jobs"]["publish"]["steps"]
        early = next(step for step in preflight_steps if step.get("name") == early_name)
        final = next(step for step in publish_steps if step.get("name") == final_name)
        self.assertEqual(early["if"], "${{ inputs.operation == 'publish' }}")
        self.assertEqual(early["env"], {"GH_TOKEN": secret})
        self.assertEqual(final["env"], {"GH_TOKEN": secret})
        self.assertEqual(early["run"].count(endpoint), 1)
        self.assertEqual(final["run"].count(endpoint), 1)
        for step in (early, final):
            self.assertIn('test -n "${GH_TOKEN:-}"', step["run"])
            self.assertIn("gh api --method GET", step["run"])
            self.assertIn('type == "object"', step["run"])
            self.assertIn('.enabled == true', step["run"])
            self.assertIn(".enforced_by_owner", step["run"])
            self.assertNotIn("github.token", step["run"])

        for job in release["jobs"].values():
            self.assertNotIn("ANIMEMO_RELEASE_ADMIN_READ_TOKEN", str(job.get("env", {})))

        permission_text = json.dumps(
            {name: job.get("permissions", {}) for name, job in release["jobs"].items()},
            sort_keys=True,
        )
        self.assertNotIn("administration", permission_text)
        self.assertEqual(release["permissions"], {"contents": "read"})
        self.assertEqual(
            release["jobs"]["publish"]["permissions"],
            {
                "actions": "read",
                "contents": "write",
                "packages": "write",
                "id-token": "write",
                "attestations": "write",
                "artifact-metadata": "write",
            },
        )

        self.assertLess(source.index(early_name), source.index("Download and verify Phase A qualification evidence"))
        self.assertLess(source.index(early_name), source.index("docker/login-action"))
        self.assertLess(publish.index(final_name), publish.index("docker/login-action"))
        first_transaction = publish.index(
            "python -m scripts.release_publication transaction-reconcile"
        )
        self.assertLess(publish.index(final_name), first_transaction)
        self.assertNotIn("crane push", publish)
        self.assertNotIn('git push origin "refs/tags/', publish)
        self.assertNotIn('gh api --method POST "repos/$GITHUB_REPOSITORY/releases"', publish)
        self.assertNotIn("build-initial-trust-kit", publish)
        self.assertLess(
            publish.index("verify-prepublication-materials"),
            publish.index("docker/login-action"),
        )

    def test_release_contract_assets_and_real_upgrade_delta_are_fail_closed(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        promotion = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        gate = (ROOT / "scripts" / "stateful-upgrade-gate.sh").read_text(encoding="utf-8")

        self.assertEqual(release.count("generate-deployment-contract"), 1)
        self.assertEqual(release.count("build-installer-materials"), 1)
        self.assertGreaterEqual(release.count("-r durability/requirements.lock"), 1)
        self.assertGreaterEqual(release.count("installer-materials.tar"), 10)
        self.assertGreaterEqual(release.count("deployment-contract.json"), 8)
        self.assertGreaterEqual(promotion.count("deployment-contract.json"), 7)
        self.assertGreaterEqual(promotion.count("installer-materials.tar"), 7)
        self.assertIn(
            "cp --no-clobber rc-assets/installer-materials.tar promotion-output/installer-materials.tar",
            promotion,
        )
        self.assertNotIn("build-installer-materials", promotion)
        self.assertNotIn("docker.io/library/postgres@sha256:", release)
        self.assertNotIn("docker.io/library/redis@sha256:", release)
        self.assertIn(
            'python -m release.registry_transport pull-all --projection github-env >> "$GITHUB_ENV"',
            release,
        )
        self.assertIn('test "$UPGRADE_BASE_SHA" != "$GITHUB_SHA"', release)
        self.assertIn('if [[ "$BASE_SHA" == "$HEAD_SHA" ]]', gate)
        release_gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("timeout-minutes: 40", release_gate)
        self.assertIn('merge-base --is-ancestor "$BASE_SHA" "$HEAD_SHA"', gate)

    def test_dependency_workflows_bind_once_and_use_closed_transport_roles(self):
        expected_counts = {
            "release.yml": 2,
            "release-gate.yml": 3,
            "performance.yml": 2,
        }
        projection = (
            'python -m release.registry_transport pull-all --projection github-env '
            '>> "$GITHUB_ENV"'
        )
        for name, count in expected_counts.items():
            source = (ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
            with self.subTest(workflow=name):
                self.assertEqual(source.count(projection), count)
                self.assertNotIn("release.registry_transport pull --role", source)
                self.assertNotIn("docker.io/library/postgres@sha256:", source)
                self.assertNotIn("docker.io/library/redis@sha256:", source)
        release_publish = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        ).split("  publish:\n", 1)[1]
        self.assertNotIn(projection, release_publish)
        self.assertIn(
            "Assemble the portable transport from accepted OCI layouts",
            release_publish,
        )

    def test_all_dependency_compose_startups_disable_hidden_pulls(self):
        paths = (
            ROOT / ".github" / "workflows" / "release-gate.yml",
            ROOT / ".github" / "workflows" / "performance.yml",
            ROOT / "scripts" / "dr-rehearsal.sh",
            ROOT / "scripts" / "stateful-upgrade-gate.sh",
            ROOT / "scripts" / "rehearse-release-images.sh",
        )
        for path in paths:
            startup_lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if re.search(r"(?:docker compose|compose [ab]|run_compose|COMPOSE\[@\]).*\bup\b", line)
            ]
            with self.subTest(path=path.name):
                self.assertTrue(startup_lines)
                for line in startup_lines:
                    self.assertIn("--pull never", line, line)

    def test_dr_upgrade_and_release_rehearsal_use_canonical_transport(self):
        for name in (
            "dr-rehearsal.sh",
            "stateful-upgrade-gate.sh",
            "rehearse-release-images.sh",
        ):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            active_source = "\n".join(
                line
                for line in source.splitlines()
                if not line.lstrip().startswith("#")
            )
            with self.subTest(script=name):
                self.assertEqual(
                    source.count(
                        "release.registry_transport pull-all --projection compose-env"
                    ),
                    1,
                )
                self.assertNotIn("dependency_images.py\" emit-github-env", source)
                self.assertNotIn("release.registry_transport pull --role", source)
                self.assertNotIn(
                    "docker.io/library/postgres@sha256:", active_source
                )
                self.assertNotIn("docker.io/library/redis@sha256:", active_source)

    def test_publish_acquires_dependencies_before_any_external_mutation(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[source.index("  publish:") :]
        accepted_gate = publish.index(
            "Verify and stage exact qualified prepublication materials before mutation"
        )
        self.assertNotIn("release.registry_transport pull-all", publish)
        self.assertNotIn("crane pull", publish)
        for mutation_marker in ("docker/login-action", "actions/attest@"):
            with self.subTest(marker=mutation_marker):
                self.assertLess(accepted_gate, publish.index(mutation_marker))
        first_transaction = publish.index(
            "python -m scripts.release_publication transaction-reconcile"
        )
        self.assertLess(accepted_gate, first_transaction)
        self.assertLess(
            publish.index("Generate the closed publication plan without mutation"),
            first_transaction,
        )
        for forbidden in (
            "crane push",
            'git push origin "refs/tags/',
            'gh api --method POST "repos/$GITHUB_REPOSITORY/releases"',
            'gh release upload "$RELEASE_TAG"',
        ):
            self.assertNotIn(forbidden, publish)
        post_mutation = publish[
            first_transaction:
        ]
        self.assertNotIn("normalize-oci-layout", post_mutation)
        self.assertNotIn("generate-manifest", post_mutation)
        self.assertIn(
            'cp -a "$ANIMEMO_ACCEPTED_CANDIDATE_ROOT/candidate-runtime/oci"',
            publish[:first_transaction],
        )

    def test_publish_normalizes_the_run_scoped_qualification_filename(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[source.index("  publish:") :]
        stage = publish[
            publish.index(
                "Verify and stage exact qualified prepublication materials before mutation"
            ) : publish.index("Recheck immutable release identity immediately before publishing")
        ]
        self.assertIn(
            'qualification_source="$candidate_root/release-qualification-'
            '${{ inputs.qualification_run_id }}.json"',
            stage,
        )
        self.assertIn(
            'install -m 0600 "$qualification_source" \\\n'
            "            release-output/release-qualification.json",
            stage,
        )
        self.assertNotIn(
            '"$candidate_root/release-qualification.json"',
            stage,
        )

    def test_release_verifier_is_built_offline_from_a_pinned_go_toolchain(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            release.count(
                "actions/setup-go@924ae3a1cded613372ab5595356fb5720e22ba16"
            ),
            1,
        )
        self.assertEqual(release.count("go-version: '1.26.6'"), 1)
        self.assertEqual(release.count("cache: false"), 1)
        self.assertNotIn("cache-dependency-path", release)
        self.assertEqual(release.count("go mod download"), 1)
        self.assertEqual(release.count("GOPROXY=off GOSUMDB=off go mod verify"), 1)
        self.assertEqual(
            release.count("GOPROXY=off GOSUMDB=off go test ./..."), 1
        )
        self.assertEqual(
            release.count(
                "CGO_ENABLED=0 GOOS=linux GOARCH=amd64 "
                "GOPROXY=off GOSUMDB=off"
            ),
            1,
        )
        self.assertEqual(
            release.count(
                "CGO_ENABLED=0 GOOS=windows GOARCH=amd64 "
                "GOPROXY=off GOSUMDB=off"
            ),
            1,
        )
        self.assertEqual(
            release.count(
                "go build -mod=readonly -trimpath -o offline-release-verifier ."
            ),
            1,
        )
        self.assertEqual(
            release.count(
                '-o "../../$formal_pretrust_work/formal-release-verifier.exe" .'
            ),
            1,
        )
        self.assertEqual(
            release.count(
                "test -x release/release_attestation_verifier/offline-release-verifier"
            ),
            1,
        )
        self.assertEqual(release.count("build-initial-trust-kit"), 1)
        self.assertEqual(
            release.count("python -B -m scripts.formal_windows_pretrust build"),
            1,
        )
        self.assertEqual(
            release.count("--formal-windows-pretrust-kit"), 1
        )
        self.assertEqual(
            release.count(
                "python -B -m scripts.formal_windows_pretrust "
                "inspect-installer-materials"
            ),
            1,
        )
        self.assertIn(".formalWindowsPretrust.kitIdentity", release)
        self.assertIn(".formalWindowsPretrust.sourceProfileIdentity", release)
        self.assertIn(
            '"scripts/candidate_profile_runner.py"',
            (ROOT / "release" / "materials.py").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"scripts/closed_runtime_inventory.py"',
            (ROOT / "release" / "materials.py").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"scripts/formal_profile_runner.py"',
            (ROOT / "release" / "materials.py").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "scripts/closed_runtime_inventory.py | cmp - "
            "scripts/closed_runtime_inventory.py",
            release,
        )
        self.assertNotIn(
            "--output release/release_attestation_verifier/pretrust-v2",
            release,
        )
        self.assertNotIn("$RUNNER_TEMP/animemo-pretrust-v2", release)
        self.assertEqual(
            release.count(
                '--initial-trust-kit "$formal_pretrust_work/initial-trust-kit"'
            ),
            1,
        )
        self.assertNotIn("--verifier \"$RUNNER_TEMP", release)
        self.assertNotIn(
            "python -B -m scripts.formal_windows_pretrust "
            "inspect-installer-materials \\\n"
            "            --installer-materials",
            release,
        )

    def test_publish_rebinds_exact_qualified_prepublication_materials(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dry_run_identity = release.index("build-prepublication-materials")
        qualification_copy = release.index(
            "install -m 0600 release-output/prepublication-materials.json"
        )
        publish_verify = release.index(
            "Verify and stage exact qualified prepublication materials before mutation"
        )
        immutable_recheck = release.index(
            "Recheck immutable release identity immediately before publishing"
        )
        docker_login = release.index("docker/login-action@", publish_verify)
        self.assertLess(dry_run_identity, qualification_copy)
        self.assertLess(publish_verify, immutable_recheck)
        self.assertLess(publish_verify, docker_login)
        self.assertIn("memberManifestSha256", (ROOT / "release" / "materials.py").read_text(encoding="utf-8"))
        self.assertIn("candidateTreeSha", release)
        self.assertIn("validated-release-input/installer-materials.tar", release)
        self.assertIn("validated-release-input/deployment-contract.json", release)

    def test_publish_consumes_only_frozen_phase_a_prepublication_bytes(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dry_run = release[
            release.index("  dry-run:\n") : release.index("  publish:\n")
        ]
        publish = release[release.index("  publish:\n") :]

        self.assertEqual(dry_run.count("build-installer-materials"), 1)
        self.assertEqual(publish.count("build-installer-materials"), 0)
        self.assertEqual(publish.count("build-initial-trust-kit"), 0)
        self.assertEqual(publish.count("python -m pip download"), 0)
        self.assertEqual(publish.count("generate-deployment-contract"), 0)
        self.assertIn("verify-prepublication-materials", publish)
        self.assertIn(
            "Verify and stage exact qualified prepublication materials before mutation",
            publish,
        )

    def test_frozen_material_transport_is_exact_and_authority_bound(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        release_authority = release[
            release.index("  release-authority:\n") : release.index("  dry-run:\n")
        ]
        qualification = release[
            release.index("  dry-run:\n") : release.index("  publish:\n")
        ]
        publish = release[release.index("  publish:\n") :]

        for frozen_name in (
            "installer-materials.tar",
            "deployment-contract.json",
            "prepublication-materials.json",
        ):
            self.assertIn(
                f"release-qualification/{frozen_name}", qualification
            )
            self.assertIn(
                f"validated-release-input/{frozen_name}", release_authority
            )
            self.assertIn(
                f"validated-release-input/{frozen_name}", publish
            )
        self.assertIn("retention-days: 30", qualification)
        self.assertIn("test \"$artifact_count\" = \"1\"", release_authority)
        self.assertIn("test \"$artifact_expired\" = \"false\"", release_authority)
        self.assertIn("actual_digest=\"sha256:$(sha256sum", release_authority)
        self.assertIn("test \"$actual_digest\" = \"$metadata_digest\"", release_authority)
        self.assertIn("test \"$run_head\" =", release_authority)
        self.assertIn("qualification_workflow_ref", release_authority)
        self.assertIn("QUALIFICATION_WORKFLOW_SHA", release_authority)
        self.assertIn("extract-qualification-artifact", release_authority)
        self.assertIn(
            '--expected-sha256 "$metadata_digest"', release_authority
        )
        self.assertGreaterEqual(
            release_authority.count("verify-prepublication-materials"), 1
        )
        self.assertGreaterEqual(qualification.count("verify-prepublication-materials"), 1)
        self.assertGreaterEqual(publish.count("verify-prepublication-materials"), 3)

    def test_every_github_authority_job_gates_the_exact_cli_security_version(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        promotion = (
            ROOT / ".github" / "workflows" / "promote-release.yml"
        ).read_text(encoding="utf-8")
        gate = "Gate exact GitHub CLI security baseline"

        self.assertNotIn("GH_REQUIRED_VERSION", release)
        self.assertNotIn("GH_REQUIRED_VERSION", promotion)
        self.assertNotIn("GH_REQUIRED_LINUX_AMD64_SHA256", release)
        self.assertNotIn("GH_REQUIRED_LINUX_AMD64_SHA256", promotion)
        self.assertEqual(release.count(gate), 4)
        self.assertEqual(promotion.count(gate), 2)
        self.assertEqual(release.count(f"uses: {PINNED_GH_ACTION}"), 4)
        self.assertEqual(promotion.count(f"uses: {PINNED_GH_ACTION}"), 2)

    def test_pinned_github_cli_action_is_digest_bound_and_fail_closed(self):
        action = yaml.load(
            (
                ROOT / ".github" / "actions" / "setup-pinned-gh-cli" / "action.yml"
            ).read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        source = (
            ROOT / ".github" / "actions" / "setup-pinned-gh-cli" / "setup.sh"
        ).read_text(encoding="utf-8")

        self.assertEqual(action["runs"]["using"], "composite")
        self.assertNotIn("inputs", action)
        install_step = action["runs"]["steps"][0]
        self.assertEqual(install_step["env"]["GH_CLI_VERSION"], PINNED_GH_VERSION)
        self.assertEqual(
            install_step["env"]["GH_CLI_LINUX_AMD64_SHA256"],
            PINNED_GH_LINUX_AMD64_SHA256,
        )
        self.assertIn('test "${RUNNER_OS:-}" = "Linux"', source)
        self.assertIn('test "${RUNNER_ARCH:-}" = "X64"', source)
        self.assertIn("--proto '=https'", source)
        self.assertIn("sha256sum --check --strict", source)
        self.assertIn('test "$actual_version" = "$GH_CLI_VERSION"', source)
        self.assertNotIn("sudo", source)

        bash = _bash_path()
        self.assertIsNotNone(bash, "Pinned GitHub CLI setup requires bash")
        syntax = subprocess.run(
            [bash, "-n", str(ROOT / ".github/actions/setup-pinned-gh-cli/setup.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX archive fixture is validated in CI")
    def test_pinned_github_cli_action_accepts_only_the_bound_archive(self):
        bash = _bash_path()
        self.assertIsNotNone(bash, "Pinned GitHub CLI setup requires bash")
        setup = ROOT / ".github/actions/setup-pinned-gh-cli/setup.sh"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fixture_root = temp / f"gh_{PINNED_GH_VERSION}_linux_amd64"
            fixture_bin = fixture_root / "bin"
            fixture_bin.mkdir(parents=True)
            fixture_gh = fixture_bin / "gh"
            fixture_gh.write_text(
                f"#!/usr/bin/env bash\nprintf 'gh version {PINNED_GH_VERSION} (fixture)\\n'\n",
                encoding="utf-8",
            )
            fixture_gh.chmod(0o755)

            archive = temp / "fixture.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(fixture_root, arcname=fixture_root.name)
            archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()

            fake_bin = temp / "fake-bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
output=""
while (( $# )); do
  if [[ "$1" = "--output" ]]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
test -n "$output"
cp "$FIXTURE_ARCHIVE" "$output"
""",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)

            def run_setup(expected_sha256: str, runner_temp: Path, github_path: Path):
                env = os.environ.copy()
                env.update(
                    {
                        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                        "RUNNER_OS": "Linux",
                        "RUNNER_ARCH": "X64",
                        "RUNNER_TEMP": str(runner_temp),
                        "GITHUB_PATH": str(github_path),
                        "GH_CLI_VERSION": PINNED_GH_VERSION,
                        "GH_CLI_LINUX_AMD64_SHA256": expected_sha256,
                        "FIXTURE_ARCHIVE": str(archive),
                    }
                )
                runner_temp.mkdir()
                github_path.touch()
                return subprocess.run(
                    [bash, str(setup)],
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )

            valid_path = temp / "github-path-valid"
            valid = run_setup(archive_sha256, temp / "runner-valid", valid_path)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            installed_bin = Path(valid_path.read_text(encoding="utf-8").strip())
            self.assertTrue((installed_bin / "gh").is_file())

            invalid_path = temp / "github-path-invalid"
            invalid = run_setup("0" * 64, temp / "runner-invalid", invalid_path)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(invalid_path.read_text(encoding="utf-8"), "")

    def test_platform_qualification_is_hosted_scoped_and_injected_exactly(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        promotion = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(
            encoding="utf-8"
        )
        job = release["jobs"]["platform-qualification"]

        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(job["if"], "${{ inputs.operation == 'qualify' }}")
        self.assertEqual(
            job["needs"], ["preflight", "full-ci", "full-release-gate"]
        )
        qualification = source[
            source.index("  platform-qualification:\n") : source.index(
                "  release-authority:\n"
            )
        ]
        for exact_identity_guard in (
            'test "$GITHUB_ACTIONS" = "true"',
            'test "$RUNNER_OS" = "Linux"',
            'test "$RUNNER_ARCH" = "X64"',
            'test "$GITHUB_SHA" = "$CANDIDATE_SHA"',
            'test "$GITHUB_WORKFLOW_SHA" = "$CANDIDATE_SHA"',
            'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"',
        ):
            self.assertIn(exact_identity_guard, qualification)
        for real_rehearsal in (
            "installer.tests.test_runtime",
            "from installer.production import build_runtime",
            "deploy/docker-compose.yml config --quiet",
            "scripts.tests.test_restore_postgres",
            "scripts.tests.test_migration_postgres",
            "updater.tests.test_adoption",
            "updater.tests.test_linux_e2e",
            "scripts.tests.test_durability_doctor",
        ):
            self.assertIn(real_rehearsal, qualification)
        for marker in (
            "fresh_install",
            "logical_restore",
            "logical_migration",
            "updater_handoff",
            "doctor_complete",
        ):
            self.assertIn(f'$REHEARSAL_DIR/{marker}', qualification)
        for observed_capability in (
            "postgresql-client-16",
            "platform_qualification.py collect",
            '--postgres-image "$POSTGRES_IMAGE"',
            '--redis-image "$REDIS_IMAGE"',
            '--source-database-url "$ANIMEMO_TEST_DATABASE_URL"',
            '--target-database-url "$ANIMEMO_RESTORE_TEST_DATABASE_URL"',
            '--rehearsal-directory "$REHEARSAL_DIR"',
            "platform-qualification-${{ github.run_id }}",
        ):
            self.assertIn(observed_capability, qualification)

        self.assertEqual(
            source.count("release/platform-qualification.json | cmp - release/platform-qualification.json"),
            1,
        )
        self.assertGreaterEqual(
            source.count("platform_qualification.py verify"), 4
        )
        self.assertIn(
            "path: ${{ runner.temp }}/animemo-release-qualification-output/",
            source,
        )
        self.assertIn(
            "install -m 0600 platform-qualification-input/platform-qualification.json", source
        )
        self.assertIn("release-qualification/platform-qualification.json", source)
        self.assertIn(
            "validated-release-input-${{ github.run_id }}", source
        )
        self.assertIn(
            "cp --no-clobber rc-assets/installer-materials.tar promotion-output/installer-materials.tar",
            promotion,
        )
        self.assertNotIn("platform_qualification.py collect", promotion)
        self.assertNotIn("build-installer-materials", promotion)

    def test_platform_qualification_waits_for_stable_published_postgres(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        qualification = source[
            source.index("  platform-qualification:\n") : source.index(
                "  release-authority:\n"
            )
        ]
        self.assertEqual(
            qualification.count("bash scripts/wait-for-stable-postgres.sh"), 1
        )
        self.assertIn(
            "timeout --foreground --signal=TERM --kill-after=5s 10m",
            qualification,
        )
        self.assertIn("PGPASSWORD: qualification-only", qualification)
        self.assertNotIn("docker exec", qualification)

    def test_exact_image_rehearsal_is_runner_scoped_and_read_only(self):
        source = (ROOT / "scripts" / "rehearse-release-images.sh").read_text(encoding="utf-8")

        self.assertIn('--confirm-isolated', source)
        self.assertIn('${GITHUB_ACTIONS:-}', source)
        self.assertIn('$RUNNER_TEMP/animemo-release-images.', source)
        self.assertIn('down -v --remove-orphans', source)
        self.assertIn('scripts/ci_first_run.py', source)
        self.assertIn('--code-stdin', source)
        self.assertNotIn('docker push', source)
        self.assertNotIn('docker system prune', source)
        self.assertNotIn('docker builder prune', source)

    def test_exact_image_rehearsal_closes_the_required_configuration_revision(self):
        source = (ROOT / "scripts" / "rehearse-release-images.sh").read_text(
            encoding="utf-8"
        )
        revision = "33333333-3333-4333-8333-333333333333"

        self.assertEqual(source.count("ANIMEMO_CONFIG_REVISION"), 2)
        self.assertIn(f"ANIMEMO_CONFIG_REVISION={revision}", source)
        self.assertIn('export ANIMEMO_CONFIG_REVISION="33333333-3333-4333-8333-333333333333"', source)
        self.assertLess(
            source.index(f"ANIMEMO_CONFIG_REVISION={revision}"),
            source.index('"${COMPOSE[@]}" config --quiet'),
        )

    def test_exact_image_rehearsal_closes_every_required_compose_variable(self):
        source = (ROOT / "scripts" / "rehearse-release-images.sh").read_text(
            encoding="utf-8"
        )
        compose_sources = (
            ROOT / "deploy" / "docker-compose.yml",
            ROOT / "updater" / "docker-compose.runtime.yml",
            ROOT / "deploy" / "docker-compose.upgrade-gate.yml",
        )
        required = set()
        for path in compose_sources:
            path_required = set(
                re.findall(
                    r"\$\{([A-Z0-9_]+):\?[^}]+\}",
                    path.read_text(encoding="utf-8"),
                )
            )
            self.assertTrue(path_required, path)
            required.update(path_required)
        env_file = re.search(
            r'cat > "\$ENV_FILE" <<EOF\n(?P<body>.*?)\nEOF',
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(env_file)
        bindings = set(
            re.findall(r"^([A-Z0-9_]+)=", env_file.group("body"), re.MULTILINE)
        )
        if "release.registry_transport pull-all --projection compose-env" in source:
            bindings.update(
                {"ANIMEMO_POSTGRES_IMAGE", "ANIMEMO_REDIS_IMAGE"}
            )

        self.assertSetEqual(required - bindings, set())
        self.assertLess(
            env_file.end(),
            source.index('"${COMPOSE[@]}" config --quiet'),
        )

    def test_exact_image_rehearsal_ignores_dependency_image_environment_overrides(self):
        bash = _bash_path()
        if bash is None:
            self.skipTest("Git Bash or bash is required for the rehearsal CLI gate.")

        for name in (
            "dr-rehearsal.sh",
            "stateful-upgrade-gate.sh",
            "rehearse-release-images.sh",
        ):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            unset = source.index(
                "unset ANIMEMO_POSTGRES_IMAGE ANIMEMO_REDIS_IMAGE"
            )
            projection = source.index(
                "release.registry_transport pull-all --projection compose-env"
            )
            with self.subTest(script=name):
                self.assertLess(unset, projection)

        script = (ROOT / "scripts" / "rehearse-release-images.sh").as_posix()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            marker = root / "docker-environment.txt"
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' "
                "'ANIMEMO_POSTGRES_IMAGE=registry.example/postgres@sha256:"
                + "a" * 64
                + "' "
                "'ANIMEMO_REDIS_IMAGE=registry.example/redis@sha256:"
                + "b" * 64
                + "' "
                "'DEPENDENCY_IMAGE_AUTHORITY_SHA256=sha256:"
                + "c" * 64
                + "'\n",
                encoding="utf-8",
                newline="\n",
            )
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s|%s\\n' "
                "\"${ANIMEMO_POSTGRES_IMAGE-UNSET}\" "
                "\"${ANIMEMO_REDIS_IMAGE-UNSET}\" >> \"$DOCKER_ENV_MARKER\"\n"
                "exit 42\n",
                encoding="utf-8",
                newline="\n",
            )
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text(
                "#!/usr/bin/env bash\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            for command in (fake_python, fake_docker, fake_sudo):
                command.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "GITHUB_ACTIONS": "true",
                    "RUNNER_TEMP": "/tmp",
                    "ANIMEMO_POSTGRES_IMAGE": "registry.invalid/postgres:latest",
                    "ANIMEMO_REDIS_IMAGE": "registry.invalid/redis:latest",
                    "DOCKER_ENV_MARKER": marker.as_posix(),
                    "PATH": str(fake_bin) + os.pathsep + environment["PATH"],
                }
            )
            completed = subprocess.run(
                [
                    bash,
                    script,
                    "--api-image",
                    "api@sha256:test",
                    "--web-image",
                    "web@sha256:test",
                    "--version",
                    "v1.1.0-rc.1",
                    "--commit",
                    "c" * 40,
                    "--channel",
                    "rc",
                    "--confirm-isolated",
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 42, completed.stderr)
            observations = marker.read_text(encoding="utf-8").splitlines()
            self.assertTrue(observations)
            self.assertTrue(all(item == "UNSET|UNSET" for item in observations))

    def test_exact_image_rehearsal_trusts_only_the_runtime_web_proxy(self):
        source = (ROOT / "scripts" / "rehearse-release-images.sh").read_text(encoding="utf-8")

        self.assertIn("TRUSTED_PROXY_IPS=127.0.0.1/32", source)
        self.assertIn(".NetworkSettings.Networks", source)
        self.assertIn('print(f"{address}/32")', source)
        self.assertIn("TRUSTED_PROXY_CIDR", source)
        self.assertIn(
            "--pull never --no-deps --force-recreate --wait --wait-timeout 120 api",
            source,
        )
        self.assertIn("AdminAuditLog.objects.get(action='installation.initialized').ip_address", source)
        self.assertIn("recorded_ip == proxy_ip", source)
        self.assertNotIn("TRUSTED_PROXY_IPS=172.16.0.0/12", source)

    def test_stable_notes_derive_from_the_frozen_rc_snapshot(self):
        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        self.assertIn("previous-stable", source)
        self.assertIn("promote-release-notes", source)
        self.assertIn("--rc-notes promotion-output/rc-release-notes.json", source)
        self.assertIn("--stable-notes-markdown promotion-output/release-notes.md", source)
        self.assertNotIn("--generate-notes", source)

    def test_stable_promotion_has_no_image_build_and_requires_acceptance(self):
        promotion_path = ROOT / ".github" / "workflows" / "promote-release.yml"
        source = promotion_path.read_text(encoding="utf-8")
        promotion = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertEqual(set(promotion["on"]), {"workflow_dispatch"})
        self.assertNotIn("acceptance_confirmation", promotion["on"]["workflow_dispatch"]["inputs"])
        self.assertIn("dry_run", promotion["on"]["workflow_dispatch"]["inputs"])
        self.assertIn('acceptance_path="release/acceptance-records/$RC_TAG.json"', source)
        self.assertIn("git ls-files --error-unmatch", source)
        self.assertIn("scripts/rc_live_acceptance.py", source)
        self.assertNotIn("docker/build-push-action", source)
        self.assertNotIn("docker build", source)
        self.assertIn("RC_COMMIT == STABLE_COMMIT", source)
        self.assertIn("RC_API_DIGEST == STABLE_API_DIGEST", source)
        self.assertIn("RC_WEB_DIGEST == STABLE_WEB_DIGEST", source)
        promote_call = source[
            source.index("python -m release.cli promote-manifest") :
            source.index("--output promotion-output/release-manifest.json")
        ]
        self.assertNotIn("--created-at", promote_call)
        self.assertNotIn("--provenance-source-commit", promote_call)
        self.assertIn("--existing-stable-tag-commit", promote_call)
        self.assertIn('git rev-parse "refs/tags/$stable_tag^{commit}"', source)
        self.assertNotIn("date -u", promote_call)
        self.assertNotIn("GITHUB_SHA", promote_call)

    def test_stable_promotion_dry_run_checks_out_before_downloading_artifact(self):
        promotion = workflow("promote-release.yml")
        steps = promotion["jobs"]["dry-run"]["steps"]
        checkout = next(
            index
            for index, step in enumerate(steps)
            if step.get("uses")
            == f"actions/checkout@{PINNED_RELEASE_ACTIONS['actions/checkout']}"
        )
        download = next(
            index
            for index, step in enumerate(steps)
            if step.get("uses")
            == f"actions/download-artifact@{PINNED_RELEASE_ACTIONS['actions/download-artifact']}"
        )

        self.assertLess(checkout, download)

    def test_stable_promotion_revalidates_authority_before_external_mutation(self):
        promotion = workflow("promote-release.yml")
        publish = promotion["jobs"]["publish"]
        self.assertEqual(publish["steps"][0]["with"]["ref"], "${{ github.sha }}")

        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(encoding="utf-8")
        publish_source = source[source.index("  publish:\n") :]
        first_mutation = publish_source.index(
            "python -m scripts.release_publication transaction-reconcile"
        )
        before_first_mutation = publish_source[:first_mutation]
        for guard in (
            'test "$(git rev-parse origin/main)" = "$GITHUB_SHA"',
            'test "$(gh api "repos/$GITHUB_REPOSITORY/immutable-releases" --jq \'.enabled\')" = "true"',
            'cmp "$RUNNER_TEMP/revalidated-stable-publication-plan.json"',
        ):
            self.assertIn(guard, before_first_mutation)
        self.assertNotIn("crane tag", publish_source)
        self.assertNotIn('git push origin "refs/tags/', publish_source)

    def test_stable_registry_prestate_is_batch_checked_before_zero_or_two_mutations(self):
        promotion = workflow("promote-release.yml")
        steps = promotion["jobs"]["publish"]["steps"]
        prestate = next(
            step
            for step in steps
            if step.get("name")
            == "Reconcile the complete Stable transaction before its first mutation"
        )
        mutation = next(
            step
            for step in steps
            if step.get("name")
            == "Add only absent Stable registry tags through the durable controller"
        )
        prestate_source = prestate["run"]
        mutation_source = mutation["run"]

        self.assertLess(steps.index(prestate), steps.index(mutation))
        self.assertNotIn("crane tag", prestate_source)
        self.assertIn("transaction-reconcile", prestate_source)
        self.assertIn("transaction-run", mutation_source)
        self.assertIn("--phase registry", mutation_source)
        self.assertNotIn("crane tag", mutation_source)
        remote = (ROOT / "release" / "publication_remote.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('name = f"registry-{role}-stable"', remote)
        self.assertIn('source_reference=f"{image_repository}@{digest}"', remote)
        controller = (ROOT / "release" / "publication_transaction.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("Observe every key before any mutation", controller)
        self.assertIn("TRANSACTION_GLOBAL_FREEZE", controller)

    def test_rc_publication_is_draft_upload_verify_publish_with_qualified_notes(self):
        release = workflow("release.yml")
        names = [step.get("name", "") for step in release["jobs"]["publish"]["steps"]]
        expected = (
            "Generate the closed publication plan without mutation",
            "Reconcile the complete RC transaction before its first mutation",
            "Publish the four exact Candidate registry keys through the durable controller",
            "Commit RC tag, Draft, individual assets, and publish through one controller",
            "Finalize and export the durable RC transaction receipt",
            "Preserve the finalized RC publication transaction before post-publication checks",
            "Verify the public RC without authenticated asset transport",
        )
        indices = [names.index(name) for name in expected]
        self.assertEqual(indices, sorted(indices))
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--generate-notes", source)
        self.assertIn("release_notes.snapshot_identity", source)
        self.assertIn("RELEASE_NOTES_MARKDOWN_SHA256", source)
        self.assertIn("release-notes.md", source)
        self.assertIn("env -u GH_TOKEN curl", source)
        publish = source[source.index("  publish:\n") :]
        self.assertNotIn('gh api --method POST "repos/$GITHUB_REPOSITORY/releases"', publish)
        self.assertNotIn('gh release upload "$RELEASE_TAG"', publish)
        self.assertIn("transaction-run", publish)
        self.assertIn("--phase publication", publish)
        file_attests = [
            step
            for step in release["jobs"]["publish"]["steps"]
            if isinstance(step.get("with"), dict) and "subject-path" in step["with"]
        ]
        self.assertEqual(len(file_attests), 3)
        self.assertTrue(
            all(step["with"].get("create-storage-record") == "true" for step in file_attests)
        )
        all_attests = [
            step
            for step in release["jobs"]["publish"]["steps"]
            if isinstance(step.get("with"), dict)
            and (
                "subject-path" in step["with"]
                or "subject-digest" in step["with"]
            )
        ]
        self.assertEqual(len(all_attests), 5)
        self.assertTrue(
            all(step["with"].get("create-storage-record") == "true" for step in all_attests)
        )
        self.assertEqual(
            release["jobs"]["publish"]["permissions"].get("artifact-metadata"),
            "write",
        )
        receipt_upload = next(
            step
            for step in release["jobs"]["publish"]["steps"]
            if step.get("name")
            == "Preserve the finalized RC publication transaction before post-publication checks"
        )
        self.assertEqual(
            receipt_upload["with"]["path"],
            "release-output/publication-transaction-ledger.json",
        )
        remote = (ROOT / "release" / "publication_remote.py").read_text(
            encoding="utf-8"
        )
        for adapter in (
            "GitTagAdapter",
            "GitHubDraftAdapter",
            "GitHubAssetAdapter",
            "GitHubPublishAdapter",
        ):
            self.assertIn(adapter, remote)

    def test_stable_publication_uses_the_same_draft_transaction_and_never_rebuilds(self):
        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(
            encoding="utf-8"
        )
        publish_source = source[source.index("  publish:\n") :]
        self.assertIn(
            "Commit Stable tag, Draft, individual assets, and publish through one controller",
            source,
        )
        self.assertIn("Finalize and export the durable Stable transaction receipt", source)
        self.assertIn(
            "Preserve the finalized Stable publication transaction before post-publication checks",
            source,
        )
        self.assertIn("Verify the public Stable release without authenticated asset transport", source)
        self.assertNotIn("docker/build-push-action", source)
        self.assertNotIn("docker build", source)
        self.assertIn("plan-stable-publication-files", source)
        self.assertIn(
            "--promotion-acceptance promotion-output/stable-promotion-acceptance.json",
            source,
        )
        self.assertIn("--rc-manifest rc-assets/release-manifest.json", source)
        self.assertNotIn("crane tag", publish_source)
        self.assertNotIn('gh release upload "$STABLE_TAG"', publish_source)
        self.assertIn("--phase publication", publish_source)
        promotion = workflow("promote-release.yml")
        file_attests = [
            step
            for step in promotion["jobs"]["publish"]["steps"]
            if isinstance(step.get("with"), dict) and "subject-path" in step["with"]
        ]
        self.assertEqual(len(file_attests), 3)
        self.assertTrue(
            all(step["with"].get("create-storage-record") == "true" for step in file_attests)
        )
        self.assertEqual(
            promotion["jobs"]["publish"]["permissions"].get("artifact-metadata"),
            "write",
        )
        receipt_upload = next(
            step
            for step in promotion["jobs"]["publish"]["steps"]
            if step.get("name")
            == "Preserve the finalized Stable publication transaction before post-publication checks"
        )
        self.assertEqual(
            receipt_upload["with"]["path"],
            "promotion-output/publication-transaction-ledger.json",
        )


    def test_rc_presentation_is_plan_derived_and_guarded_before_each_mutation(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[source.index("  publish:\n") :]

        self.assertEqual(publish.count("emit-publication-presentation"), 1)
        self.assertLess(
            publish.index("emit-publication-presentation"),
            publish.index("--phase publication"),
        )
        self.assertNotIn("steps.presentation.outputs", publish)
        remote = (ROOT / "release" / "publication_remote.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("title=tag", remote)
        self.assertIn("subject=tag", remote)
        self.assertIn("body=body", remote)
        self.assertIn('"git-tag"', remote)
        self.assertIn('"release-draft"', remote)
        self.assertIn('"release-publish"', remote)
        post_guard = publish.index("--repository . --state published")
        post_verification = publish.index("verify-post-publish")
        self.assertLess(post_guard, post_verification)

    def test_stable_presentation_uses_the_shared_validator_and_rejects_bad_source_rc(
        self,
    ):
        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[source.index("  publish:\n") :]
        plan = source[source.index("  plan:\n") : source.index("  dry-run:\n")]

        self.assertEqual(publish.count("emit-stable-presentation"), 1)
        self.assertEqual(plan.count("verify-stable-source-presentation"), 1)
        self.assertLess(
            plan.index("verify-stable-source-presentation"),
            plan.index("docker/login-action"),
        )
        self.assertEqual(publish.count("verify-stable-source-presentation"), 1)
        self.assertLess(
            publish.index("verify-stable-source-presentation"),
            publish.index("transaction-reconcile"),
        )
        self.assertNotIn("steps.presentation.outputs", publish)
        self.assertNotIn("crane tag", publish)
        self.assertNotIn('git push origin "refs/tags/', publish)
        remote = (ROOT / "release" / "publication_remote.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("prerelease = plan[\"channel\"] != \"stable\"", remote)
        self.assertIn("title=tag", remote)
        self.assertIn("subject=tag", remote)
        self.assertLess(
            publish.index("--repository . --state published"),
            publish.index("verify-post-publish"),
        )

    def test_production_workflows_have_no_duplicate_prefix_formatter_or_shell_eval(
        self,
    ):
        production = "\n".join(
            (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            for name in ("release.yml", "promote-release.yml")
        )
        for forbidden in (
            "AniMemo $RELEASE_TAG",
            "AniMemo ${RELEASE_TAG}",
            "AniMemo $STABLE_TAG",
            "AniMemo ${STABLE_TAG}",
            '--title "AniMemo',
            '--message "AniMemo',
            "eval ",
            "bash -c",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, production)
        self.assertNotIn(
            "release_title:",
            production.split("workflow_dispatch:", 1)[1].split("jobs:", 1)[0],
        )
        self.assertNotIn(
            "annotated_tag_subject:",
            production.split("workflow_dispatch:", 1)[1].split("jobs:", 1)[0],
        )

    def test_metadata_freshness_workflow_has_three_inputs_and_read_only_permissions(self):
        freshness = workflow("release-metadata-freshness.yml")
        inputs = freshness["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(inputs), {
                "qualification_run_id",
                "intended_main_sha",
                "candidate_acceptance_receipt_b64url",
            }
        )
        self.assertEqual(
            freshness["permissions"],
            {"contents": "read", "actions": "read"},
        )
        self.assertEqual(
            freshness["concurrency"]["group"],
            "animemo-release-metadata-freshness-"
            "${{ inputs.intended_main_sha }}-${{ inputs.qualification_run_id }}",
        )
        self.assertEqual(freshness["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(freshness["name"], "Release Metadata Freshness")
        self.assertEqual(set(freshness["on"]), {"workflow_dispatch"})

    def test_metadata_freshness_workflow_rejects_arbitrary_authority_inputs(self):
        source = (
            ROOT / ".github" / "workflows" / "release-metadata-freshness.yml"
        ).read_text(encoding="utf-8")
        header = source[: source.index("jobs:")]
        for forbidden in (
            "release_title:",
            "tag_subject:",
            "release_notes_identity:",
            "markdown_sha:",
            "pull_requests:",
            "api_url:",
            "repository:",
            "workflow_path:",
            "target_version_override:",
            "freshness_passed:",
        ):
            self.assertNotIn(forbidden, header)
        self.assertNotIn("contents: write", source)
        self.assertNotIn("packages: write", source)
        self.assertNotIn("id-token: write", source)
        self.assertNotIn("administration: write", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("eval ", source)

    def test_metadata_freshness_authenticates_exact_qualification_and_artifact(self):
        source = (
            ROOT / ".github" / "workflows" / "release-metadata-freshness.yml"
        ).read_text(encoding="utf-8")
        for guard in (
            "validate-qualification-run-metadata",
            '--run-metadata "$RUNNER_TEMP/qualification-run.json"',
            '--jobs-metadata "$RUNNER_TEMP/qualification-jobs.json"',
            '--artifacts-metadata "$RUNNER_TEMP/qualification-artifacts.json"',
            '--expected-run-id "$QUALIFICATION_RUN_ID"',
            '--expected-sha "$INTENDED_MAIN_SHA"',
            "extract-qualification-artifact",
            'test "$(jq -r \'.candidateTreeSha\' "$prepublication")" = "$candidate_tree"',
        ):
            self.assertIn(guard, source)
        module = (ROOT / "release" / "metadata_freshness.py").read_text()
        for guard in (
            "QUALIFICATION_WORKFLOW_NAME = \"Release Producer\"",
            'job.get("name") == "phase-a-qualification-evidence"',
            'job.get("name") == "publish-immutable-prerelease"',
            'job.get("conclusion") == "skipped"',
            "artifact.get(\"expired\") is not False",
            "fileCount",
        ):
            self.assertIn(guard, module)

    def test_metadata_freshness_reads_back_one_frozen_authority_into_exact_artifact(self):
        source = (
            ROOT / ".github" / "workflows" / "release-metadata-freshness.yml"
        ).read_text(encoding="utf-8")
        module = (ROOT / "release" / "metadata_freshness.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("MINIMUM_SNAPSHOT_INTERVAL_SECONDS = 0", module)
        self.assertIn('test "$GITHUB_WORKFLOW_SHA" = "$INTENDED_MAIN_SHA"', source)
        self.assertIn('"releaseNotesAuthorityProducerCount": 1', module)
        self.assertIn('"livePrLabelQueryCount": 0', module)
        self.assertNotIn("runtime_clock.sleep(MINIMUM_SNAPSHOT_INTERVAL_SECONDS)", module)
        self.assertIn("release-metadata-freshness-${{ github.run_id }}", source)
        self.assertIn('test "$(find "$directory" -mindepth 1 -maxdepth 1 -type f | wc -l)" = "10"', source)
        self.assertIn("candidate-acceptance-receipt.json", source)
        self.assertIn("--candidate-acceptance-receipt-sha256", source)
        self.assertIn("credential-shaped material detected", source)

    def test_metadata_freshness_consumes_only_frozen_qualification_authority(self):
        freshness = workflow("release-metadata-freshness.yml")
        source = (
            ROOT / ".github" / "workflows" / "release-metadata-freshness.yml"
        ).read_text(encoding="utf-8")
        module = (ROOT / "release" / "metadata_freshness.py").read_text(
            encoding="utf-8"
        )
        collection = source[
            source.index("collect-metadata-freshness") - 500 :
            source.index("collect-metadata-freshness") + 1500
        ]

        self.assertEqual(
            freshness["permissions"], {"contents": "read", "actions": "read"}
        )
        for name in (
            "release-notes-input.json",
            "release-notes.json",
            "release-notes.md",
            "release-notes-readback.json",
            "release-notes-preflight.json",
        ):
            self.assertIn(name, source)
        self.assertNotIn("GITHUB_TOKEN", collection)
        self.assertNotIn("pull-requests: read", source)
        self.assertNotIn("runtime_clock.sleep(MINIMUM_SNAPSHOT_INTERVAL_SECONDS)", module)

    def test_publish_authenticates_freshness_producer_bindings_and_ttl(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        authority = release["jobs"]["metadata-freshness-authority"]
        self.assertEqual(authority["runs-on"], "ubuntu-24.04")
        self.assertEqual(authority["permissions"], {"contents": "read", "actions": "read"})
        self.assertEqual(authority["needs"], ["preflight", "release-authority"])
        for guard in (
            "validate-freshness-run-metadata",
            '--run-metadata "$RUNNER_TEMP/freshness-run.json"',
            '--artifacts-metadata "$RUNNER_TEMP/freshness-artifacts.json"',
            '--expected-run-id "$FRESHNESS_RUN_ID"',
            '--expected-sha "$INTENDED_MAIN_SHA"',
            '--expected-qualification-run-id "$QUALIFICATION_RUN_ID"',
            '--expected-candidate-sha "$INTENDED_MAIN_SHA"',
            "extract-metadata-freshness-artifact",
        ):
            self.assertIn(guard, source)
        module = (ROOT / "release" / "metadata_freshness.py").read_text()
        self.assertIn('FRESHNESS_WORKFLOW_NAME = "Release Metadata Freshness"', module)
        self.assertIn('value.get("event") != "workflow_dispatch"', module)
        self.assertIn('value.get("run_attempt") != 1', module)
        publish = source[source.index("  publish:\n") :]
        self.assertEqual(publish.count("verify-metadata-freshness"), 2)
        self.assertIn("METADATA_FRESHNESS_EXPIRED", module)

    def test_candidate_runtime_bytes_and_publish_receipt_are_fail_closed(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dry_run = source[source.index("  dry-run:\n") : source.index("  publish:\n")]
        qualification = dry_run
        publish = source[source.index("  publish:\n") :]
        self.assertEqual(dry_run.count("outputs: type=oci"), 2)
        self.assertEqual(dry_run.count("normalize-candidate-oci-layout"), 4)
        self.assertIn('local layout="$root/oci/$role"', dry_run)
        self.assertIn('test ! -e "$layout" && test ! -L "$layout"', dry_run)
        self.assertIn('crane pull "$reference" "$layout" --format=oci', dry_run)
        self.assertIn('test -d "$layout" && test ! -L "$layout"', dry_run)
        self.assertIn(
            'test -f "$layout/oci-layout" && test ! -L "$layout/oci-layout"',
            dry_run,
        )
        self.assertIn(
            'test -f "$layout/index.json" && test ! -L "$layout/index.json"',
            dry_run,
        )
        self.assertIn('test -d "$layout/blobs" && test ! -L "$layout/blobs"', dry_run)
        self.assertNotIn('local archive="$RUNNER_TEMP/$role.oci.tar"', dry_run)
        self.assertNotIn("extract-candidate-oci-archive", dry_run)
        self.assertIn("build-prepublication-candidate-input", qualification)
        self.assertIn("PLATFORM_ARTIFACT_DIGEST", qualification)
        self.assertIn("DRY_RUN_ARTIFACT_DIGEST", qualification)
        self.assertIn("release-qualification/candidate-runtime", qualification)
        self.assertNotIn(".dockerbuild", qualification)
        self.assertEqual(source.count("--require-candidate-contract"), 1)
        freshness_source = (
            ROOT / ".github" / "workflows" / "release-metadata-freshness.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(freshness_source.count("--require-candidate-contract"), 1)
        self.assertIn("candidate_acceptance_receipt_b64url", source[: source.index("jobs:")])
        self.assertIn('test -n "$CANDIDATE_ACCEPTANCE_RECEIPT_B64URL"', source)
        self.assertEqual(
            publish.count("--expected-candidate-acceptance-receipt-sha256"), 2
        )
        self.assertEqual(publish.count("--expected-candidate-version"), 2)
        self.assertNotIn("continue-on-error", publish)

    def test_qualification_producer_uses_only_explicit_run_authority(self):
        release = workflow("release.yml")
        qualification_step = next(
            step
            for step in release["jobs"]["dry-run"]["steps"]
            if step.get("name")
            == "Generate qualification evidence from the single Candidate byte producer"
        )
        qualification = qualification_step["run"]

        self.assertIn(
            'install -m 0600 "$QUALIFICATION_ARTIFACT_PATH"', qualification
        )
        self.assertIn(
            '"release-qualification/release-qualification-${RUN_ID}.json"',
            qualification,
        )
        self.assertIn('--run-id "$RUN_ID"', qualification)
        self.assertIn('--run-attempt "$RUN_ATTEMPT"', qualification)
        self.assertIn('--qualification-run-id "$RUN_ID"', qualification)
        self.assertIn('--qualification-run-attempt "$RUN_ATTEMPT"', qualification)
        self.assertNotIn("$GITHUB_RUN_ID", qualification)
        self.assertNotIn("$GITHUB_RUN_ATTEMPT", qualification)
        self.assertEqual(qualification_step["env"]["RUN_ID"], "${{ github.run_id }}")
        self.assertEqual(
            qualification_step["env"]["RUN_ATTEMPT"], "${{ github.run_attempt }}"
        )

    def test_publish_consumes_the_candidate_accepted_bytes_without_rebuilding(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[source.index("  publish:\n") :]
        authority = source[
            source.index("  release-authority:\n") :
            source.index("  metadata-freshness-authority:\n")
        ]

        self.assertNotIn("docker/build-push-action", publish)
        self.assertNotIn("docker build", publish)
        self.assertNotIn("generate-manifest", publish)
        for required in (
            "candidate-input.json",
            "verified-candidate.json",
            "candidate-runtime",
            "release-manifest.json",
        ):
            self.assertIn(required, authority)
            self.assertIn(required, publish)
        self.assertIn('chmod 0700 "$candidate_state"', publish)
        self.assertIn(
            'find "$candidate_state" -mindepth 1 -maxdepth 1 -type d', publish
        )
        self.assertIn("PUBLISH_CANDIDATE_BYTE_MISMATCH", publish)
        mismatch_guard = publish.index("PUBLISH_CANDIDATE_BYTE_MISMATCH")
        first_mutation = publish.index(
            "python -m scripts.release_publication transaction-reconcile"
        )
        self.assertLess(mismatch_guard, first_mutation)

    def test_qualification_artifact_outputs_use_canonical_sha256_identity(self):
        release = workflow("release.yml")

        self.assertEqual(
            release["jobs"]["platform-qualification"]["outputs"]["artifact_digest"],
            "${{ format('sha256:{0}', "
            "steps.platform_artifact.outputs.artifact-digest) }}",
        )
        self.assertEqual(
            release["jobs"]["dry-run"]["outputs"]["artifact_digest"],
            "${{ format('sha256:{0}', "
            "steps.qualification_artifact.outputs.artifact-digest) }}",
        )
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        qualification = source[source.index("  dry-run:\n") : source.index("  publish:\n")]
        self.assertIn(
            '[[ "$PLATFORM_ARTIFACT_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]',
            qualification,
        )
        self.assertIn(
            '[[ "$DRY_RUN_ARTIFACT_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]',
            qualification,
        )

    def test_every_external_release_mutation_is_after_the_freshness_gate(self):
        release = workflow("release.yml")
        publish = release["jobs"]["publish"]
        self.assertIn("metadata-freshness-authority", publish["needs"])
        steps = publish["steps"]
        freshness_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name")
            == "Verify metadata freshness TTL immediately before external publication"
        )
        mutation_indices = []
        mutation_fragments = (
            "docker push",
            "crane push",
            "python -m release.cli build-portable",
            "git tag --annotate",
            'git push origin "refs/tags/',
            'gh api --method POST "repos/$GITHUB_REPOSITORY/releases"',
            'gh release upload "$RELEASE_TAG"',
            'gh api --method PATCH "repos/$GITHUB_REPOSITORY/releases/',
        )
        for index, step in enumerate(steps):
            body = str(step.get("run", ""))
            action = str(step.get("uses", ""))
            action_push = action.startswith("docker/build-push-action@") and str(
                step.get("with", {}).get("push", "")
            ).lower() == "true"
            if (
                any(fragment in body for fragment in mutation_fragments)
                or action.startswith("actions/attest@")
                or action_push
            ):
                mutation_indices.append(index)
        self.assertTrue(mutation_indices)
        self.assertTrue(all(index > freshness_index for index in mutation_indices))
        for job_name, job in release["jobs"].items():
            if job_name == "publish":
                continue
            for step in job.get("steps", []):
                body = str(step.get("run", ""))
                action = str(step.get("uses", ""))
                action_push = action.startswith("docker/build-push-action@") and str(
                    step.get("with", {}).get("push", "")
                ).lower() == "true"
                self.assertFalse(
                    any(fragment in body for fragment in mutation_fragments)
                    or action.startswith("actions/attest@")
                    or action_push,
                    f"external mutation bypass in job {job_name}",
                )

    def test_publish_has_no_freshness_fallback_or_manual_override(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        preflight = source[source.index("  preflight:\n") : source.index("  full-ci:\n")]
        self.assertIn('test -z "$METADATA_FRESHNESS_RUN_ID"', preflight)
        self.assertIn('[[ "$METADATA_FRESHNESS_RUN_ID" =~ ^[1-9][0-9]*$ ]]', preflight)
        self.assertNotIn("freshness_passed", source)
        self.assertNotIn("metadata_freshness_override", source)
        self.assertNotIn("continue-on-error", source)
        self.assertNotIn("local metadata", source.lower())

if __name__ == "__main__":
    unittest.main()
