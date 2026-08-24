from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
        self.assertEqual(mirror["env"], {"GH_REQUIRED_VERSION": "2.97.0"})
        self.assertIn("验证固定 GitHub CLI 安全基线", source)
        self.assertIn('test "$version" = "$GH_REQUIRED_VERSION"', source)
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
        self.assertEqual(len(scripts), 3)
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
        materialize = next(
            step
            for step in publish_steps
            if step.get("name") == "Materialize exact OCI layouts without rebuilding"
        )["run"]
        self.assertEqual(
            materialize.count("python -m release.cli normalize-oci-layout"), 1
        )
        self.assertLess(
            materialize.index('crane pull "$reference" "$layout" --format=oci'),
            materialize.index("python -m release.cli normalize-oci-layout"),
        )
        self.assertLess(
            materialize.index("python -m release.cli normalize-oci-layout"),
            materialize.index("python -m release.cli build-portable"),
        )
        for argument in (
            '--source-root "$portable_source"',
            '--layout "$layout"',
            '--role "$role"',
            '--repository "${reference%@*}"',
            '--expected-digest "${reference#*@}"',
            "--expected-platform linux/amd64",
        ):
            self.assertIn(argument, materialize)

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
        for declaration in ("local role", "local reference", "local layout"):
            self.assertIn(declaration, source)
        self.assertNotIn('archive="$RUNNER_TEMP/${role}-oci.tar"', source)
        self.assertNotIn('"$portable_source/oci/$role"', source)
        for role in ("api", "web", "postgres", "redis"):
            self.assertIn(f'layout="$portable_source/oci/{role}"', source)

    def test_oci_layout_function_runs_under_bash_nounset_for_every_role(self):
        bash = _bash_path()
        self.assertIsNotNone(
            bash,
            "Bash is required for the fail-closed OCI layout runtime regression",
        )
        release = workflow("release.yml")
        materialize = next(
            step
            for step in release["jobs"]["publish"]["steps"]
            if step.get("name")
            == "Materialize exact OCI layouts without rebuilding"
        )["run"]
        function = re.search(
            r"(?ms)^export_layout\(\) \{\n.*?^\}", materialize
        )
        self.assertIsNotNone(function)
        function_source = function.group(0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portable_source = root / "portable"
            trace = root / "trace.txt"
            symlink_target = root / "symlink-target"
            symlink_target.mkdir()
            references = {
                "api": "ghcr.io/yanyuhanyue/animemo-api@sha256:"
                "3331277a905902388afe430b92370f55b6d0425c663a2ae7470b6d678e579a5a",
                "web": "ghcr.io/yanyuhanyue/animemo-web@sha256:"
                "86b99658c1ea71c0a407ef4cddbd5349d8c159195ebedcb3055d9c3c34d5824a",
                "postgres": "docker.io/library/postgres@sha256:"
                "075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571",
                "redis": "docker.io/library/redis@sha256:"
                "9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf",
            }
            stubs = (
                "crane() {\n"
                "  test \"$1\" = pull\n"
                "  test \"$4\" = --format=oci\n"
                "  printf 'crane|%s|%s\\n' \"$2\" \"$3\" >> \"$TRACE\"\n"
                "  if [[ \"$CRANE_MODE\" = fail ]]; then return 17; fi\n"
                "  if [[ \"$CRANE_MODE\" = symlink ]]; then\n"
                "    ln -s \"$SYMLINK_TARGET\" \"$3\"\n"
                "    return\n"
                "  fi\n"
                "  mkdir -p \"$3\"\n"
                "  if [[ \"$CRANE_MODE\" != missing-oci-layout ]]; then\n"
                "    printf '{\"imageLayoutVersion\":\"1.0.0\"}\\n' > \"$3/oci-layout\"\n"
                "  fi\n"
                "  if [[ \"$CRANE_MODE\" != missing-index ]]; then\n"
                "    printf '{\"schemaVersion\":2,\"manifests\":[]}\\n' > \"$3/index.json\"\n"
                "  fi\n"
                "  if [[ \"$CRANE_MODE\" != missing-blobs ]]; then\n"
                "    mkdir -p \"$3/blobs\"\n"
                "  fi\n"
                "}\n"
                "python() {\n"
                "  test \"$1\" = -m\n"
                "  test \"$2\" = release.cli\n"
                "  test \"$3\" = normalize-oci-layout\n"
                "  printf 'normalize|%s\\n' \"$*\" >> \"$TRACE\"\n"
                "  if [[ \"$CRANE_MODE\" = normalize-fail ]]; then return 23; fi\n"
                "}\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PORTABLE_SOURCE": str(portable_source),
                    "TRACE": str(trace),
                    "SYMLINK_TARGET": str(symlink_target),
                    "CRANE_MODE": "valid",
                }
            )

            def script_for(calls):
                return (
                    "set -euo pipefail\n"
                    "umask 077\n"
                    'portable_source="$PORTABLE_SOURCE"\n'
                    'mkdir -p "$portable_source/oci"\n'
                    + stubs
                    + function_source
                    + "\n"
                    + calls
                )

            calls = "".join(
                f'export_layout {role} "{reference}"\n'
                for role, reference in references.items()
            )
            script = script_for(calls)
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
            completed = subprocess.run(
                [bash],
                input=script,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            trace_text = trace.read_text(encoding="utf-8").replace("\\", "/")
            layout_paths = []
            for role in ("api", "web", "postgres", "redis"):
                self.assertIn(references[role], trace_text)
                layout = portable_source / "oci" / role
                layout_paths.append(layout.resolve())
                self.assertTrue((layout / "oci-layout").is_file())
                self.assertTrue((layout / "index.json").is_file())
                self.assertTrue((layout / "blobs").is_dir())
                self.assertIn(layout.as_posix(), trace_text)
                self.assertIn(f"--role {role}", trace_text)
                self.assertIn(
                    f"--expected-digest {references[role].split('@', 1)[1]}", trace_text
                )
            self.assertEqual(len(set(layout_paths)), 4)
            self.assertNotIn("tar --extract", function_source)

            failures = (
                ("unknown role", f'unknown "{references["api"]}"', "valid"),
                ("empty role", f'"" "{references["api"]}"', "valid"),
                ("missing reference", "api", "valid"),
                ("empty reference", 'api ""', "valid"),
                ("mutable reference", "api ghcr.io/example/image:latest", "valid"),
                ("crane failure", f'api "{references["api"]}"', "fail"),
                (
                    "normalize failure",
                    f'api "{references["api"]}"',
                    "normalize-fail",
                ),
                (
                    "missing oci-layout",
                    f'api "{references["api"]}"',
                    "missing-oci-layout",
                ),
                ("missing index.json", f'api "{references["api"]}"', "missing-index"),
                ("missing blobs", f'api "{references["api"]}"', "missing-blobs"),
                ("symlink layout", f'api "{references["api"]}"', "symlink"),
            )
            for label, arguments, crane_mode in failures:
                case_environment = environment.copy()
                case_environment["CRANE_MODE"] = crane_mode
                failed = subprocess.run(
                    [bash],
                    input=script_for(f"export_layout {arguments}\n"),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    env=case_environment,
                    check=False,
                )
                self.assertNotEqual(
                    failed.returncode,
                    0,
                    f"{label} unexpectedly passed: {failed.stdout}",
                )

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
            "selection-authority",
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
        self.assertIn("target_version_override", inputs)
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
            ["preflight", "release-authority", "platform-qualification"],
        )
        self.assertEqual(
            release["jobs"]["publish"]["needs"],
            ["preflight", "release-authority", "metadata-freshness-authority"],
        )
        for job_name in ("dry-run", "publish"):
            self.assertNotIn("performance", release["jobs"][job_name]["needs"])
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
            dry_permissions, {"contents": "read", "pull-requests": "read"}
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
        dry_run = source[source.index("  dry-run:\n") : source.index("  publish:\n")]

        self.assertIn("UPGRADE_BASE_SHA: ${{ inputs.upgrade_base_sha }}", dry_run)
        self.assertIn('release_notes_base="$UPGRADE_BASE_SHA"', dry_run)
        self.assertIn('if [[ -n "$PREVIOUS_STABLE" ]]', dry_run)
        self.assertIn(
            'release_notes_base="$(git rev-parse "$PREVIOUS_STABLE^{commit}")"',
            dry_run,
        )
        self.assertIn(
            'git merge-base --is-ancestor "$release_notes_base" "$CANDIDATE_SHA"',
            dry_run,
        )
        self.assertIn('--range-start "$release_notes_base"', dry_run)
        self.assertNotIn('--range-start "$UPGRADE_BASE_SHA"', dry_run)
        self.assertNotIn('test -n "$PREVIOUS_STABLE"', dry_run)

    def test_release_images_receive_the_same_runtime_identity_as_the_manifest(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("ANIMEMO_VERSION=${{ needs.preflight.outputs.release_tag }}"), 4)
        self.assertEqual(source.count("ANIMEMO_COMMIT=${{ needs.preflight.outputs.candidate_sha }}"), 2)
        self.assertEqual(source.count("ANIMEMO_COMMIT=${{ github.sha }}"), 2)
        self.assertNotIn("VITE_TURNSTILE_SITE_KEY", source)
        self.assertIn("promote-manifest", source)
        self.assertEqual(source.count("scripts/rehearse-release-images.sh"), 2)
        self.assertIn("Start and accept the exact images before any external publication", source)
        self.assertIn("Publish only the already rehearsed images", source)
        self.assertNotIn("Build and publish API image once", source)
        publish_section = source.index("  publish:\n")
        rehearse = source.index("Start and accept the exact images before any external publication", publish_section)
        publish = source.index("Publish only the already rehearsed images", publish_section)
        self.assertLess(rehearse, publish)
        self.assertNotIn("push: true", source[publish_section:publish])

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
            },
        )

        self.assertLess(source.index(early_name), source.index("Download and verify Phase A qualification evidence"))
        self.assertLess(source.index(early_name), source.index("docker/login-action"))
        self.assertLess(publish.index(final_name), publish.index("docker/login-action"))
        self.assertLess(publish.index(final_name), publish.index("docker push"))
        self.assertLess(publish.index(final_name), publish.index("git push origin"))
        self.assertLess(
            publish.index(final_name),
            publish.index('gh api --method POST "repos/$GITHUB_REPOSITORY/releases"'),
        )
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
        self.assertGreaterEqual(release.count("-r durability/requirements.txt"), 1)
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
            "release.yml": 3,
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
        pull_gate = publish.index("Acquire exact dependency images before external publication")
        dependency_layouts = publish.index(
            "Materialize exact dependency OCI layouts before external publication"
        )
        for mutation_marker in (
            "docker/login-action",
            "docker push",
            "actions/attest@",
            "git push origin",
            'gh api --method POST "repos/$GITHUB_REPOSITORY/releases"',
        ):
            with self.subTest(marker=mutation_marker):
                self.assertLess(pull_gate, publish.index(mutation_marker))
                self.assertLess(dependency_layouts, publish.index(mutation_marker))
        post_mutation = publish[publish.index("Publish only the already rehearsed images") :]
        self.assertNotIn('export_layout postgres "$POSTGRES_IMAGE"', post_mutation)
        self.assertNotIn('export_layout redis "$REDIS_IMAGE"', post_mutation)

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
                "CGO_ENABLED=0 GOPROXY=off GOSUMDB=off go build "
                "-mod=readonly -trimpath -o offline-release-verifier ."
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
        self.assertNotIn(
            "--output release/release_attestation_verifier/pretrust-v2",
            release,
        )
        self.assertGreaterEqual(release.count("$RUNNER_TEMP/animemo-pretrust-v2"), 4)
        self.assertEqual(
            release.count(
                '--initial-trust-kit "$RUNNER_TEMP/animemo-pretrust-v2"'
            ),
            1,
        )

    def test_publish_rebinds_exact_qualified_prepublication_materials(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dry_run_identity = release.index("build-prepublication-materials")
        qualification_copy = release.index(
            "install -m 0600 release-dry-run-input/prepublication-materials.json"
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
            release.index("  dry-run:\n") : release.index(
                "  qualification-evidence:\n"
            )
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
            release.index("  qualification-evidence:\n") : release.index(
                "  publish:\n"
            )
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

        self.assertIn("GH_REQUIRED_VERSION: 2.97.0", release)
        self.assertIn("GH_REQUIRED_VERSION: 2.97.0", promotion)
        self.assertEqual(release.count(gate), 4)
        self.assertEqual(promotion.count(gate), 2)

    def test_platform_qualification_is_hosted_scoped_and_injected_exactly(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        promotion = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(
            encoding="utf-8"
        )
        job = release["jobs"]["platform-qualification"]

        self.assertEqual(job["runs-on"], "ubuntu-latest")
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
        self.assertIn("path: release-qualification/", source)
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
        self.assertIn("--notes-file promotion-output/release-notes.md", source)
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
        before_first_mutation = publish_source[: publish_source.index("crane tag")]
        for guard in (
            'test "$(git rev-parse origin/main)" = "$GITHUB_SHA"',
            '! git ls-remote --exit-code --tags origin "refs/tags/$STABLE_TAG"',
            '! gh release view "$STABLE_TAG" --repo "$GITHUB_REPOSITORY"',
        ):
            self.assertIn(guard, before_first_mutation)

    def test_rc_publication_is_draft_upload_verify_publish_with_qualified_notes(self):
        release = workflow("release.yml")
        names = [step.get("name", "") for step in release["jobs"]["publish"]["steps"]]
        expected = (
            "Generate the closed publication plan without mutation",
            "Create the immutable annotated RC tag",
            "Create an unpublished GitHub Draft Pre-release",
            "Upload and read back the complete Draft asset set",
            "Publish only the fully verified Draft Pre-release",
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
        self.assertLess(
            source.index('gh api --method POST "repos/$GITHUB_REPOSITORY/releases"'),
            source.index('gh release upload "$RELEASE_TAG"'),
        )
        self.assertLess(
            source.index('gh release upload "$RELEASE_TAG"'),
            source.index('gh api --method PATCH "repos/$GITHUB_REPOSITORY/releases/$RELEASE_ID"'),
        )
        self.assertIn("id: draft_release", source)
        self.assertIn('release_id=$release_id', source)
        draft_step = source[
            source.index("Create an unpublished GitHub Draft Pre-release") :
            source.index("Upload and read back the complete Draft asset set")
        ]
        self.assertIn('--input "$RUNNER_TEMP/draft-create-request.json"', draft_step)
        self.assertNotIn("releases?per_page=100", draft_step)
        self.assertIn('gh api "repos/$GITHUB_REPOSITORY/releases/$RELEASE_ID"', source)
        self.assertNotIn(
            'gh api "repos/$GITHUB_REPOSITORY/releases/tags/$RELEASE_TAG"',
            source[: source.index("Verify the public RC without authenticated asset transport")],
        )

    def test_stable_publication_uses_the_same_draft_transaction_and_never_rebuilds(self):
        source = (ROOT / ".github" / "workflows" / "promote-release.yml").read_text(
            encoding="utf-8"
        )
        publish_source = source[source.index("  publish:\n") :]
        self.assertIn("Create an unpublished Stable Draft Release", source)
        self.assertIn("Upload and read back the complete Stable Draft asset set", source)
        self.assertIn("Publish only the fully verified Stable Draft as Latest", source)
        self.assertIn("Verify the public Stable release without authenticated asset transport", source)
        self.assertNotIn("docker/build-push-action", source)
        self.assertNotIn("docker build", source)
        self.assertIn("plan-stable-publication-files", source)
        self.assertIn(
            "--promotion-acceptance promotion-output/stable-promotion-acceptance.json",
            source,
        )
        self.assertIn("--rc-manifest rc-assets/release-manifest.json", source)
        self.assertNotIn(
            'test "$(git rev-parse origin/main)" = "$GITHUB_SHA"',
            publish_source[publish_source.index("crane tag") :],
        )


    def test_rc_presentation_is_plan_derived_and_guarded_before_each_mutation(self):
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        publish = source[source.index("  publish:\n") :]

        self.assertEqual(publish.count("emit-publication-presentation"), 1)
        self.assertIn(
            "RELEASE_TAG: ${{ steps.presentation.outputs.release_tag }}",
            publish,
        )
        self.assertIn(
            "RELEASE_TITLE: ${{ steps.presentation.outputs.release_title }}",
            publish,
        )
        self.assertIn(
            "ANNOTATED_TAG_SUBJECT: ${{ steps.presentation.outputs.annotated_tag_subject }}",
            publish,
        )
        self.assertIn('--message "$ANNOTATED_TAG_SUBJECT"', publish)
        self.assertIn('--arg name "$RELEASE_TITLE"', publish)
        local_guard = publish.index("verify-local-tag-presentation")
        remote_push = publish.index('git push origin "refs/tags/$RELEASE_TAG"')
        draft_guard = publish.index("--repository . --state draft")
        draft_get = publish.index(
            'gh api "repos/$GITHUB_REPOSITORY/releases/$release_id"'
        )
        asset_upload = publish.index('gh release upload "$RELEASE_TAG"')
        post_guard = publish.index("--repository . --state published")
        post_verification = publish.index("verify-post-publish")
        self.assertLess(local_guard, remote_push)
        self.assertLess(draft_get, draft_guard)
        self.assertLess(draft_guard, asset_upload)
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
            publish.index("crane tag"),
        )
        self.assertIn(
            "STABLE_TAG: ${{ steps.presentation.outputs.release_tag }}",
            publish,
        )
        self.assertIn(
            "RELEASE_TITLE: ${{ steps.presentation.outputs.release_title }}",
            publish,
        )
        self.assertIn('--message "$ANNOTATED_TAG_SUBJECT"', publish)
        self.assertIn('--arg name "$RELEASE_TITLE"', publish)
        self.assertLess(
            publish.index("verify-local-tag-presentation"),
            publish.index('git push origin "refs/tags/$STABLE_TAG"'),
        )
        self.assertLess(
            publish.index("--repository . --state draft"),
            publish.index('gh release upload "$STABLE_TAG"'),
        )
        self.assertLess(
            publish.index('gh api "repos/$GITHUB_REPOSITORY/releases/$release_id"'),
            publish.index("--repository . --state draft"),
        )
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

    def test_metadata_freshness_workflow_has_two_inputs_and_read_only_permissions(self):
        freshness = workflow("release-metadata-freshness.yml")
        inputs = freshness["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(inputs), {"qualification_run_id", "intended_main_sha"}
        )
        self.assertEqual(
            freshness["permissions"],
            {"contents": "read", "pull-requests": "read", "actions": "read"},
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

    def test_metadata_freshness_collects_two_complete_snapshots_and_exact_artifact(self):
        source = (
            ROOT / ".github" / "workflows" / "release-metadata-freshness.yml"
        ).read_text(encoding="utf-8")
        module = (ROOT / "release" / "metadata_freshness.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("MINIMUM_SNAPSHOT_INTERVAL_SECONDS = 60", module)
        self.assertIn("MAX_COMPLETE_ATTEMPTS = 3", module)
        self.assertIn('test "$GITHUB_WORKFLOW_SHA" = "$INTENDED_MAIN_SHA"', source)
        self.assertIn('snapshot_label="A"', module)
        self.assertIn('snapshot_label="B"', module)
        self.assertIn("runtime_clock.sleep(MINIMUM_SNAPSHOT_INTERVAL_SECONDS)", module)
        self.assertIn("release-metadata-freshness-${{ github.run_id }}", source)
        self.assertIn('test "$(find "$directory" -mindepth 1 -maxdepth 1 -type f | wc -l)" = "9"', source)
        self.assertIn("credential-shaped material detected", source)

    def test_publish_authenticates_freshness_producer_bindings_and_ttl(self):
        release = workflow("release.yml")
        source = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        authority = release["jobs"]["metadata-freshness-authority"]
        self.assertEqual(authority["runs-on"], "ubuntu-latest")
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
