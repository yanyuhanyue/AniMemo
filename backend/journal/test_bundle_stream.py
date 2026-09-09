"""Pure parser tests; runnable without Django setup or a database."""

import gc
import importlib.util
import io
import itertools
import json
import tracemalloc
import unittest
from pathlib import Path

# data_bundle.__init__ imports Django services. Load this pure module directly so
# the same tests run with stdlib unittest and through Django's regular discovery.
_SPEC = importlib.util.spec_from_file_location(
    "_animemo_bundle_stream_tests", Path(__file__).parent / "data_bundle" / "stream.py"
)
_STREAM = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_STREAM)
BundleStreamError = _STREAM.BundleStreamError
iter_bundle_items = _STREAM.iter_bundle_items


HEADER = {
    "format": "animemo-data-bundle",
    "schema_version": 1,
    "exported_at": "2026-09-08T00:00:00+00:00",
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def document(entries):
    return encoded({**HEADER, "entries": entries})


class ChunkReader:
    def __init__(self, value, chunk_size):
        self.source = io.BytesIO(value)
        self.chunk_size = chunk_size
        self.requests = []

    def read(self, size):
        if not 0 < size <= 65536:
            raise AssertionError("Unbounded read requested")
        self.requests.append(size)
        return self.source.read(min(size, self.chunk_size))


class RepeatingReader:
    """Generate a large bundle without ever constructing its full byte string."""

    def __init__(self, count, item):
        prefix = encoded(HEADER)[:-1] + b',"entries":['
        self.parts = itertools.chain(
            [prefix], (item if index == 0 else b"," + item for index in range(count)), [b"]}"]
        )
        self.pending = b""
        self.bytes_read = 0
        self.max_requested = 0

    def read(self, size):
        if not 0 < size <= 65536:
            raise AssertionError("Unbounded read requested")
        self.max_requested = max(self.max_requested, size)
        result = bytearray()
        while len(result) < size:
            if not self.pending:
                self.pending = next(self.parts, b"")
                if not self.pending:
                    break
            take = min(size - len(result), len(self.pending))
            result.extend(self.pending[:take])
            self.pending = self.pending[take:]
        self.bytes_read += len(result)
        return bytes(result)


class BundleStreamTests(unittest.TestCase):
    def parse(self, raw, *, chunk=65536, **limits):
        reader = ChunkReader(raw, chunk)
        iterator = iter_bundle_items(reader, max_item_bytes=limits.pop("max_item_bytes", 1048576), **limits)
        self.assertIs(iter(iterator), iterator)
        result = list(iterator)
        self.assertTrue(reader.requests)
        self.assertLessEqual(max(reader.requests), 65536)
        return result, iterator.header

    def assert_invalid(self, raw, *, code="invalid_data_bundle", **limits):
        iterator = iter_bundle_items(io.BytesIO(raw), max_item_bytes=limits.pop("max_item_bytes", 1048576), **limits)
        with self.assertRaises(BundleStreamError) as context:
            list(iterator)
        self.assertEqual(context.exception.code, code)
        self.assertLess(len(context.exception.detail), 100)
        self.assertIsNone(iterator.header)
        return context.exception

    def test_every_root_field_order(self):
        fields = {**HEADER, "entries": [{"entry": {"title": "动画"}}, {}]}
        for order in itertools.permutations(fields):
            with self.subTest(order=order):
                result, header = self.parse(encoded({key: fields[key] for key in order}), chunk=7)
                self.assertEqual(result, fields["entries"])
                self.assertEqual(header, HEADER)

    def test_boundaries_split_unicode_escapes_and_nested_values(self):
        item = {"entry": {"title": '动画😀\\\\\"}],{', "escaped": "\n\r\t"}, "metadata": [{"nested": [True, None, 1.25e-20, -18]}]}
        raw = document([item, item]).replace(b'"escaped"', b'"escap\\u0065d"')
        for chunk in (1, 2, 3, 4, 7, 31, 64):
            with self.subTest(chunk=chunk):
                self.assertEqual(self.parse(raw, chunk=chunk), ([item, item], HEADER))

    def test_byte_order_mark_and_whitespace(self):
        self.assertEqual(self.parse(b"\xef\xbb\xbf \r\n" + document([]) + b"\t "), ([], HEADER))
        for prefix in (b" \xef\xbb\xbf", b"\xef", b"\xef\xbb", b"\xff\xfe"):
            with self.subTest(prefix=prefix):
                self.assert_invalid(prefix + document([]))

    def test_header_is_only_published_after_complete_valid_eof(self):
        raw = encoded({"entries": [{"entry": {}}, {}], **HEADER})
        source = io.BytesIO(raw)
        iterator = iter_bundle_items(source, max_item_bytes=1000)
        self.assertIsNone(iterator.header)
        self.assertEqual(next(iterator), {"entry": {}})
        self.assertIsNone(iterator.header)
        self.assertEqual(next(iterator), {})
        self.assertIsNone(iterator.header)
        with self.assertRaises(StopIteration):
            next(iterator)
        self.assertEqual(iterator.header, HEADER)
        self.assertFalse(source.closed)
        with self.assertRaises(StopIteration):
            next(iterator)

    def test_long_item_spans_multiple_input_buffers(self):
        item = {"entry": {"review": "番" * 100000}}
        result, header = self.parse(document([item]), max_item_bytes=len(encoded(item)))
        self.assertEqual(result, [item])
        self.assertEqual(header, HEADER)

    def test_exact_item_raw_byte_budget_includes_internal_whitespace(self):
        item = b'{ "entry" : {"title":"' + "动画😀".encode() + b'"} }'
        raw = encoded(HEADER)[:-1] + b',"entries":[ \n' + item + b' \r\n,\t' + item + b" ]}"
        self.assertEqual(len(self.parse(raw, max_item_bytes=len(item))[0]), 2)
        self.assert_invalid(raw, max_item_bytes=len(item) - 1, code="bundle_item_too_large")
        self.assertEqual(self.parse(document([{}]), max_item_bytes=2)[0], [{}])
        self.assert_invalid(document([{}]), max_item_bytes=1, code="bundle_item_too_large")

    def test_exact_cumulative_header_budget_for_both_sides_of_entries(self):
        before = b'\xef\xbb\xbf {"format":"animemo-data-bundle","entries": '
        after = b', "schema_version": 1,"exported_at":"2026-09-08T00:00:00+00:00"} \n'
        raw = before + b"[ {} , {} ]" + after
        limit = len(before) + len(after)
        self.assertEqual(self.parse(raw, max_header_bytes=limit), ([{}, {}], HEADER))
        self.assert_invalid(raw, max_header_bytes=limit - 1, code="bundle_header_too_large")

    def test_oversized_header_key_value_and_whitespace_are_bounded(self):
        for raw in (
            b'{"' + b"x" * 200 + b'":0}',
            b'{"format":"' + b"x" * 200 + b'"}',
            b" " * 200 + document([]),
            document([]) + b" " * 200,
        ):
            with self.subTest(raw=raw[:30]):
                self.assert_invalid(raw, max_header_bytes=150, code="bundle_header_too_large")

    def test_large_total_stream_does_not_retain_prior_items(self):
        count = 5000
        item = {"entry": {"review": "x" * 2048}}
        item_bytes = encoded(item)
        reader = RepeatingReader(count, item_bytes)
        gc.collect()
        tracemalloc.start()
        try:
            iterator = iter_bundle_items(reader, max_item_bytes=len(item_bytes))
            for index, value in enumerate(iterator, start=1):
                if index == 1:
                    self.assertLessEqual(reader.bytes_read, 65536)
                self.assertEqual(value, item)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(index, count)
        self.assertGreater(reader.bytes_read, 10_000_000)
        self.assertEqual(reader.max_requested, 65536)
        self.assertLess(peak, 1024 * 1024)
        self.assertEqual(iterator.header, HEADER)
        print("bundle_stream_memory " + json.dumps({
            "items": index,
            "input_bytes": reader.bytes_read,
            "max_read_bytes": reader.max_requested,
            "peak_traced_bytes": peak,
        }, sort_keys=True))

    def test_missing_unknown_and_duplicate_root_fields(self):
        fields = {**HEADER, "entries": []}
        for missing in fields:
            with self.subTest(missing=missing):
                self.assert_invalid(encoded({key: value for key, value in fields.items() if key != missing}))
        for key, value in fields.items():
            with self.subTest(duplicate=key):
                self.assert_invalid(encoded(fields)[:-1] + b"," + encoded(key) + b":" + encoded(value) + b"}")
        self.assert_invalid(document([])[:-1] + b',"\\u0065ntries":[]}')
        secret_key = "DO_NOT_REFLECT_SECRET_KEY"
        error = self.assert_invalid(encoded({**fields, secret_key: "private"}))
        self.assertNotIn(secret_key, str(error))
        self.assertNotIn("private", str(error))

    def test_invalid_json_shapes_and_punctuation(self):
        invalid = [
            b"", b"[]", b"null", b"{}", b"{", b'{"entries":',
            document([]) + b"{}", document([]) + b"garbage", document([])[:-1],
            document([]).replace(b'"entries":[]', b'"entries":{}'),
            document([]).replace(b'"entries":[]', b'"entries":[null]'),
            document([]).replace(b'"entries":[]', b'"entries":[[]]'),
            document([]).replace(b'"entries":[]', b'"entries":[{},]'),
            document([]).replace(b'"entries":[]', b'"entries":[{} {}]'),
            document([]).replace(b'"entries":[]', b'"entries":[{"x":}]'),
            document([]).replace(b'"entries":[]', b'"entries":[{"x":01}]'),
            document([]).replace(b'"entries":[]', b'"entries":[{"x":1,}]'),
            document([]).replace(b'"entries":[]', b'"entries":[{"x":[}}]'),
            document([]).replace(b'"entries":[]', b'"entries":[{"x":"\\q"}]'),
            document([]).replace(b'"entries":[]', b'"entries":[{"x":"line\n"}]'),
            document([])[:-1] + b",}",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assert_invalid(raw)

    def test_nonfinite_constants_and_overflow_numbers(self):
        for number in (b"NaN", b"Infinity", b"-Infinity", b"1e309", b"-1e9999"):
            with self.subTest(number=number):
                self.assert_invalid(document([]).replace(b'"entries":[]', b'"entries":[{"x":' + number + b"}]"))
                self.assert_invalid(document([]).replace(b'"schema_version":1', b'"schema_version":' + number))

    def test_illegal_utf8_in_header_and_items(self):
        for invalid in (b"\xff", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\xe5\x8a"):
            with self.subTest(invalid=invalid):
                for raw in (
                    document([]).replace(b"animemo-data-bundle", invalid),
                    document([]).replace(b'"entries":[]', b'"entries":[{"x":"' + invalid + b'"}]'),
                ):
                    iterator = iter_bundle_items(ChunkReader(raw, 1), max_item_bytes=10000)
                    with self.assertRaises(BundleStreamError):
                        list(iterator)
                    self.assertIsNone(iterator.header)

    def test_depth_limit_counts_document_containers(self):
        self.assertEqual(self.parse(document([]), max_depth=2), ([], HEADER))
        self.assert_invalid(document([]), max_depth=1, code="bundle_depth_exceeded")
        self.assertEqual(self.parse(document([{}]), max_depth=3)[0], [{}])
        self.assert_invalid(document([{}]), max_depth=2, code="bundle_depth_exceeded")
        item = {"value": [[{"x": "brackets [] {} inside strings"}]]}
        self.assertEqual(self.parse(document([item]), max_depth=6)[0], [item])
        self.assert_invalid(document([item]), max_depth=5, code="bundle_depth_exceeded")
        self.assert_invalid(document([]).replace(b'"schema_version":1', b'"schema_version":[[1]]'), max_depth=2, code="bundle_depth_exceeded")

    def test_bounded_binary_reader_contract(self):
        class BadReader:
            def __init__(self, result):
                self.result = result

            def read(self, size):
                return self.result

        for result in (b"x" * 65537, "{}", None, bytearray(b"{}")):
            with self.subTest(result_type=type(result).__name__):
                iterator = iter_bundle_items(BadReader(result), max_item_bytes=100)
                with self.assertRaises(BundleStreamError) as context:
                    list(iterator)
                self.assertEqual(context.exception.code, "invalid_data_bundle")
                self.assertIsNone(iterator.header)

    def test_reader_failures_are_bounded(self):
        class FailingReader:
            def read(self, size):
                raise OSError("DO_NOT_REFLECT_SOURCE_PATH")

        iterator = iter_bundle_items(FailingReader(), max_item_bytes=100)
        with self.assertRaises(BundleStreamError) as context:
            next(iterator)
        self.assertNotIn("DO_NOT_REFLECT_SOURCE_PATH", str(context.exception))

    def test_invalid_parser_configuration(self):
        for field in ("max_item_bytes", "max_header_bytes", "max_depth"):
            for value in (0, -1, True, 1.5, "20", None):
                with self.subTest(field=field, value=value):
                    limits = {"max_item_bytes": 100, field: value}
                    with self.assertRaises(ValueError):
                        iter_bundle_items(io.BytesIO(document([])), **limits)


if __name__ == "__main__":
    unittest.main()
