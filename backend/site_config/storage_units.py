"""Explicit storage capacity units used by the API and admin UI."""

DECIMAL_GB_BYTES = 1_000_000_000
# 1 GiB, kept as an explicit constant so callers cannot label binary bytes GB.
BINARY_GIB_BYTES = 1_073_741_824


def gb_decimal_to_bytes(value):
    return max(0, int(round(float(value) * DECIMAL_GB_BYTES)))


def bytes_to_gb_decimal(value):
    return float(value or 0) / DECIMAL_GB_BYTES


def gib_to_bytes(value):
    return max(0, int(round(float(value) * BINARY_GIB_BYTES)))


def bytes_to_gib(value):
    return float(value or 0) / BINARY_GIB_BYTES
