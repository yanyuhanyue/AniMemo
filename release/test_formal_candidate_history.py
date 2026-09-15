"""Full v4 history validators with synthetic held material; no VM or grant replay."""
import copy
import pickle
import unittest
from types import SimpleNamespace
from unittest import mock

from release import formal_candidate_history as history
from release.candidate import canonical_json_bytes, sha256_bytes
from scripts.candidate_vm_harness import HeldCandidateMaterialAuthority
from scripts.tests import test_candidate_plugin_acceptance as fixtures


class FormalCandidateHistoryTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.PluginAcceptanceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root,self.receipt=fixture.root,fixture.receipt
        self.loaded=SimpleNamespace(**fixture.loaded.__dict__)
        self.loaded.candidate_input=copy.deepcopy(self.loaded.candidate_input)
        for name,detail in self.receipt['profile_receipts'].items():
            for role in ('api','web','postgres','redis'):
                detail[role+'_digest']=self.loaded.candidate_input[role+'_oci_digest']
            self.receipt['profile_results'][name.lower()]['receipt_digest']=sha256_bytes(canonical_json_bytes(detail))
        fixtures.seal(self.receipt)
        self.loaded.materials=SimpleNamespace(material=lambda name:self.root/name)
        self.material=object.__new__(HeldCandidateMaterialAuthority)
        self.material._closed=False
        self.material._loaded=self.loaded
        self.material._identity='sha256:'+'1'*64
        self.material._tree_inventory_identity='sha256:'+'2'*64
        self.path=self.root/'history.json'

    def read(self, receipt=None, raw=None):
        raw=canonical_json_bytes(self.receipt if receipt is None else receipt) if raw is None else raw
        self.path.write_bytes(raw)
        return history.read_published_candidate_history(material_authority=self.material,
            aggregate_path=self.path,expected_aggregate_digest=sha256_bytes(raw))

    def test_full_history_gets_new_capability_and_current_hold_expiry_revokes_it(self):
        authority=self.read()
        self.assertIs(type(authority),history.PublishedCandidateFormalAuthority)
        self.assertEqual(authority.candidate_aggregate_receipt,self.receipt)
        self.assertNotEqual(authority.candidate_history_evidence_digest,
            authority.candidate_aggregate_receipt_digest)
        with self.assertRaises(TypeError):
            pickle.dumps(authority)
        authority.close()
        self.assertFalse(self.material._closed)
        with self.assertRaises(history.FormalProducerError):
            _=authority.loaded
        authority=self.read()
        self.material._closed=True
        with self.assertRaisesRegex(Exception,'CANDIDATE_MATERIAL_AUTHORITY_CLOSED'):
            _=authority.loaded

    def test_recomputed_outer_digest_does_not_allow_q_source_session_or_profile_splice(self):
        for key,value in (('source_sha','f'*40),('qualification_run_id',999999),
                          ('session_id','f'*32),('plan_digest','sha256:'+'f'*64)):
            receipt=copy.deepcopy(self.receipt)
            receipt[key]=value
            fixtures.seal(receipt)
            with self.subTest(key=key),self.assertRaises(history.FormalProducerError):
                self.read(receipt)
        receipt=copy.deepcopy(self.receipt)
        receipt['profile_receipts'].pop('DOCKER_BASE')
        fixtures.seal(receipt)
        with self.assertRaises(history.FormalProducerError):
            self.read(receipt)

    def test_wrong_current_material_images_and_noncanonical_or_duplicate_json_reject(self):
        for key in ('api_oci_digest','web_oci_digest','postgres_oci_digest','redis_oci_digest'):
            with self.subTest(key=key),mock.patch.dict(self.loaded.candidate_input,{key:'sha256:'+'f'*64}):
                with self.assertRaises(history.FormalProducerError):
                    self.read()
        raw=canonical_json_bytes(self.receipt)
        for encoded in (raw+b' ',b'{"result":"PASS",'+raw[1:],raw[:100]):
            with self.assertRaises(history.FormalProducerError):
                self.read(raw=encoded)

    def test_plain_json_is_not_a_current_material_hold(self):
        with self.assertRaisesRegex(history.FormalProducerError,'CURRENT_MATERIAL_HOLD_REQUIRED'):
            history.read_published_candidate_history(material_authority={'result':'PASS'},
                aggregate_path=self.path,expected_aggregate_digest='sha256:'+'1'*64)
