"""Synthetic consent and secrets; real Windows directory holds, never native input."""
import pickle
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts import guest_batch_scope as scope
from scripts import development_session_owner as owners
from scripts.tests import test_local_candidate_development as development_fixtures
from scripts.tests.formal_windows_pretrust_fixture import private_windows_test_directory


class LocalBatchScopeTests(unittest.TestCase):
    def setUp(self):
        fixture = development_fixtures.DevelopmentPlanTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.plan = fixture.plan
        self.root = Path(self.enterContext(private_windows_test_directory()))
        body = dict(purpose='LOCAL_INSTALLER_DEVELOPMENT', authorization_id=scope.DEVELOPMENT_AUTHORIZATION,
            confirmed_utc_seconds=time.time(), material_identity=scope.material_identity(self.plan),
            initial_plan_digest=self.plan.plan_digest)
        self.authorization = scope.LocalBatchAuthorization(scope._ISSUER, body=body, root=self.root,
            holds=ExitStack(), monotonic_now=time.monotonic())
        self.addCleanup(self.authorization.close)

    def next_plan(self, index):
        from scripts.candidate_vm_harness import canonical_json_bytes, sha256_bytes
        profiles = tuple(replace(p, clone_identity='sha256:'+f'{index:062x}{i:02x}')
            for i, p in enumerate(self.plan.profiles))
        plan = replace(self.plan, session_id=f'{index:032x}', profiles=profiles)
        return replace(plan, plan_digest=sha256_bytes(canonical_json_bytes(plan.identity_body())))

    def test_one_capture_twelve_distinct_rounds_and_no_replenishment(self):
        with self.assertRaises(scope.ControllerFailure):
            self.authorization.consume_capture()
        self.authorization.reserve_round(self.plan).close()
        self.authorization.consume_capture()
        original = (self.root/'capture-attempt.json').read_bytes()
        for index in range(2, 13):
            self.authorization.reserve_round(self.next_plan(index)).close()
            with self.assertRaises(scope.ControllerFailure):
                self.authorization.consume_capture()
        with self.assertRaises(scope.ControllerFailure):
            self.authorization.reserve_round(self.next_plan(13))
        self.assertEqual((self.root/'capture-attempt.json').read_bytes(), original)

    def test_plan_replay_changed_material_and_wrong_purpose_are_rejected(self):
        self.authorization.reserve_round(self.plan).close()
        for plan in (self.plan, replace(self.next_plan(2), source_sha='f'*40),
                     self.next_plan(2).identity_body()):
            with self.subTest(plan=type(plan).__name__), self.assertRaises(scope.ControllerFailure):
                self.authorization.reserve_round(plan)

    def test_expiry_close_and_serialization_cannot_reopen_reservation(self):
        reservation = self.authorization.reserve_round(self.plan)
        for value in (reservation, self.authorization):
            with self.assertRaises(TypeError):
                pickle.dumps(value)
        self.authorization.expires_utc = time.time()-1
        with self.assertRaises(scope.ControllerFailure):
            reservation.require_open()
        self.authorization.close()
        self.assertTrue(reservation._closed)
        with self.assertRaises(scope.ControllerFailure):
            self.authorization.consume_capture()

    def test_owner_exposes_capture_and_round_budgets_separately(self):
        owner = owners.acquire_confirmed_development_owner(authorization=self.authorization,
            material_identity=owners._material(self.plan))
        try:
            self.assertEqual(owner.record['capture_limit'], 1)
            self.assertEqual(owner.record['round_limit'], 12)
            secret = bytearray(b'synthetic-test-only')
            owner._secret, owner._attempts, owner._completed = secret, 1, 1
            owner._idle_since = time.monotonic()-1801
            with self.assertRaises(owners.DevelopmentOwnerError):
                owner._live()
            self.assertEqual(secret, b'')
            self.assertEqual(owner.record['close_reason'], 'DEVELOPMENT_SESSION_IDLE_EXPIRED')
        finally:
            owner.dispose()

    def test_retired_scope_rejected_before_native_confirmation(self):
        with mock.patch.object(scope, 'WindowsConsoleCapture') as console:
            with self.assertRaises(scope.ControllerFailure):
                scope.confirm_local_batch(authorization_id=scope.RETIRED_FORMAL_AUTHORIZATION,
                    purpose='LOCAL_INSTALLER_DEVELOPMENT', plan=self.plan)
            console.assert_not_called()


class ConfirmedOwnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        from scripts.tests.test_local_candidate_development import DevelopmentBatchFlowTests
        from scripts import development_capture_scope
        setup = DevelopmentBatchFlowTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        self.fixture = setup.fixture
        self.fixture.batch.close()
        self.plan = self.fixture.plan
        self.root = Path(self.enterContext(private_windows_test_directory()))
        body = dict(purpose='LOCAL_INSTALLER_DEVELOPMENT',authorization_id=scope.DEVELOPMENT_AUTHORIZATION,
            confirmed_utc_seconds=time.time(),material_identity=scope.material_identity(self.plan),
            initial_plan_digest=self.plan.plan_digest)
        self.authorization = scope.LocalBatchAuthorization(scope._ISSUER,body=body,root=self.root,
            holds=ExitStack(),monotonic_now=time.monotonic())
        self.owner = owners.acquire_confirmed_development_owner(authorization=self.authorization,
            material_identity=owners._material(self.plan))
        self.addCleanup(self.owner.dispose)
        self.console,self.secret=self.fixture.console,self.fixture.secret
        self.original=bytes(self.secret)
        self.enterContext(mock.patch.object(development_capture_scope,'AUTHORIZATION',scope.DEVELOPMENT_AUTHORIZATION))

    def test_new_scope_three_targets_nine_grants_one_capture_and_final_wipe(self):
        from scripts.tests.test_development_session_owner import DevelopmentMemoryOwnerTests
        DevelopmentMemoryOwnerTests.test_persistent_capability_uses_all_nine_existing_verified_delivery_grants(self)
        self.assertTrue(self.authorization._capture_consumed)
        self.assertEqual(self.owner.record['capture_attempts'],1)

    def test_new_scope_refuses_missing_owner_before_any_capture_or_reservation(self):
        from scripts.candidate_batch_session import CandidateBatch
        with self.assertRaisesRegex(scope.ControllerFailure,'DEVELOPMENT_SESSION_OWNER_REQUIRED'):
            CandidateBatch(self.fixture.provider,self.plan,authorization_id=scope.DEVELOPMENT_AUTHORIZATION,
                local_authorization=self.authorization)
        self.console.capture.assert_not_called()
        self.assertEqual(self.authorization._reservations,[])

    def test_cleaned_product_failure_reuses_owner_and_original_hard_deadline(self):
        import io
        from contextlib import redirect_stdout
        from scripts.candidate_batch_session import CandidateBatch
        from scripts.tests.test_development_session_owner import DevelopmentMemoryOwnerTests
        batch=CandidateBatch(self.fixture.provider,self.plan,authorization_id=scope.DEVELOPMENT_AUTHORIZATION,
            development_owner=self.owner)
        self.addCleanup(batch.close)
        with batch.operation('BOOTSTRAP',self.plan.profiles[0]),redirect_stdout(io.StringIO()):
            batch.capture_after_bootstrap_observation(self.plan.profiles[0])
        first_deadline=self.owner._deadline
        self.owner.finish_round(DevelopmentMemoryOwnerTests.completed_failure(self,batch))
        second=DevelopmentMemoryOwnerTests.begin(self,2)
        self.assertEqual(self.owner._deadline,first_deadline)
        self.assertEqual(second.record['session_capture_attempts'],0)
        self.assertEqual(self.owner.record['last_reserved_round'],2)
        self.console.capture.assert_called_once()
        self.owner.finish_round(DevelopmentMemoryOwnerTests.completed_failure(self,second))
        self.assertEqual(self.owner.record['state'],'READY')

    def test_preparation_is_bounded_in_flight_without_a_secret_use(self):
        from scripts.candidate_batch_session import CandidateBatch
        batch=CandidateBatch(self.fixture.provider,self.plan,authorization_id=scope.DEVELOPMENT_AUTHORIZATION,
            development_owner=self.owner)
        self.addCleanup(batch.close)
        self.owner._secret=bytearray(b'synthetic-existing-secret')
        self.owner._completed=self.owner._attempts=1
        with batch.operation('PREPARATION',self.plan.profiles[0]):
            self.owner._idle_since=time.monotonic()-1801
            self.owner._live()
            with self.assertRaises(owners.DevelopmentOwnerError):
                self.owner.require_batch(batch)
        batch.close()
        self.assertTrue(self.owner.closed)
        self.assertIsNone(self.owner._secret)
