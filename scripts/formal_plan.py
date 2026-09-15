"""A published-product plan has a separate host tool identity and purpose."""
from dataclasses import dataclass

from scripts import candidate_vm_harness as h


@dataclass(frozen=True)
class FormalHarnessPlan(h.CandidateHarnessPlan):
    authority_digest: str
    execution_source_sha: str
    execution_source_tree: str
    execution_inventory_digest: str

    @property
    def purpose(self):
        return 'FORMAL_POSTPUBLICATION'

    @property
    def target_version(self):
        return self.candidate_version

    def identity_body(self):
        body = super().identity_body()
        body.update(schema='animemo.postpublication-formal-vm-plan/v1',purpose=self.purpose,
            authorityDigest=self.authority_digest,executionSourceSha=self.execution_source_sha,
            executionSourceTree=self.execution_source_tree,executionInventoryDigest=self.execution_inventory_digest,
            r2OriginProofRequiredBeforeClone=False)
        return body


def is_formal_plan(plan):
    return type(plan) is FormalHarnessPlan


def from_provider_plan(plan, *, loaded, execution_source_sha, execution_source_tree,
                       execution_inventory_digest):
    if (type(plan) is not h.ClosedVmProviderPlan or plan.purpose != 'FORMAL_POSTPUBLICATION'
            or h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())) != plan.plan_digest
            or not h._SHA.fullmatch(execution_source_sha) or not h._SHA.fullmatch(execution_source_tree)
            or not h._DIGEST.fullmatch(execution_inventory_digest)
            or any(loaded.candidate_input[key] != value for key,value in (
                ('source_sha',plan.source_sha),('source_tree',plan.source_tree),
                ('candidate_version',plan.target_version)))):
        raise h.CandidateHarnessError('FORMAL_PLAN_BINDING_INVALID')
    values = {key:getattr(plan,key) for key in ('source_sha','source_tree','source_vm_identity',
        'source_vm_digest','source_vm_inventory_identity','source_disk_graph_identity',
        'original_vm_hashes','provider_readiness_receipt_digest','session_id','authority_digest')}
    values.update(verified_candidate_digest=loaded.verified_digest,
        candidate_input_digest=loaded.verified['candidate_input_sha256'],
        qualification_run_id=loaded.candidate_input['qualification_run_id'],
        candidate_version=plan.target_version,profiles=tuple(h.CandidateProfilePlan(
            **p.__dict__,installer_profile=h.INSTALLER_PROFILES[p.profile]) for p in plan.profiles),
        execution_source_sha=execution_source_sha,execution_source_tree=execution_source_tree,
        execution_inventory_digest=execution_inventory_digest,plan_digest='')
    provisional=FormalHarnessPlan(**values)
    return FormalHarnessPlan(**{**values,'plan_digest':
        h.sha256_bytes(h.canonical_json_bytes(provisional.identity_body()))})
