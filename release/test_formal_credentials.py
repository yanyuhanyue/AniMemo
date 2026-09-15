from copy import deepcopy
import unittest

from release.candidate import canonical_json_bytes, sha256_bytes
from release.formal_acceptance_test_support import build_test_formal_acceptance
from release.formal_vm_controller import FormalProducerError, validate_formal_execution_receipt


def fixture():
    digest='sha256:'+'a'*64
    return build_test_formal_acceptance(rc_tag='v2.0.0-rc.2',rc_commit='1'*40,rc_tree='2'*40,
        **{name:digest for name in ('release_manifest_identity','deployment_contract_identity',
            'installer_materials_identity','api_digest','web_digest','fresh_base_identity',
            'docker_base_identity','runtime_base_identity')},
        accepted_at='2026-09-15T01:00:00Z',observed_at='2026-09-15T01:00:01Z',
        operator_identity='SYNTHETIC_FORMAL_TEST_SCOPE')['formal_evidence']['executionReceipt']


def rehash(receipt):
    receipt['receipt_digest']=sha256_bytes(canonical_json_bytes({k:v for k,v in receipt.items() if k!='receipt_digest'}))
    return receipt


class FormalCredentialConsumerTests(unittest.TestCase):
    def test_complete_synthetic_session_is_consumable_only_with_full_accounting(self):
        receipt=fixture()
        self.assertEqual(validate_formal_execution_receipt(receipt),receipt)

    def test_environment_text_cannot_exempt_missing_session(self):
        for environment in ('windows-vmware-private','test-private-vm','arbitrary-host'):
            with self.subTest(environment=environment):
                receipt=fixture()
                receipt.pop('credential_session')
                receipt['execution_environment']=environment
                with self.assertRaises(FormalProducerError):
                    validate_formal_execution_receipt(rehash(receipt))

    def test_each_cleanup_or_target_failure_rejects_even_with_recomputed_hash(self):
        changes={'session_keys_removed':False,'known_hosts_removed':False,'result':'ERROR',
            'profile':'WRONG_PROFILE','clone_identity':'sha256:'+'d'*64,
            'snapshot_identity':'sha256:'+'e'*64,'power_state':'NOT_STARTED','lease_released':False,
            'cleanup_errors':[{'step':'lease','code':'SYNTHETIC_FAILURE'}]}
        original=fixture()
        for key,value in changes.items():
            with self.subTest(field=key):
                receipt=deepcopy(original)
                receipt['credential_session']['profile_resources']['FRESH_BASE'][key]=value
                with self.assertRaisesRegex(FormalProducerError,'FORMAL_CREDENTIAL_SESSION_INVALID'):
                    validate_formal_execution_receipt(rehash(receipt))

    def test_capture_delivery_and_timestamp_splicing_reject(self):
        original=fixture()
        for mutation in (
            lambda r:r['credential_session']['batch'].update(session_capture_attempts=2),
            lambda r:r['credential_session']['batch']['profiles']['DOCKER_BASE']['FORMAL_WORKLOAD'].update(delivery_completed=0),
            lambda r:r.update(accepted_at='2026-09-15T00:59:59Z'),
            lambda r:r.update(observed_at='2026-09-15T00:59:59Z'),
            lambda r:r.update(current_workflow_commit='9'*40),
        ):
            receipt=deepcopy(original)
            mutation(receipt)
            with self.assertRaisesRegex(FormalProducerError,'FORMAL_CREDENTIAL_SESSION_INVALID'):
                validate_formal_execution_receipt(rehash(receipt))


if __name__=='__main__':
    unittest.main()
