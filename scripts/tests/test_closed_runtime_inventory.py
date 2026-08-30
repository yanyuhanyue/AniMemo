from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import closed_runtime_inventory
from scripts.closed_runtime_inventory import (
    closed_runtime_cli_root,
    closed_runtime_inventory_digest,
    main,
)


class ClosedRuntimeInventoryCliTests(unittest.TestCase):
    @unittest.skipUnless(
        closed_runtime_inventory._descriptor_inventory_available(),
        "descriptor-relative inventory contract",
    )
    def test_descriptor_inventory_rejects_directory_membership_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "member").write_bytes(b"fixed")
            real_listdir = closed_runtime_inventory.os.listdir
            calls = 0

            def changing_listdir(descriptor):
                nonlocal calls
                calls += 1
                names = list(real_listdir(descriptor))
                return names if calls == 1 else names + ["late-member"]

            with (
                mock.patch.object(
                    closed_runtime_inventory.os,
                    "listdir",
                    side_effect=changing_listdir,
                ),
                self.assertRaisesRegex(SystemExit, "42"),
            ):
                closed_runtime_inventory._descriptor_inventory(root)

    def test_cli_fails_closed_without_descriptor_inventory_authority(self) -> None:
        with (
            mock.patch.object(
                closed_runtime_inventory,
                "_descriptor_inventory_available",
                return_value=False,
            ),
            mock.patch.object(
                closed_runtime_inventory,
                "closed_runtime_cli_root",
            ) as resolve_root,
        ):
            self.assertEqual(main(["/var/lib/animemo/formal-authority/" + "a" * 64]), 66)
        resolve_root.assert_not_called()

    def test_cli_positive_fixed_root_uses_descriptor_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixed_root = Path(directory) / "formal-authority"
            fixed_root.mkdir()
            authority = fixed_root / ("b" * 64)
            authority.mkdir()
            expected = "sha256:" + "c" * 64
            with (
                mock.patch.object(
                    closed_runtime_inventory,
                    "_FIXED_GUEST_AUTHORITY_ROOTS",
                    (fixed_root,),
                ),
                mock.patch.object(
                    closed_runtime_inventory,
                    "_descriptor_inventory_available",
                    return_value=True,
                ),
                mock.patch.object(
                    closed_runtime_inventory,
                    "closed_runtime_inventory_digest",
                    return_value=expected,
                ) as inventory,
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(main([authority.as_posix()]), 0)

            inventory.assert_called_once_with(authority)
            self.assertEqual(output.getvalue(), expected + "\n")

    def test_inventory_rejects_multiply_linked_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.write_bytes(b"shared")
            second.hardlink_to(first)

            with self.assertRaisesRegex(SystemExit, "43"):
                closed_runtime_inventory_digest(root)

    def test_cli_maps_only_fixed_guest_authority_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixed_root = Path(directory) / "fixed-authority"
            fixed_root.mkdir()
            identity = "a" * 64
            authority = fixed_root / identity
            authority.mkdir()
            canonical = authority.as_posix()

            with mock.patch.object(
                closed_runtime_inventory,
                "_FIXED_GUEST_AUTHORITY_ROOTS",
                (fixed_root,),
            ):
                self.assertEqual(closed_runtime_cli_root(canonical), authority)
                for rejected in (
                    (Path(directory) / identity).as_posix(),
                    canonical + "/child",
                    canonical + "/../" + identity,
                ):
                    with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                        closed_runtime_cli_root(rejected)

    def test_cli_rejects_arbitrary_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "private.txt"
            sentinel.write_bytes(b"private")

            with (
                mock.patch.object(
                    closed_runtime_inventory,
                    "_descriptor_inventory_available",
                    return_value=True,
                ),
                redirect_stdout(StringIO()),
            ):
                status = main([str(root)])

            self.assertEqual(status, 65)
            self.assertEqual(sentinel.read_bytes(), b"private")


if __name__ == "__main__":
    unittest.main()
