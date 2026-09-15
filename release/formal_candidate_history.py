"""Read full Candidate history against currently held qualified materials.

The result carries historical evidence, never the former process's grant.
The production verifier must still close the immutable publication/provenance,
and the current provider and locally confirmed batch must issue fresh leases.
"""
from __future__ import annotations

import json
from pathlib import Path

from release.candidate import (
    AGGREGATE_RECEIPT_SCHEMA, canonical_json_bytes, reject_duplicate_json_keys,
    sha256_bytes, validate_aggregate_receipt,
)
from release.formal_vm_controller import (
    FormalProducerError, QualifiedCandidateFormalAuthority,
    _QualifiedCandidateFormalRecord, _candidate_source_vm_authority_identity,
)
from release.formal_windows_pretrust import FORMAL_WINDOWS_PRETRUST_PREFIX
from release.materials import read_bounded_release_file

_ISSUER = object()


class PublishedCandidateFormalAuthority(QualifiedCandidateFormalAuthority):
    """New readback capability, distinct from a consumed Candidate continuation."""
    __slots__ = ('__history', '__closed')

    def __init__(self, issuer, record):
        if issuer is not _ISSUER or type(record) is not _QualifiedCandidateFormalRecord:
            raise TypeError('Published history is acquired through readback')
        self.__history, self.__closed = record, False

    def _record(self):
        if self.__closed:
            raise FormalProducerError('FORMAL_HISTORY_AUTHORITY_CLOSED')
        self.__history.candidate_material_authority._require_open()
        return self.__history

    def close(self):
        # Material holds are owned by the enclosing fresh provider lifetime.
        self.__closed = True


def read_published_candidate_history(*, material_authority, aggregate_path: Path,
                                     expected_aggregate_digest: str):
    from scripts.candidate_vm_harness import HeldCandidateMaterialAuthority
    if type(material_authority) is not HeldCandidateMaterialAuthority:
        raise FormalProducerError('FORMAL_CURRENT_MATERIAL_HOLD_REQUIRED')
    loaded = material_authority.loaded
    try:
        raw = read_bounded_release_file(aggregate_path, subject='Candidate history', maximum=16*1024*1024)
        if sha256_bytes(raw) != expected_aggregate_digest:
            raise ValueError('full history bytes differ')
        aggregate = validate_aggregate_receipt(json.loads(raw, object_pairs_hook=reject_duplicate_json_keys))
        candidate = loaded.candidate_input
        expected = {key: candidate[key] for key in
            ('source_sha','source_tree','candidate_version','qualification_run_id')}
        expected.update(candidate_input_digest=loaded.verified['candidate_input_sha256'],
            verified_candidate_digest=loaded.verified_digest, qualification_run_attempt=1)
        if (canonical_json_bytes(aggregate) != raw or aggregate['schema'] != AGGREGATE_RECEIPT_SCHEMA
                or aggregate['result'] != 'PASS' or aggregate['all_profiles_pass'] is not True
                or any(aggregate[key] != value for key,value in expected.items())):
            raise ValueError('history identity differs')
        if any(aggregate['profile_receipts'][name][key] != candidate[material_key]
               for name in ('FRESH_BASE','DOCKER_BASE','RUNTIME_BASE_OFFLINE')
               for key,material_key in (('api_digest','api_oci_digest'),('web_digest','web_oci_digest'),
                   ('postgres_digest','postgres_oci_digest'),('redis_digest','redis_oci_digest'))):
            raise ValueError('historical image identity differs')
        source = dict(base_vm_identity=aggregate['base_vm_identity'],
            original_vm_hashes=aggregate['original_vm_hashes'],
            snapshot_identities=aggregate['snapshot_identities'],
            source_disk_graph_identity=aggregate['source_disk_graph_identity'],
            snapshot_disk_graph_identities=aggregate['snapshot_disk_graph_identities'],
            source_vm_inventory_identity=aggregate['source_vm_inventory_identity'])
        evidence = dict(schema='animemo.formal-candidate-history-readback/v1',
            aggregate_sha256=sha256_bytes(raw), aggregate_bytes=len(raw),
            verified_candidate_digest=loaded.verified_digest,
            candidate_input_digest=loaded.verified['candidate_input_sha256'],
            historical_session_id=aggregate['session_id'], old_process_grant_reconstructed=False)
        record = _QualifiedCandidateFormalRecord(loaded=loaded,
            candidate_material_authority=material_authority,
            candidate_plan_digest=aggregate['plan_digest'],
            candidate_history_evidence_digest=sha256_bytes(canonical_json_bytes(evidence)),
            candidate_material_authority_identity=material_authority.identity,
            candidate_material_tree_inventory_identity=material_authority.tree_inventory_identity,
            candidate_aggregate_receipt_digest=sha256_bytes(raw),
            candidate_profile_receipt_digests={key: value['receipt_digest']
                for key,value in aggregate['profile_results'].items()},
            candidate_source_vm_authority_identity=_candidate_source_vm_authority_identity(**source),
            formal_windows_pretrust_root=loaded.materials.material(
                f'{FORMAL_WINDOWS_PRETRUST_PREFIX}/formal-windows-trust-profile.json').parent,
            candidate_aggregate_receipt_json=raw, **source)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise FormalProducerError('FORMAL_CANDIDATE_HISTORY_INVALID') from error
    return PublishedCandidateFormalAuthority(_ISSUER, record)
