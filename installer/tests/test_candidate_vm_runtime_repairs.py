from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability.private_store import AtomicPrivateFile
from installer import production
from installer.production import ProductionFreshInstallPort


class CandidateVmRuntimeRepairTests(unittest.TestCase):
    def test_atomic_private_replacement_preserves_existing_posix_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AtomicPrivateFile(root, "authority.json")
            store.write(b"before")
            existing = store.path.lstat()

            with mock.patch(
                "durability.private_store.os.fchown",
                create=True,
            ) as fchown:
                store.write(b"after")

            fchown.assert_called_once()
            self.assertEqual(
                fchown.call_args.args[1:],
                (existing.st_uid, existing.st_gid),
            )

    def test_adopt_updater_waits_for_the_unix_socket_after_service_start(self) -> None:
        fresh = object.__new__(ProductionFreshInstallPort)
        fresh.namespace = SimpleNamespace(
            name="default",
            updater_service="animemo-updater@default.service",
            updater_socket_path=Path("/run/animemo-updater/default/updater.sock"),
        )
        fresh.runner = mock.Mock()
        release = mock.Mock()
        release.version = "v1.1.0-rc.14"
        release.as_dict.return_value = {"version": release.version}
        plan = SimpleNamespace(release=release)
        fresh.releases = mock.Mock()
        fresh.releases.resolve.return_value = release
        fresh.releases.materials_for.return_value.manifest = {"release": {"version": release.version}}
        fresh._ownership_receipt = mock.Mock(return_value=object())
        fresh._locator = mock.Mock(return_value=object())
        fresh._manifest = mock.Mock(return_value={"release": {"version": release.version}})

        receipt_store = mock.Mock()
        receipt_store.path = Path("/tmp/ownership.json")
        fake_pwd = SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=1001))
        fake_grp = SimpleNamespace(getgrnam=lambda _name: SimpleNamespace(gr_gid=1002))
        with mock.patch.object(
            production.os, "name", "posix"
        ), mock.patch.object(
            production.os, "chown", create=True
        ), mock.patch.dict(
            "sys.modules", {"pwd": fake_pwd, "grp": fake_grp}
        ), mock.patch.object(
            production,
            "LocalOwnershipReceiptStore",
            return_value=receipt_store,
        ), mock.patch.object(
            production, "adopt_initial_release"
        ), mock.patch.object(
            fresh, "_wait_for_updater_socket", create=True
        ) as wait_for_socket:
            fresh.adopt_updater(plan)

        wait_for_socket.assert_called_once_with()
        self.assertEqual(
            fresh.runner.run.call_args.args[0],
            [
                "/usr/bin/systemctl",
                "enable",
                "--now",
                "animemo-updater@default.service",
            ],
        )

    def test_updater_socket_wait_is_bounded_and_requires_socket_mode(self) -> None:
        fresh = object.__new__(ProductionFreshInstallPort)
        fresh.namespace = SimpleNamespace(
            updater_socket_path=Path("/run/animemo-updater/default/updater.sock")
        )
        invalid = SimpleNamespace(st_mode=stat.S_IFREG | 0o660)
        valid = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660)
        sleep = mock.Mock()
        with mock.patch.object(
            Path,
            "lstat",
            side_effect=[FileNotFoundError, invalid, valid],
        ), mock.patch.object(
            production,
            "time",
            SimpleNamespace(sleep=sleep),
            create=True,
        ):
            fresh._wait_for_updater_socket()

        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
