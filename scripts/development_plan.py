"""Development execution and qualified material have distinct source identities."""
from __future__ import annotations

from dataclasses import dataclass

from scripts import candidate_vm_harness as h


@dataclass(frozen=True)
class DevelopmentHarnessPlan(h.CandidateHarnessPlan):
    execution_source_sha: str
    execution_source_tree: str
    execution_inventory_digest: str
    platform_diagnostic: bool = False

    def identity_body(self):
        body = super().identity_body()
        body['schema'] = 'animemo.local-installer-development-vm-plan/v1'
        body['purpose'] = 'LOCAL_INSTALLER_DEVELOPMENT'
        body['materialSourceSha'] = body.pop('sourceSha')
        body['materialSourceTree'] = body.pop('sourceTree')
        body['executionSourceSha'] = self.execution_source_sha
        body['executionSourceTree'] = self.execution_source_tree
        body['executionInventoryDigest'] = self.execution_inventory_digest
        body['r2OriginProofRequiredBeforeClone'] = False
        body['candidateAcceptanceAuthorityGranted'] = False
        body['developmentMode'] = 'PLATFORM_DIAGNOSTIC' if self.platform_diagnostic else 'CLEAN_PREACCEPTANCE'
        return body


def is_development_plan(plan):
    return type(plan) is DevelopmentHarnessPlan


def execution_source(plan):
    from scripts.formal_plan import is_formal_plan
    if is_formal_plan(plan):
        return plan.execution_source_sha, plan.execution_source_tree
    if is_development_plan(plan):
        return plan.execution_source_sha, plan.execution_source_tree
    if type(plan) is h.CandidateHarnessPlan:
        return plan.source_sha, plan.source_tree
    raise h.CandidateHarnessError('DEVELOPMENT_PLAN_TYPE_INVALID')


def from_material_plan(plan, *, execution_source_sha, execution_source_tree, execution_inventory_digest,
                       platform_diagnostic=False):
    if (type(plan) is not h.CandidateHarnessPlan
            or h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())) != plan.plan_digest
            or not h._SHA.fullmatch(execution_source_sha)
            or not h._SHA.fullmatch(execution_source_tree)
            or not h._DIGEST.fullmatch(execution_inventory_digest)
            or type(platform_diagnostic) is not bool):
        raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
    values = {**plan.__dict__, 'execution_source_sha': execution_source_sha,
              'execution_source_tree': execution_source_tree,
              'execution_inventory_digest': execution_inventory_digest, 'plan_digest': '',
              'platform_diagnostic': platform_diagnostic}
    if platform_diagnostic:
        if tuple(profile.profile for profile in plan.profiles) != h.PROFILES:
            raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
        values['profiles'] = plan.profiles[:1]
    provisional = DevelopmentHarnessPlan(**values)
    return DevelopmentHarnessPlan(**{**values, 'plan_digest':
        h.sha256_bytes(h.canonical_json_bytes(provisional.identity_body()))})
