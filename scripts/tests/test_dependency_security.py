from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DependencySecurityContractTests(unittest.TestCase):
    def test_sqlparse_pin_excludes_known_vulnerable_release_line(self) -> None:
        requirements = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
        pins = {
            line.split("==", 1)[0].strip(): line.split("==", 1)[1].strip()
            for line in requirements.splitlines()
            if "==" in line and not line.lstrip().startswith("#")
        }
        self.assertEqual(pins.get("sqlparse"), "0.6.0")

    def test_dependency_toolchain_uses_patched_exact_pip_line(self) -> None:
        requirements = (ROOT / "scripts" / "requirements-tools.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("pip==26.2.1", requirements.splitlines())
        self.assertIn("pip-tools==7.6.1", requirements.splitlines())
        self.assertNotIn("pip<26", requirements.splitlines())

    def test_external_github_actions_are_pinned_to_exact_commits(self) -> None:
        mutable: list[str] = []
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
            source = workflow.read_text(encoding="utf-8")
            for match in re.finditer(r"\buses:\s*([^\s#]+)", source):
                reference = match.group(1)
                if reference.startswith("./"):
                    continue
                if not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference):
                    mutable.append(f"{workflow.name}:{reference}")
        self.assertEqual(mutable, [])

    def test_release_container_base_images_are_pinned_to_exact_digests(self) -> None:
        mutable: list[str] = []
        for dockerfile in sorted((ROOT / "deploy").glob("*.Dockerfile")):
            for line in dockerfile.read_text(encoding="utf-8").splitlines():
                if not line.startswith("FROM "):
                    continue
                reference = line.split()[1]
                if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", reference):
                    mutable.append(f"{dockerfile.name}:{reference}")
        self.assertEqual(mutable, [])

        backend = (ROOT / "deploy" / "backend.Dockerfile").read_text(encoding="utf-8")
        frontend = (ROOT / "deploy" / "frontend.Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "FROM python:3.12-alpine@sha256:"
            "d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31",
            backend,
        )
        self.assertIn(
            "FROM node:24-alpine@sha256:"
            "d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43",
            frontend,
        )
        self.assertIn(
            "FROM nginx:1.29-alpine@sha256:"
            "5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de",
            frontend,
        )
        self.assertIn(
            "RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1",
            backend,
        )
        install_requirements = "RUN python -m pip install --no-cache-dir -r /app/requirements.txt"
        remove_runtime_pip = "RUN python -m pip uninstall --yes pip"
        self.assertIn(install_requirements, backend)
        self.assertIn(remove_runtime_pip, backend)
        self.assertLess(backend.index(install_requirements), backend.index(remove_runtime_pip))
        self.assertLess(backend.index(remove_runtime_pip), backend.index("USER animemo"))
        self.assertIn("RUN npm install --global npm@12.0.2", frontend)
        self.assertGreaterEqual(backend.count("apk upgrade --no-cache"), 1)
        self.assertGreaterEqual(frontend.count("apk upgrade --no-cache"), 2)

    def test_release_verifier_uses_patched_go_toolchain_and_grpc(self) -> None:
        verifier = ROOT / "release" / "release_attestation_verifier"
        go_mod = (verifier / "go.mod").read_text(encoding="utf-8")
        self.assertIn("go 1.26.6", go_mod.splitlines())
        self.assertIn("google.golang.org/grpc v1.82.1", go_mod)

        release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dry_run = release_workflow[
            release_workflow.index("  dry-run:\n") : release_workflow.index(
                "  qualification-evidence:\n"
            )
        ]
        publish = release_workflow[release_workflow.index("  publish:\n") :]
        setup_go = "actions/setup-go@924ae3a1cded613372ab5595356fb5720e22ba16"
        offline_verifier_build = (
            "CGO_ENABLED=0 GOPROXY=off GOSUMDB=off go build "
            "-mod=readonly -trimpath -o offline-release-verifier ."
        )

        self.assertEqual(release_workflow.count("go-version: '1.26.6'"), 1)
        self.assertEqual(dry_run.count(setup_go), 1)
        self.assertEqual(dry_run.count("go-version: '1.26.6'"), 1)
        self.assertEqual(dry_run.count(offline_verifier_build), 1)
        self.assertEqual(publish.count(setup_go), 0)
        self.assertEqual(publish.count("go-version: '1.26.6'"), 0)
        self.assertEqual(publish.count(offline_verifier_build), 0)
        self.assertEqual(publish.count("build-initial-trust-kit"), 0)
        self.assertNotIn("go-version: '1.25.8'", release_workflow)

        contract = json.loads(
            (verifier / "INSTALLATION_CONTRACT_V2.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["build"]["minimumGoVersion"], "1.26.6")


if __name__ == "__main__":
    unittest.main()
