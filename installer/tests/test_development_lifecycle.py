"""Explicit command/OS fixtures; no Docker socket or system directory access."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from durability.instance import instance_namespace
from installer import development_lifecycle as lifecycle


class RuntimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.running = True
        self.foreign = False
        self.fail_stop = False
        self.identifier = 'a' * 64
        self.plan = SimpleNamespace(plan_digest='sha256:' + 'b' * 64,
            configuration=SimpleNamespace(instance_id='synthetic-instance'))
        self.gate = SimpleNamespace(binding={'session_id': 'c' * 32, 'plan_digest': 'sha256:' + 'd' * 64},
            require_cleanup=lambda p: None, execution_started=True,
            lifetime=SimpleNamespace(instance_stop_timeout=lambda seconds: seconds))
        self.fresh = SimpleNamespace(namespace=instance_namespace('default'),
            runner=SimpleNamespace(run=self.command), _candidate_listener=object(),
            _development_updater_start_attempted=True,
            _development_service_source=SimpleNamespace(observe_installed=lambda: None))
        self.fresh.close_candidate_listener = lambda: setattr(self.fresh, '_candidate_listener', None)
        self.data = {'path': '/data/animemo-instances/default', 'device': 1, 'inode': 2}
        self.hold = mock.patch.object(lifecycle, '_data_identity', return_value=self.data)
        self.hold.start()
        self.addCleanup(self.hold.stop)

    def command(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ['/usr/bin/systemctl', 'show']:
            out = ('animemo-updater@default.service\n/etc/systemd/system/animemo-updater@.service'
                if '--property=Id' in argv else 'inactive\n0')
        elif argv[:2] == ['/usr/bin/docker', 'ps']:
            out = self.identifier
        elif argv[:2] == ['/usr/bin/docker', 'inspect']:
            out = json.dumps([self.identifier, 'default',
                'other-instance' if self.foreign else 'synthetic-instance', 'animemo-default', 'api', self.running])
        elif argv[:2] == ['/usr/bin/docker', 'stop']:
            if self.fail_stop:
                raise OSError('synthetic stop failure')
            self.running = False
            out = self.identifier
        else:
            self.assertEqual(argv, ['/usr/bin/systemctl', 'stop', 'animemo-updater@default.service'])
            out = ''
        return SimpleNamespace(returncode=0, stdout=out)

    def test_happy_retention_stops_instance_then_listener_without_delete(self):
        value = lifecycle.stop_runtime_instance(self.fresh, self.plan, self.gate)
        lifecycle.validate_instance_stop(value, binding=self.gate.binding, installer_plan_digest=self.plan.plan_digest)
        self.assertFalse(self.running)
        self.assertIsNone(self.fresh._candidate_listener)
        self.assertFalse(any(word in call for call in self.calls for word in ('rm', 'down', 'prune', 'remove')))
        self.assertTrue(all(call[:2] != ['/usr/bin/systemctl', 'stop'] or call[-1] == 'animemo-updater@default.service'
            for call in self.calls))

    def test_business_failure_or_cancel_uses_same_fixed_cleanup(self):
        # A terminal business error or cancellation does not revoke cleanup of
        # resources already attempted within this same approved plan.
        for reason in ('BUSINESS_FAILED', 'CANCELLED', 'SUPERVISOR_FAILED'):
            with self.subTest(reason=reason):
                self.running = True
                value = lifecycle.stop_runtime_instance(self.fresh, self.plan, self.gate)
                self.assertEqual(value['result'], 'PASS')

    def test_foreign_container_never_stopped(self):
        self.foreign = True
        with self.assertRaises(lifecycle.DevelopmentStopError) as caught:
            lifecycle.stop_runtime_instance(self.fresh, self.plan, self.gate)
        self.assertIn('CONTAINER_STOP_UNVERIFIED', caught.exception.receipt['failures'])
        self.assertFalse(any(call[:2] == ['/usr/bin/docker', 'stop'] for call in self.calls))
        self.assertIsNone(self.fresh._candidate_listener)

    def test_stop_failure_blocks_pass_but_still_closes_listener(self):
        self.fail_stop = True
        with self.assertRaises(lifecycle.DevelopmentStopError) as caught:
            lifecycle.stop_runtime_instance(self.fresh, self.plan, self.gate)
        self.assertEqual(caught.exception.receipt['result'], 'FAIL')
        self.assertTrue(caught.exception.receipt['listenerClosed'])

    def test_unknown_container_and_wrong_namespace_do_not_stop(self):
        self.identifier = 'not-an-id'
        with self.assertRaises(lifecycle.DevelopmentStopError):
            lifecycle.stop_runtime_instance(self.fresh, self.plan, self.gate)
        self.assertFalse(any(call[:2] == ['/usr/bin/docker', 'stop'] for call in self.calls))
        self.calls.clear()
        self.fresh.namespace = instance_namespace('other')
        with self.assertRaises(ValueError):
            lifecycle.stop_runtime_instance(self.fresh, self.plan, self.gate)
        self.assertEqual(self.calls, [])

    def test_report_loss_cannot_reconstruct_business_stop_from_power_state(self):
        for value in ({}, {'result': 'PASS', 'powerState': 'STOPPED'}):
            with self.assertRaises(lifecycle.DevelopmentStopError):
                lifecycle.validate_instance_stop(value, binding=self.gate.binding)

    def test_no_execution_does_not_stop_an_existing_service(self):
        self.gate.execution_started = False
        value = lifecycle.stop_runtime_instance(self.fresh, None, self.gate)
        self.assertEqual(value['result'], 'NOT_STARTED')
        self.assertEqual(self.calls, [])
