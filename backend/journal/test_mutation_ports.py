from unittest.mock import Mock

from django.db import transaction
from django.test import TestCase

from .mutation_ports import (
    JournalMutationContext,
    bind_mutation_ports,
    publish_event,
    restore_mutation_ports,
    run_policy,
)


class MutationPortTests(TestCase):
    def test_event_is_published_once_after_commit(self):
        publisher = Mock()
        previous = bind_mutation_ports(policy_runner=Mock(), event_publisher=publisher)
        self.addCleanup(restore_mutation_ports, previous)
        context = JournalMutationContext(user_id=7, journal_entry_id=11, source="test")

        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                publish_event("journal.after_update", context)
                publisher.assert_not_called()

        publisher.assert_called_once_with("journal.after_update", context)

    def test_event_is_discarded_when_transaction_rolls_back(self):
        publisher = Mock()
        previous = bind_mutation_ports(policy_runner=Mock(), event_publisher=publisher)
        self.addCleanup(restore_mutation_ports, previous)

        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    publish_event(
                        "journal.after_delete",
                        JournalMutationContext(user_id=7, journal_entry_id=11),
                    )
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass

        publisher.assert_not_called()

    def test_policy_port_returns_adapter_decision_synchronously(self):
        policy = Mock(return_value=False)
        previous = bind_mutation_ports(policy_runner=policy, event_publisher=Mock())
        self.addCleanup(restore_mutation_ports, previous)
        context = JournalMutationContext(user_id=7, journal_entry_id=11)

        self.assertFalse(run_policy("user.before_delete", True, context))
        policy.assert_called_once_with("user.before_delete", True, context)
