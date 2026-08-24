"""AniMemo release identity and immutable artifact contract.

The package initializer deliberately stays standard-library-only.  Release
contract dependencies are loaded when a public contract export is first used,
so lightweight ``python -m release.*`` entrypoints remain self-contained.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = [
    "ReleaseContractError",
    "assert_tag_absent",
    "build_manifest",
    "promote_manifest",
    "resolve_prerelease",
    "validate_manifest",
]

if TYPE_CHECKING:
    from .contract import (
        ReleaseContractError,
        assert_tag_absent,
        build_manifest,
        promote_manifest,
        resolve_prerelease,
        validate_manifest,
    )


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(".contract", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
