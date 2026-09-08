"""Bounded framing of the existing UTF-8 Data Bundle JSON format.

This module only validates JSON and its root shape. The caller must consume the
iterator completely, validate ``header`` with DataBundleSerializer (entries=[]),
and validate each item with the existing domain serializers before publishing.
Yielding an item does not establish that the remaining document is valid.

Item budgets count the original bytes from the item's opening { to closing },
including interior whitespace. The header budget counts all original document
bytes outside the entries array's [ through ], including a leading UTF-8 BOM,
root keys, punctuation, and whitespace. Array separators/whitespace are skipped
without accumulation. Depth counts the root object as 1 and entries array as 2.
Memory is bounded by one item, the header budget, and one 64 KiB input buffer;
the iterator never retains earlier items or closes the caller's file.
"""

from __future__ import annotations

import json
import math

_READ_BYTES = 64 * 1024
_WHITESPACE = b" \t\r\n"
_ROOT_FIELDS = frozenset({"format", "schema_version", "exported_at", "entries"})
_INVALID_JSON = "Data Bundle must contain valid UTF-8 JSON."


class BundleStreamError(ValueError):
    """A bounded error; parser messages never include input keys or values."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _invalid(detail=_INVALID_JSON):
    return BundleStreamError("invalid_data_bundle", detail)


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise _invalid("Data Bundle numbers must be finite.")
    return number


def _reject_constant(_value):
    raise _invalid("Data Bundle numbers must be finite.")


class _BundleItems:
    def __init__(self, binary_file, *, max_item_bytes, max_header_bytes, max_depth):
        for limit in (max_item_bytes, max_header_bytes, max_depth):
            if type(limit) is not int or limit < 1:
                raise ValueError("Data Bundle parser limits must be positive integers.")
        self.header = None
        self._file = binary_file
        self._max_item_bytes = max_item_bytes
        self._max_header_bytes = max_header_bytes
        self._max_depth = max_depth
        self._buffer = b""
        self._offset = 0
        self._eof = False
        self._in_entries = False
        self._header_bytes = 0
        self._iterator = self._parse()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def _peek(self):
        if self._offset == len(self._buffer):
            if self._eof:
                return None
            # Release the previous buffer before requesting the next one.
            self._buffer = b""
            self._offset = 0
            try:
                chunk = self._file.read(_READ_BYTES)
            except (AttributeError, OSError, TypeError, ValueError):
                raise _invalid("Data Bundle input could not be read.") from None
            if not isinstance(chunk, bytes) or len(chunk) > _READ_BYTES:
                raise _invalid("Data Bundle input must support bounded binary reads.")
            self._buffer = chunk
            if not chunk:
                self._eof = True
                return None
        return self._buffer[self._offset]

    def _take(self):
        value = self._peek()
        if value is None:
            raise _invalid()
        self._offset += 1
        if not self._in_entries:
            self._header_bytes += 1
            if self._header_bytes > self._max_header_bytes:
                raise BundleStreamError(
                    "bundle_header_too_large", "Data Bundle header exceeds its byte limit."
                )
        return value

    def _expect(self, value):
        if self._take() != value:
            raise _invalid()

    def _skip_whitespace(self):
        while (value := self._peek()) is not None and value in _WHITESPACE:
            self._take()

    def _check_depth(self, depth):
        if depth > self._max_depth:
            raise BundleStreamError("bundle_depth_exceeded", "Data Bundle exceeds its nesting depth limit.")

    def _value(self, *, parent_depth, item=False):
        """Frame one value; json.loads remains the JSON grammar authority."""
        raw = bytearray()

        def append():
            if item and len(raw) >= self._max_item_bytes:
                raise BundleStreamError(
                    "bundle_item_too_large", "Data Bundle item exceeds its byte limit."
                )
            value = self._take()
            raw.append(value)
            return value

        first = append()
        if first in (ord("{"), ord("[")):
            stack = [ord("}") if first == ord("{") else ord("]")]
            self._check_depth(parent_depth + 1)
            in_string = False
            escaped = False
            while stack:
                value = append()
                if in_string:
                    if escaped:
                        escaped = False
                    elif value == ord("\\"):
                        escaped = True
                    elif value == ord('"'):
                        in_string = False
                elif value == ord('"'):
                    in_string = True
                elif value in (ord("{"), ord("[")):
                    stack.append(ord("}") if value == ord("{") else ord("]"))
                    self._check_depth(parent_depth + len(stack))
                elif value in (ord("}"), ord("]")) and value != stack.pop():
                    raise _invalid()
        elif first == ord('"'):
            escaped = False
            while True:
                value = append()
                if escaped:
                    escaped = False
                elif value == ord("\\"):
                    escaped = True
                elif value == ord('"'):
                    break
        else:
            while (value := self._peek()) is not None:
                if value in _WHITESPACE or value in b",}]":
                    break
                append()
        try:
            return json.loads(
                raw.decode("utf-8"), parse_float=_finite_float, parse_constant=_reject_constant
            )
        except BundleStreamError:
            raise
        except (UnicodeError, ValueError, RecursionError):
            raise _invalid() from None

    def _entries(self):
        self._check_depth(2)
        self._in_entries = True
        self._expect(ord("["))
        self._skip_whitespace()
        if self._peek() != ord("]"):
            while True:
                if self._peek() != ord("{"):
                    raise _invalid("Data Bundle entries must contain JSON objects.")
                # No local item variable survives the yield or accumulates items.
                yield self._value(parent_depth=2, item=True)
                self._skip_whitespace()
                if self._peek() == ord("]"):
                    break
                self._expect(ord(","))
                self._skip_whitespace()
        self._expect(ord("]"))
        self._in_entries = False

    def _parse(self):
        header = {}
        seen = set()
        if self._peek() == 0xEF:
            for value in b"\xef\xbb\xbf":
                self._expect(value)
        self._skip_whitespace()
        self._check_depth(1)
        self._expect(ord("{"))
        self._skip_whitespace()
        if self._peek() != ord("}"):
            while True:
                if self._peek() != ord('"'):
                    raise _invalid()
                key = self._value(parent_depth=1)
                if key not in _ROOT_FIELDS:
                    raise _invalid("Data Bundle contains an unsupported root field.")
                if key in seen:
                    raise _invalid("Data Bundle contains a duplicate root field.")
                seen.add(key)
                self._skip_whitespace()
                self._expect(ord(":"))
                self._skip_whitespace()
                if key == "entries":
                    yield from self._entries()
                else:
                    header[key] = self._value(parent_depth=1)
                self._skip_whitespace()
                if self._peek() == ord("}"):
                    break
                self._expect(ord(","))
                self._skip_whitespace()
        self._expect(ord("}"))
        self._skip_whitespace()
        if self._peek() is not None:
            raise _invalid("Data Bundle contains trailing content.")
        if seen != _ROOT_FIELDS:
            raise _invalid("Data Bundle is missing required root fields.")
        self.header = header


def iter_bundle_items(binary_file, *, max_item_bytes, max_header_bytes=4096, max_depth=128):
    """Yield raw decoded entries; ``iterator.header`` is set after valid EOF.

    The returned objects are unnormalized JSON objects. Domain validation, bundle
    version checks, and atomic publication are the caller's responsibilities.
    ``header`` stays None on partial consumption or any parsing failure.
    """
    return _BundleItems(
        binary_file,
        max_item_bytes=max_item_bytes,
        max_header_bytes=max_header_bytes,
        max_depth=max_depth,
    )
