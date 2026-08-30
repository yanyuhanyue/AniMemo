from __future__ import annotations

import stat
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_LZMA, ZIP_STORED, ZipFile, ZipInfo

from installer.safe_archive import (
    SafeArchiveError,
    ZipExtractionLimits,
    extract_zip_archives,
    wheel_runtime_member_path,
)


class SafeArchiveBoundaryTests(unittest.TestCase):
    @staticmethod
    def _limits(**overrides) -> ZipExtractionLimits:
        values = {
            "max_archives": 2,
            "max_members": 8,
            "max_member_bytes": 1024 * 1024,
            "max_total_bytes": 2 * 1024 * 1024,
            "max_compression_ratio": 100,
            "chunk_bytes": 64,
        }
        values.update(overrides)
        return ZipExtractionLimits(**values)

    @staticmethod
    def _archive(
        path: Path,
        members: list[tuple[str | ZipInfo, bytes]],
        *,
        compression: int = ZIP_STORED,
    ) -> None:
        with ZipFile(path, "w", compression=compression) as archive:
            for name, value in members:
                archive.writestr(name, value)

    @staticmethod
    def _forge_declared_identity(
        path: Path,
        *,
        declared: bytes,
    ) -> None:
        encoded = bytearray(path.read_bytes())
        local = encoded.find(b"PK\x03\x04")
        central = encoded.find(b"PK\x01\x02")
        if local < 0 or central < 0:
            raise AssertionError("test ZIP signatures are absent")
        checksum = zlib.crc32(declared) & 0xFFFFFFFF
        struct.pack_into("<L", encoded, local + 14, checksum)
        struct.pack_into("<L", encoded, local + 22, len(declared))
        struct.pack_into("<L", encoded, central + 16, checksum)
        struct.pack_into("<L", encoded, central + 24, len(declared))
        path.write_bytes(encoded)

    def _extract(
        self,
        root: Path,
        archives: list[Path],
        *,
        limits: ZipExtractionLimits | None = None,
    ) -> Path:
        destination = root / "runtime"
        extract_zip_archives(
            archives,
            destination,
            member_path=wheel_runtime_member_path,
            limits=limits or self._limits(),
        )
        return destination

    def test_path_escape_and_platform_alias_matrix_is_rejected(self) -> None:
        unsafe_names = (
            "../escape.py",
            "/absolute.py",
            "C:/drive.py",
            r"package\backslash.py",
            "package/./dot.py",
            "package//empty.py",
            "package//",
            "package/stream:name.py",
            "package/plain./member.py",
            "package/space /member.py",
            "package/CON",
            "package/con.txt",
            "package/Lpt9.log",
            "package/COM\u00b9.txt",
            "package/conout$.log",
        )
        for unsafe_name in unsafe_names:
            with self.subTest(name=unsafe_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "unsafe.whl"
                self._archive(source, [(unsafe_name, b"unsafe")])
                if "\\" in unsafe_name:
                    normalized = unsafe_name.replace("\\", "/").encode()
                    hostile = unsafe_name.encode()
                    source.write_bytes(
                        source.read_bytes().replace(normalized, hostile)
                    )

                with self.assertRaises(SafeArchiveError):
                    self._extract(root, [source])

                self.assertFalse((root / "runtime").exists())
                self.assertEqual(list(root.glob(".runtime.extract-*")), [])

    def test_duplicate_ignored_wheel_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "duplicate-ignored.whl"
            self._archive(
                source,
                [
                    ("fixture.data/scripts/tool", b"first"),
                    ("fixture.data/scripts/tool", b"second"),
                    ("fixture/runtime.py", b"safe"),
                ],
            )

            with self.assertRaisesRegex(
                SafeArchiveError,
                "SAFE_ARCHIVE_INVALID",
            ):
                self._extract(root, [source])

            self.assertFalse((root / "runtime").exists())
            self.assertEqual(list(root.glob(".runtime.extract-*")), [])

    def test_ignored_wheel_member_alias_collisions_are_rejected(self) -> None:
        aliases = (
            (
                "fixture.data/scripts/Tool",
                "fixture.data/scripts/tool",
            ),
            (
                "fixture.data/scripts/caf\u00e9",
                "fixture.data/scripts/cafe\u0301",
            ),
        )
        for first, second in aliases:
            with self.subTest(first=first, second=second), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "ignored-alias.whl"
                self._archive(
                    source,
                    [
                        (first, b"first"),
                        (second, b"second"),
                        ("fixture/runtime.py", b"safe"),
                    ],
                )

                with self.assertRaisesRegex(
                    SafeArchiveError,
                    "SAFE_ARCHIVE_INVALID",
                ):
                    self._extract(root, [source])

                self.assertFalse((root / "runtime").exists())
                self.assertEqual(list(root.glob(".runtime.extract-*")), [])

    def test_non_regular_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "unsafe.whl"
            link = ZipInfo("package/link.py")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            self._archive(source, [(link, b"target")])

            with self.assertRaises(SafeArchiveError):
                self._extract(root, [source])

            self.assertFalse((root / "runtime").exists())

    def test_nfd_member_is_published_with_nfc_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "safe.whl"
            self._archive(source, [("package/cafe\u0301.py", b"safe")])

            destination = self._extract(root, [source])

            self.assertEqual(
                (destination / "package" / "caf\u00e9.py").read_bytes(),
                b"safe",
            )
            self.assertFalse(
                (destination / "package" / "cafe\u0301.py").exists()
            )

    def test_casefold_collision_in_ancestor_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "collision.whl"
            self._archive(
                source,
                [
                    ("Package/one.py", b"one"),
                    ("package/two.py", b"two"),
                ],
            )

            with self.assertRaises(SafeArchiveError):
                self._extract(root, [source])

            self.assertFalse((root / "runtime").exists())

    def test_member_and_aggregate_declared_limits_are_rejected(self) -> None:
        cases = (
            (
                [("package/large.py", b"12345")],
                self._limits(max_member_bytes=4),
            ),
            (
                [("package/one.py", b"123"), ("package/two.py", b"456")],
                self._limits(max_total_bytes=5),
            ),
        )
        for members, limits in cases:
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "oversize.whl"
                self._archive(source, members)

                with self.assertRaises(SafeArchiveError):
                    self._extract(root, [source], limits=limits)

                self.assertFalse((root / "runtime").exists())

    def test_compression_ratio_limit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "compressed.whl"
            self._archive(
                source,
                [("package/repeated.py", b"A" * 4096)],
                compression=ZIP_DEFLATED,
            )

            with self.assertRaises(SafeArchiveError):
                self._extract(
                    root,
                    [source],
                    limits=self._limits(max_compression_ratio=2),
                )

            self.assertFalse((root / "runtime").exists())

    def test_later_archive_failure_removes_staging_and_keeps_target_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.whl"
            second = root / "second.whl"
            self._archive(first, [("package/safe.py", b"safe")])
            self._archive(second, [("../escape.py", b"unsafe")])

            with self.assertRaises(SafeArchiveError):
                self._extract(root, [first, second])

            self.assertFalse((root / "runtime").exists())
            self.assertEqual(list(root.glob(".runtime.extract-*")), [])
            self.assertFalse((root / "escape.py").exists())

    def test_forged_declared_size_and_crc_cannot_hide_deflate_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "forged.whl"
            self._archive(
                source,
                [("package/payload.py", b"A" * (2 * 1024 * 1024))],
                compression=ZIP_DEFLATED,
            )
            self._forge_declared_identity(source, declared=b"A")

            with self.assertRaisesRegex(
                SafeArchiveError,
                "SAFE_ARCHIVE_INVALID",
            ):
                self._extract(root, [source])

            self.assertFalse((root / "runtime").exists())
            self.assertEqual(list(root.glob(".runtime.extract-*")), [])

    def test_local_header_identity_must_match_central_directory(self) -> None:
        for offset, value in ((14, 0), (18, 1), (22, 1)):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "local-header-drift.whl"
                self._archive(source, [("package/payload.py", b"safe")])
                encoded = bytearray(source.read_bytes())
                local = encoded.find(b"PK\x03\x04")
                self.assertGreaterEqual(local, 0)
                struct.pack_into("<L", encoded, local + offset, value)
                source.write_bytes(encoded)

                with self.assertRaisesRegex(
                    SafeArchiveError,
                    "SAFE_ARCHIVE_INVALID",
                ):
                    self._extract(root, [source])

                self.assertFalse((root / "runtime").exists())
                self.assertEqual(list(root.glob(".runtime.extract-*")), [])

    def test_raw_nul_filename_is_rejected_before_sanitized_alias_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "nul.whl"
            self._archive(source, [("package/memberX.py", b"safe")])
            source.write_bytes(
                source.read_bytes().replace(
                    b"package/memberX.py",
                    b"package/member\x00.py",
                )
            )

            with self.assertRaises(SafeArchiveError):
                self._extract(root, [source])

            self.assertFalse((root / "runtime").exists())

    def test_unsupported_compression_algorithm_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "lzma.whl"
            self._archive(
                source,
                [("package/member.py", b"safe")],
                compression=ZIP_LZMA,
            )

            with self.assertRaises(SafeArchiveError):
                self._extract(root, [source])

            self.assertFalse((root / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
