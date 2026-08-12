"""AniMemo release identity and immutable artifact contract."""

from .contract import (
    ReleaseContractError,
    assert_tag_absent,
    build_manifest,
    promote_manifest,
    resolve_prerelease,
    validate_manifest,
)

__all__ = [
    "ReleaseContractError",
    "assert_tag_absent",
    "build_manifest",
    "promote_manifest",
    "resolve_prerelease",
    "validate_manifest",
]
