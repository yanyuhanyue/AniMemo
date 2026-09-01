from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.workflow_dag import WorkflowDagError, validate_repository


class WorkflowDagContractTests(unittest.TestCase):
    def _repository(self) -> tempfile.TemporaryDirectory[str]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / "release").mkdir()
        (root / "scripts").mkdir()
        self._write(
            root / "scripts" / "release_qualification.py",
            'REQUIRED_RESULT_JOB_IDS = ("build",)\n',
        )
        return temporary

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    @staticmethod
    def _contract(**overrides: object) -> dict[str, object]:
        contract: dict[str, object] = {
            "schemaVersion": "animemo.workflow-dag-contract/v1",
            "requiredWorkflows": [".github/workflows/release.yml"],
            "nonCancellingConcurrency": [".github/workflows/release.yml"],
            "skipAuthorities": [],
            "requiredGateReachability": [
                {
                    "workflow": ".github/workflows/release.yml",
                    "job": "publish",
                    "requiredGates": ["build"],
                }
            ],
            "mutationAuthorities": [
                {
                    "domain": "RC_PUBLICATION",
                    "workflow": ".github/workflows/release.yml",
                    "job": "publish",
                    "reconciliationMarker": "python -m scripts.release_publication transaction-",
                }
            ],
            "candidateAuthority": {
                "producer": {
                    "workflow": ".github/workflows/release.yml",
                    "job": "build",
                    "annotation": "AUTHORITATIVE_CANDIDATE_BYTE_PRODUCER",
                },
                "consumers": [
                    {
                        "workflow": ".github/workflows/release.yml",
                        "job": "qualification-finalizer",
                    },
                    {
                        "workflow": ".github/workflows/release.yml",
                        "job": "publish",
                    }
                ],
                "producerMarkers": ["build-installer-materials"],
                "forbiddenConsumerBuildMarkers": [
                    "docker build",
                    "docker/build-push-action@",
                    "build-installer-materials",
                    "generate-manifest",
                ],
                "nonAuthoritativeBuilds": [],
            },
            "completedResultBuilders": [
                {
                    "workflow": ".github/workflows/release.yml",
                    "job": "qualification-finalizer",
                    "builderModule": "scripts/release_qualification.py",
                    "requiredResultsConstant": "REQUIRED_RESULT_JOB_IDS",
                    "requiredResults": ["build"],
                }
            ],
        }
        contract.update(overrides)
        return contract

    @staticmethod
    def _valid_workflow() -> str:
        return """name: Release
on: workflow_dispatch
concurrency:
  group: release
  cancel-in-progress: false
jobs:
  build:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    env:
      ANIMEMO_CANDIDATE_BUILD_AUTHORITY: AUTHORITATIVE_CANDIDATE_BYTE_PRODUCER
    steps:
      - run: python -m release.cli build-installer-materials
  qualification-finalizer:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    needs: build
    steps:
      - run: python -m release.cli build-prepublication-candidate-input
  publish:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    needs: build
    steps:
      - run: python -m scripts.release_publication transaction-reconcile
"""

    def test_duplicate_workflow_mapping_key_is_rejected(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            self._write(
                root / ".github" / "workflows" / "release.yml",
                """name: Release
on: workflow_dispatch
concurrency:
  group: release
  cancel-in-progress: false
jobs:
  build:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    env:
      ANIMEMO_CANDIDATE_BUILD_AUTHORITY: AUTHORITATIVE_CANDIDATE_BYTE_PRODUCER
    steps:
      - run: python -m release.cli build-prepublication-candidate-input
  publish:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    timeout-minutes: 20
    needs: build
    steps:
      - run: python -m scripts.release_publication transaction-reconcile
""",
            )
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "duplicate mapping key.*timeout-minutes"
            ):
                validate_repository(root)

    def test_executable_job_without_timeout_is_rejected(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "    timeout-minutes: 20\n    needs: build\n",
                "    needs: build\n",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "executable job requires timeout-minutes.*publish"
            ):
                validate_repository(root)

    def test_contract_unknown_field_is_rejected(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            self._write(
                root / ".github" / "workflows" / "release.yml",
                self._valid_workflow(),
            )
            contract = self._contract()
            contract["advisoryOnly"] = True
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(contract),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "contract has unknown or missing fields"
            ):
                validate_repository(root)

    def test_job_dependency_cycle_is_rejected(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "    steps:\n      - run: python -m release.cli build-installer-materials\n  qualification-finalizer:",
                "    needs: publish\n    steps:\n      - run: python -m release.cli build-installer-materials\n  qualification-finalizer:",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(WorkflowDagError, "workflow DAG contains cycle"):
                validate_repository(root)

    def test_required_gate_must_be_transitively_reachable(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace("    needs: build\n", "")
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "required gate is unreachable.*build"
            ):
                validate_repository(root)

    def test_needs_output_without_reachable_producer_is_rejected(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "    needs: build\n    steps:",
                "    needs: build\n    env:\n      INPUT: ${{ needs.build.outputs.missing }}\n    steps:",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "needs output has no reachable producer.*missing"
            ):
                validate_repository(root)

    def test_required_concurrency_cannot_be_missing_or_cancelling(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "concurrency:\n  group: release\n  cancel-in-progress: false\n",
                "",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "workflow requires closed concurrency"
            ):
                validate_repository(root)

    def test_mutation_authority_requires_unique_durable_reconciliation(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "python -m scripts.release_publication transaction-reconcile",
                "git push origin refs/tags/v1.1.0-rc.1",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "mutation authority lacks durable reconciliation"
            ):
                validate_repository(root)

    def test_mutation_reconciliation_marker_must_be_an_executed_command(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "    steps:\n      - run: python -m scripts.release_publication transaction-reconcile",
                "    steps:\n"
                "      - name: python -m scripts.release_publication transaction-reconcile\n"
                "        run: echo marker-labels-are-not-authority",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "mutation authority lacks durable reconciliation"
            ):
                validate_repository(root)

    def test_pull_request_lane_cannot_reach_release_mutation_authority(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "on: workflow_dispatch", "on: [pull_request, workflow_dispatch]"
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "pull-request lane reaches mutation authority"
            ):
                validate_repository(root)

    def test_artifact_consumer_requires_one_reachable_producer(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "    steps:\n      - run: python -m scripts.release_publication transaction-reconcile",
                "    steps:\n      - uses: actions/download-artifact@1111111111111111111111111111111111111111\n        with:\n          name: candidate-bytes\n      - run: python -m scripts.release_publication transaction-reconcile",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "artifact consumer requires exactly one producer"
            ):
                validate_repository(root)

    def test_artifact_name_resolves_reachable_job_output_identity(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "    steps:\n      - run: python -m release.cli build-installer-materials\n  qualification-finalizer:",
                "    outputs:\n"
                "      candidate_tag: ${{ steps.identity.outputs.candidate_tag }}\n"
                "    steps:\n"
                "      - id: identity\n"
                "        run: |\n"
                "          echo candidate_tag=v1.1.0-rc.19 >> \"$GITHUB_OUTPUT\"\n"
                "          python -m release.cli build-installer-materials\n"
                "      - uses: actions/upload-artifact@1111111111111111111111111111111111111111\n"
                "        with:\n"
                "          name: candidate-${{ steps.identity.outputs.candidate_tag }}\n"
                "  qualification-finalizer:",
            ).replace(
                "    steps:\n      - run: python -m scripts.release_publication transaction-reconcile",
                "    steps:\n"
                "      - uses: actions/download-artifact@1111111111111111111111111111111111111111\n"
                "        with:\n"
                "          name: candidate-${{ needs.build.outputs.candidate_tag }}\n"
                "      - run: python -m scripts.release_publication transaction-reconcile",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            receipt = validate_repository(root)

            self.assertEqual(receipt["artifactProducerCount"], 1)
            self.assertEqual(receipt["artifactConsumerCount"], 1)
            self.assertEqual(receipt["completedResultBuilderCount"], 1)
            self.assertEqual(receipt["completedResultRequirementCount"], 1)

    def test_skip_authority_must_always_observe_every_covered_job(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            self._write(
                root / ".github" / "workflows" / "release.yml",
                self._valid_workflow(),
            )
            contract = self._contract(
                skipAuthorities=[
                    {
                        "workflow": ".github/workflows/release.yml",
                        "job": "publish",
                        "covers": ["build"],
                        "allowedSkippedJobs": ["build"],
                        "conditionMarker": "always()",
                    }
                ]
            )
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(contract),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "skip authority must run under its condition marker"
            ):
                validate_repository(root)

    def test_candidate_consumer_cannot_build_or_claim_byte_authority(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "python -m scripts.release_publication transaction-reconcile",
                "docker build .\n          python -m scripts.release_publication transaction-reconcile",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "candidate authority consumer cannot build"
            ):
                validate_repository(root)

    def test_candidate_input_metadata_generation_is_allowed_in_finalizer(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            self._write(
                root / ".github" / "workflows" / "release.yml",
                self._valid_workflow(),
            )
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            receipt = validate_repository(root)

            self.assertEqual(receipt["candidateAuthorityProducer"], ".github/workflows/release.yml:build")
            self.assertEqual(receipt["completedResultBuilderCount"], 1)

    def test_completed_result_builder_cannot_require_itself(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            self._write(
                root / ".github" / "workflows" / "release.yml",
                self._valid_workflow(),
            )
            self._write(
                root / "scripts" / "release_qualification.py",
                'REQUIRED_RESULT_JOB_IDS = ("qualification-finalizer",)\n',
            )
            contract = self._contract()
            contract["completedResultBuilders"][0]["requiredResults"] = [
                "qualification-finalizer"
            ]
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(contract),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "completed result builder requires its own result"
            ):
                validate_repository(root)

    def test_completed_result_builder_required_job_must_exist(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            self._write(
                root / ".github" / "workflows" / "release.yml",
                self._valid_workflow(),
            )
            self._write(
                root / "scripts" / "release_qualification.py",
                'REQUIRED_RESULT_JOB_IDS = ("missing",)\n',
            )
            contract = self._contract()
            contract["completedResultBuilders"][0]["requiredResults"] = ["missing"]
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(contract),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "references unknown required job.*missing"
            ):
                validate_repository(root)

    def test_completed_result_builder_requires_direct_need(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "  qualification-finalizer:\n",
                "  intermediate:\n"
                "    runs-on: ubuntu-24.04\n"
                "    timeout-minutes: 10\n"
                "    needs: build\n"
                "    steps:\n"
                "      - run: echo observed\n"
                "  qualification-finalizer:\n",
            ).replace(
                "    needs: build\n    steps:\n      - run: python -m release.cli build-prepublication-candidate-input",
                "    needs: intermediate\n    steps:\n      - run: python -m release.cli build-prepublication-candidate-input",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "completed result builder result is not a direct need.*build"
            ):
                validate_repository(root)

    def test_completed_result_builder_constant_must_exactly_match_contract(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            self._write(
                root / ".github" / "workflows" / "release.yml",
                self._valid_workflow(),
            )
            self._write(
                root / "scripts" / "release_qualification.py",
                'REQUIRED_RESULT_JOB_IDS = ("publish",)\n',
            )
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "contract does not match source constant"
            ):
                validate_repository(root)

    def test_job_cannot_read_its_own_needs_result(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "    needs: build\n    steps:\n      - run: python -m release.cli build-prepublication-candidate-input",
                "    needs: build\n"
                "    env:\n"
                "      IMPOSSIBLE_SELF_RESULT: ${{ needs.qualification-finalizer.result }}\n"
                "    steps:\n"
                "      - run: python -m release.cli build-prepublication-candidate-input",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(WorkflowDagError, "job reads its own needs result"):
                validate_repository(root)

    def test_builder_cannot_default_current_or_future_authority(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            self._write(
                root / ".github" / "workflows" / "release.yml",
                self._valid_workflow(),
            )
            self._write(
                root / "scripts" / "release_qualification.py",
                "REQUIRED_RESULT_JOB_IDS = ('build',)\n"
                "def build(*, status='completed', conclusion='success'):\n"
                "    return status, conclusion\n",
            )
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError, "hardcodes current or future authority"
            ):
                validate_repository(root)

    def test_non_authoritative_build_inventory_cannot_omit_annotated_job(self) -> None:
        with self._repository() as directory:
            root = Path(directory)
            source = self._valid_workflow().replace(
                "  publish:\n",
                "  isolated-build:\n"
                "    runs-on: ubuntu-24.04\n"
                "    timeout-minutes: 10\n"
                "    env:\n"
                "      ANIMEMO_CANDIDATE_BUILD_AUTHORITY: NON_AUTHORITATIVE_ISOLATED_TEST_ONLY\n"
                "    steps:\n"
                "      - run: docker compose build\n"
                "  publish:\n",
            )
            self._write(root / ".github" / "workflows" / "release.yml", source)
            self._write(
                root / "release" / "workflow-dag-contract.json",
                json.dumps(self._contract()),
            )

            with self.assertRaisesRegex(
                WorkflowDagError,
                "non-authoritative build inventory is incomplete or stale",
            ):
                validate_repository(root)


if __name__ == "__main__":
    unittest.main()
