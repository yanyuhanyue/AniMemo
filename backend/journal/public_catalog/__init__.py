"""The bounded, permission-rechecked live public catalogue read boundary."""
from .contracts import CatalogError
from .query import require_database, resolve_scope
from .readers import (
    read_detail,
    read_directory,
    read_entries,
    read_facets,
    read_field,
    read_summary,
)

__all__ = [
    "CatalogError",
    "read_detail",
    "read_directory",
    "read_entries",
    "read_facets",
    "read_field",
    "read_summary",
    "require_database",
    "resolve_scope",
]
