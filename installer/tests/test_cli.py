from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from installer.cli import EXIT_SUCCESS, EXIT_VALIDATION, _listen, main
from installer.runtime import (
    Installer,
    InstallerError,
    InstallerMode,
    InstallRequest,
    ReleaseSelector,
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


class _Runtime:
    def __init__(self) -> None:
        from installer.tests.test_runtime import (
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
        )


class InstallerCliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
