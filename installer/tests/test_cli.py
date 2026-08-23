from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from installer.cli import EXIT_SUCCESS, EXIT_VALIDATION, _listen, main
from installer.runtime import (
    Installer,
    InstallerError,
    InstallerMode,
    InstallRequest,
    InstallTransportSource,
    ReleaseSelector,
    TargetClass,
    TargetEvidence,
)
from updater.local_bundle import LocalBundleTransportPolicy
from updater.transport import ExplicitTransportPolicy


def digest(character: str) -> str:
    return "sha256:" + character * 64


class _Runtime:
    def __init__(self) -> None:
        from installer.tests.test_runtime import (
            BootstrapGateFake,
            CompatibilityFake,
            ConfigurationFake,
            FreshFake,
            OperationFake,
            PlatformFake,
            ReleaseFake,
            RestoreFake,
            TargetFake,
        )

        self.runtime = Installer(
            releases=ReleaseFake(),
            target=TargetFake(),
            platform=PlatformFake(),
            compatibility=CompatibilityFake(),
            configuration=ConfigurationFake(),
            operations=OperationFake(),
            fresh=FreshFake(),
            restore=RestoreFake(),
            bootstrap_privilege_gate=BootstrapGateFake(),
        )


class InstallerCliTests(unittest.TestCase):
    def test_production_online_execution_authorizes_stage0_before_runtime(self) -> None:
        holder = _Runtime()
        output = io.StringIO()
        with (
            mock.patch(
                "installer.production.build_runtime",
                return_value=holder.runtime,
            ),
            mock.patch("installer.bootstrap.authorize_online_stage0") as authorize,
            redirect_stdout(output),
        ):
            code = main(
                [
                    "install",
                    "--channel",
                    "rc",
                    "--public-origin",
                    "https://anime.example",
                    "--non-interactive",
                    "--accept",
                    "--json",
                ]
            )

        self.assertEqual(code, EXIT_SUCCESS)
        authorize.assert_called_once()
        self.assertEqual(
            authorize.call_args.kwargs["tag"],
            holder.runtime._releases.evidence.version,
        )
        self.assertEqual(
            authorize.call_args.kwargs["release_commit"],
            holder.runtime._releases.evidence.commit,
        )

    def test_release_source_has_no_auto_url_or_fallback_value(self) -> None:
        for source in (
            "auto",
            "fallback",
            "https://download.example.invalid/release",
        ):
            with (
                self.subTest(source=source),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(
                    [
                        "install",
                        "--channel",
                        "rc",
                        "--source",
                        source,
                        "--public-origin",
                        "https://anime.example",
                        "--dry-run",
                    ],
                    runtime=_Runtime().runtime,
                )

    def test_local_bundle_requires_exact_version_payload_and_release_attestation(
        self,
    ) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "install",
                    "--version",
                    "v1.1.0",
                    "--source",
                    "local-bundle",
                    "--public-origin",
                    "https://anime.example",
                    "--dry-run",
                    "--json",
                ]
            )

        self.assertEqual(code, EXIT_VALIDATION)
        self.assertIn(
            "INSTALL_LOCAL_BUNDLE_INPUT_REQUIRED",
            output.getvalue(),
        )

    def test_local_bundle_paths_are_wired_to_production_but_not_rendered_in_plan(
        self,
    ) -> None:
        holder = _Runtime()
        policy = LocalBundleTransportPolicy()
        holder.runtime._releases.evidence = replace(
            holder.runtime._releases.evidence,
            version="v1.1.0",
            channel="stable",
            transport_source=InstallTransportSource.LOCAL_BUNDLE,
            transport_policy_identity=policy.identity,
        )
        offline_root = Path(tempfile.gettempdir()).resolve() / "animemo-offline"
        payload = offline_root / "payload.tar"
        sidecar = offline_root / "release-attestation.sigstore.json"
        output = io.StringIO()
        with (
            mock.patch(
                "installer.production.build_runtime",
                return_value=holder.runtime,
            ) as factory,
            redirect_stdout(output),
        ):
            code = main(
                [
                    "install",
                    "--version",
                    "v1.1.0",
                    "--source",
                    "local-bundle",
                    "--bundle-payload",
                    str(payload),
                    "--release-attestation",
                    str(sidecar),
                    "--public-origin",
                    "https://anime.example",
                    "--dry-run",
                    "--json",
                ]
            )

        self.assertEqual(code, EXIT_SUCCESS)
        factory.assert_called_once_with(
            instance_name="default",
            transport_source=InstallTransportSource.LOCAL_BUNDLE,
            transport_policy=policy,
            local_bundle_payload=payload,
            local_bundle_release_attestation=sidecar,
        )
        rendered = output.getvalue()
        self.assertIn('"transportSource": "local-bundle"', rendered)
        self.assertNotIn(str(payload), rendered)
        self.assertNotIn(str(sidecar), rendered)

    def test_local_bundle_without_immutable_publication_proof_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload.tar"
            sidecar = root / "release-attestation.sigstore.json"
            payload.write_bytes(b"payload")
            sidecar.write_bytes(b"unverified structural lookalike")
            output = io.StringIO()
            with (
                mock.patch(
                    "installer.production.ReleaseResolver",
                    side_effect=AssertionError(
                        "network resolver must not be constructed"
                    ),
                ) as resolver,
                redirect_stdout(output),
            ):
                code = main(
                    [
                        "install",
                        "--version",
                        "v1.1.0",
                        "--source",
                        "local-bundle",
                        "--bundle-payload",
                        str(payload),
                        "--release-attestation",
                        str(sidecar),
                        "--public-origin",
                        "https://anime.example",
                        "--dry-run",
                        "--json",
                    ]
                )

        self.assertEqual(code, EXIT_VALIDATION)
        self.assertIn("INSTALL_LOCAL_BUNDLE_VERIFICATION_FAILED", output.getvalue())
        resolver.assert_not_called()

    def test_official_mirror_is_explicit_and_bound_into_the_plan(self) -> None:
        holder = _Runtime()
        policy = ExplicitTransportPolicy.official_mirror()
        holder.runtime._releases.evidence = replace(
            holder.runtime._releases.evidence,
            transport_source=InstallTransportSource.OFFICIAL_MIRROR,
            transport_policy_identity=policy.identity,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "install",
                    "--channel",
                    "rc",
                    "--source",
                    "official-mirror",
                    "--public-origin",
                    "https://anime.example",
                    "--dry-run",
                    "--json",
                ],
                runtime=holder.runtime,
            )

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn('"transportSource": "official-mirror"', output.getvalue())
        self.assertIn(
            f'"transportPolicyIdentity": "{policy.identity}"',
            output.getvalue(),
        )

    def test_updater_handoff_is_a_nonzero_rejection(self) -> None:
        holder = _Runtime()
        holder.runtime._target.evidence = TargetEvidence(
            TargetClass.ACTIVE,
            digest("4"),
            instance_id="12345678-1234-4234-9234-123456789abc",
            release_manifest_digest=digest("a"),
            material_identity_digest=digest("b"),
            config_revision="22345678-1234-4234-9234-123456789abc",
            public_origin="https://anime.example",
            listen_host="127.0.0.1",
            listen_port=8088,
            exact_release_running=True,
            doctor_complete=True,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "install",
                    "--channel",
                    "rc",
                    "--public-origin",
                    "https://anime.example",
                    "--non-interactive",
                    "--accept",
                    "--json",
                ],
                runtime=holder.runtime,
            )

        self.assertEqual(code, EXIT_VALIDATION)
        self.assertIn('"outcome": "UPDATER_HANDOFF"', output.getvalue())

    def test_restore_requires_one_explicit_secret_acquisition_mode(self) -> None:
        with self.assertRaises(SystemExit):
            main(
                [
                    "restore-to-new",
                    "--channel",
                    "rc",
                    "--public-origin",
                    "https://anime.example",
                    "--backup",
                    "C:/backup",
                    "--dry-run",
                ],
                runtime=_Runtime().runtime,
            )

    def test_restore_none_mode_is_explicit_and_plan_remains_secret_free(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "restore-to-new",
                    "--channel",
                    "rc",
                    "--public-origin",
                    "https://anime.example",
                    "--backup",
                    "C:/backup",
                    "--protection-none",
                    "--dry-run",
                    "--json",
                ],
                runtime=_Runtime().runtime,
            )

        self.assertEqual(code, EXIT_SUCCESS)
        rendered = output.getvalue()
        self.assertIn('"mode": "restore-to-new"', rendered)
        self.assertNotIn("passphrase", rendered.casefold())
        self.assertNotIn("one-time-key", rendered.casefold())

    def test_listen_accepts_alternate_loopback_without_direct_warning(self) -> None:
        endpoint = _listen("127.0.0.2:8088")
        self.assertEqual(endpoint.host, "127.0.0.2")
        self.assertFalse(endpoint.direct_exposure_accepted)

    def test_direct_listen_requires_separate_explicit_acceptance(self) -> None:
        endpoint = _listen("0.0.0.0:8088")
        with self.assertRaises(InstallerError):
            InstallRequest(
                mode=InstallerMode.FRESH,
                selector=ReleaseSelector(channel="rc"),
                public_origin="https://anime.example",
                listen=endpoint,
            )

    def test_noninteractive_requires_explicit_acceptance(self) -> None:
        runtime = _Runtime().runtime
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "install",
                    "--channel",
                    "rc",
                    "--public-origin",
                    "https://anime.example",
                    "--non-interactive",
                    "--json",
                ],
                runtime=runtime,
            )

        self.assertEqual(code, EXIT_VALIDATION)
        self.assertIn("INSTALL_PLAN_ACCEPTANCE_REQUIRED", output.getvalue())

    def test_dry_run_does_not_execute(self) -> None:
        holder = _Runtime()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "install",
                    "--channel",
                    "rc",
                    "--public-origin",
                    "https://anime.example",
                    "--dry-run",
                    "--json",
                ],
                runtime=holder.runtime,
            )

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn('"planDigest"', output.getvalue())
        self.assertIn('"transportSource": "github"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
