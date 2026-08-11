"""Domain-owned seams for transaction-critical policies and post-commit events."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction


@dataclass(frozen=True)
class JournalMutationContext:
    user_id: int
    journal_entry_id: int
    source: str = "core"


@dataclass(frozen=True)
class UserDeletionContext:
    user_id: int
    actor_id: int | None = None
    source: str = "core"


def _default_policy(_hook_name, value, _context):
    return value


def _default_event(_hook_name, _context):
    return None


_policy_runner = _default_policy
_event_publisher = _default_event


def bind_mutation_ports(*, policy_runner, event_publisher):
    """Bind an outer adapter and return the previous pair for isolated tests."""
    global _policy_runner, _event_publisher
    previous = (_policy_runner, _event_publisher)
    _policy_runner = policy_runner
    _event_publisher = event_publisher
    return previous


def restore_mutation_ports(previous):
    global _policy_runner, _event_publisher
    _policy_runner, _event_publisher = previous


def run_policy(hook_name, value, context):
    return _policy_runner(hook_name, value, context)


def publish_event(hook_name, context):
    """Publish an immutable event only after the surrounding transaction commits."""
    transaction.on_commit(
        lambda: _event_publisher(hook_name, context),
        robust=True,
    )
