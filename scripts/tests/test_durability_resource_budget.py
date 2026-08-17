from __future__ import annotations

import unittest
from io import BytesIO

from durability.resource_budget import (
    CopyByteCounter,
    DatabaseExpansionGuard,
    DurabilityResourceBudget,
    ResourceLimitExceeded,
    ResourceLimitReason,
    bounded_copy,
    preflight_copy_sizes,
)


class ResourceBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = DurabilityResourceBudget(
            maximum_compressed_member_bytes=16,
            maximum_uncompressed_database_bytes=32,
            maximum_filesystem_member_bytes=12,
            maximum_total_copied_bytes=20,
            maximum_compression_ratio=4,
        )

    def test_normal_copy_under_limits_passes(self) -> None:
        counter = CopyByteCounter(self.budget.maximum_total_copied_bytes)
        target = BytesIO()

        copied = bounded_copy(
            BytesIO(b"normal"),
            target,
            counter=counter,
            maximum_member_bytes=self.budget.maximum_filesystem_member_bytes,
            expected_size=6,
        )

        self.assertEqual(copied, 6)
        self.assertEqual(counter.copied, 6)
        self.assertEqual(target.getvalue(), b"normal")

    def test_declared_single_member_and_total_preflight_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ResourceLimitExceeded,
            ResourceLimitReason.FILESYSTEM_MEMBER_BYTES,
        ):
            preflight_copy_sizes(
                (13,),
                maximum_member_bytes=self.budget.maximum_filesystem_member_bytes,
                maximum_total_bytes=self.budget.maximum_total_copied_bytes,
            )

        with self.assertRaisesRegex(
            ResourceLimitExceeded,
            ResourceLimitReason.TOTAL_COPY_BYTES,
        ):
            preflight_copy_sizes(
                (11, 10),
                maximum_member_bytes=self.budget.maximum_filesystem_member_bytes,
                maximum_total_bytes=self.budget.maximum_total_copied_bytes,
            )

    def test_actual_stream_exceeds_declared_size_without_writing_extra_bytes(self) -> None:
        counter = CopyByteCounter(self.budget.maximum_total_copied_bytes)
        target = BytesIO()

        with self.assertRaisesRegex(
            ResourceLimitExceeded,
            ResourceLimitReason.DECLARED_SIZE_MISMATCH,
        ):
            bounded_copy(
                BytesIO(b"seven!!"),
                target,
                counter=counter,
                maximum_member_bytes=self.budget.maximum_filesystem_member_bytes,
                expected_size=6,
                chunk_bytes=3,
            )

        self.assertLessEqual(len(target.getvalue()), 6)

    def test_partial_target_writes_are_completed_without_silent_truncation(self) -> None:
        class PartialWriter:
            def __init__(self) -> None:
                self.output = BytesIO()

            def write(self, chunk) -> int:
                accepted = min(2, len(chunk))
                return self.output.write(bytes(chunk[:accepted]))

        target = PartialWriter()
        counter = CopyByteCounter(self.budget.maximum_total_copied_bytes)

        copied = bounded_copy(
            BytesIO(b"complete"),
            target,  # type: ignore[arg-type]
            counter=counter,
            maximum_member_bytes=self.budget.maximum_filesystem_member_bytes,
            expected_size=8,
            chunk_bytes=4,
        )

        self.assertEqual(copied, 8)
        self.assertEqual(counter.copied, 8)
        self.assertEqual(target.output.getvalue(), b"complete")

    def test_zero_progress_target_write_fails_closed(self) -> None:
        class StalledWriter:
            @staticmethod
            def write(_chunk) -> int:
                return 0

        with self.assertRaisesRegex(OSError, "TARGET_WRITE_INCOMPLETE"):
            bounded_copy(
                BytesIO(b"data"),
                StalledWriter(),  # type: ignore[arg-type]
                counter=CopyByteCounter(
                    self.budget.maximum_total_copied_bytes
                ),
                maximum_member_bytes=self.budget.maximum_filesystem_member_bytes,
                expected_size=4,
            )

    def test_actual_single_member_and_total_stream_limits_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ResourceLimitExceeded,
            ResourceLimitReason.FILESYSTEM_MEMBER_BYTES,
        ):
            bounded_copy(
                BytesIO(b"x" * 13),
                BytesIO(),
                counter=CopyByteCounter(20),
                maximum_member_bytes=12,
                chunk_bytes=4,
            )

        counter = CopyByteCounter(10)
        bounded_copy(
            BytesIO(b"a" * 6),
            BytesIO(),
            counter=counter,
            maximum_member_bytes=12,
            chunk_bytes=3,
        )
        with self.assertRaisesRegex(
            ResourceLimitExceeded,
            ResourceLimitReason.TOTAL_COPY_BYTES,
        ):
            bounded_copy(
                BytesIO(b"b" * 5),
                BytesIO(),
                counter=counter,
                maximum_member_bytes=12,
                chunk_bytes=3,
            )

    def test_database_uncompressed_and_compression_ratio_limits_fail_closed(self) -> None:
        uncompressed_guard = DatabaseExpansionGuard(
            compressed_bytes=16,
            budget=self.budget,
        )
        with self.assertRaisesRegex(
            ResourceLimitExceeded,
            ResourceLimitReason.UNCOMPRESSED_DATABASE_BYTES,
        ):
            uncompressed_guard.consume(33)

        ratio_guard = DatabaseExpansionGuard(
            compressed_bytes=4,
            budget=self.budget,
        )
        with self.assertRaisesRegex(
            ResourceLimitExceeded,
            ResourceLimitReason.COMPRESSION_RATIO,
        ):
            ratio_guard.consume(17)

    def test_compressed_member_limit_is_checked_before_expansion(self) -> None:
        with self.assertRaisesRegex(
            ResourceLimitExceeded,
            ResourceLimitReason.COMPRESSED_MEMBER_BYTES,
        ):
            DatabaseExpansionGuard(
                compressed_bytes=17,
                budget=self.budget,
            )


if __name__ == "__main__":
    unittest.main()
