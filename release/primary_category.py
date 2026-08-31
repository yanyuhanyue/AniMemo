"""Single authority for Release Notes primary-category decisions."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

PRIMARY_LABELS = MappingProxyType({
    "release/feature": "feature",
    "release/fix": "fix",
    "release/improvement": "improvement",
    "release/ui": "ui",
    "release/performance": "performance",
    "release/refactor": "refactor",
    "release/deployment": "deployment",
    "release/ci": "ci",
    "release/dependencies": "dependencies",
    "release/security": "security",
    "release/breaking": "breaking",
    "release/docs": "docs",
})

EXCLUSION_LABELS = MappingProxyType({
    "release/internal": ("internal", "EXCLUDED_INTERNAL"),
    "skip-changelog": ("skip", "EXCLUDED_SKIP"),
})

CATEGORY_ORDER = (
    "feature",
    "fix",
    "improvement",
    "ui",
    "performance",
    "refactor",
    "deployment",
    "ci",
    "dependencies",
    "security",
    "breaking",
    "docs",
    "internal",
    "skip",
)


@dataclass(frozen=True)
class PrimaryCategoryDecision:
    """The classification result for one immutable PR metadata observation."""

    category: str
    decision: str
    primary_labels: tuple[str, ...]
    exclusion_labels: tuple[str, ...]


class PrimaryCategoryError(ValueError):
    """PR metadata does not satisfy the primary-category contract."""

    def __init__(
        self,
        *,
        code: str,
        number: int,
        primary_labels: tuple[str, ...],
        exclusion_labels: tuple[str, ...],
        merge_commit: str,
        observed_updated_at: str,
    ) -> None:
        self.code = code
        self.number = number
        self.primary_labels = primary_labels
        self.exclusion_labels = exclusion_labels
        self.merge_commit = merge_commit
        self.observed_updated_at = observed_updated_at
        primary_text = ",".join(primary_labels)
        exclusion_text = ",".join(exclusion_labels)
        super().__init__(
            f"{code}: PR #{number}; primaryLabels=[{primary_text}]; "
            f"exclusionLabels=[{exclusion_text}]; mergeCommit={merge_commit}; "
            f"observedUpdatedAt={observed_updated_at}"
        )


def validate_primary_category(
    *,
    number: int,
    labels: list[str],
    merge_commit: str,
    observed_updated_at: str,
) -> PrimaryCategoryDecision:
    """Validate and classify one PR using the canonical label policy."""

    primary_labels = tuple(sorted(label for label in labels if label in PRIMARY_LABELS))
    exclusion_labels = tuple(
        sorted(label for label in labels if label in EXCLUSION_LABELS)
    )
    if primary_labels and exclusion_labels:
        raise PrimaryCategoryError(
            code="release_primary_category_exclusion_conflict",
            number=number,
            primary_labels=primary_labels,
            exclusion_labels=exclusion_labels,
            merge_commit=merge_commit,
            observed_updated_at=observed_updated_at,
        )
    if len(primary_labels) > 1:
        raise PrimaryCategoryError(
            code="release_primary_category_conflict",
            number=number,
            primary_labels=primary_labels,
            exclusion_labels=exclusion_labels,
            merge_commit=merge_commit,
            observed_updated_at=observed_updated_at,
        )
    if len(primary_labels) == 1 and not exclusion_labels:
        return PrimaryCategoryDecision(
            category=PRIMARY_LABELS[primary_labels[0]],
            decision="INCLUDED",
            primary_labels=primary_labels,
            exclusion_labels=exclusion_labels,
        )
    if not primary_labels and len(exclusion_labels) == 1:
        category, decision = EXCLUSION_LABELS[exclusion_labels[0]]
        return PrimaryCategoryDecision(
            category=category,
            decision=decision,
            primary_labels=primary_labels,
            exclusion_labels=exclusion_labels,
        )
    if len(exclusion_labels) > 1:
        raise PrimaryCategoryError(
            code="release_primary_category_exclusion_conflict",
            number=number,
            primary_labels=primary_labels,
            exclusion_labels=exclusion_labels,
            merge_commit=merge_commit,
            observed_updated_at=observed_updated_at,
        )
    if not primary_labels and not exclusion_labels:
        raise PrimaryCategoryError(
            code="release_primary_category_unclassified",
            number=number,
            primary_labels=primary_labels,
            exclusion_labels=exclusion_labels,
            merge_commit=merge_commit,
            observed_updated_at=observed_updated_at,
        )
    raise AssertionError("unreachable primary-category state")
