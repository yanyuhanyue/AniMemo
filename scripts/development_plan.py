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
    published_subject_digest: str | None = None
    userspace_probe_digest: str | None = None
    runtime_offline_only: bool = False
    runtime_baseline_only: bool = False
    runtime_authorization_deadline: str | None = None
    runtime_trust_selection_digest: str | None = None
    runtime_retention_policy: str | None = None

    def identity_body(self):
        if (type(self.runtime_offline_only) is not bool or type(self.runtime_baseline_only) is not bool
                or self.runtime_baseline_only and not self.runtime_offline_only):
            raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
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
        if self.runtime_offline_only:
            body['runtimeOfflineOnly'] = True
            body['runtimeAuthorizationDeadline'] = self.runtime_authorization_deadline
            body['runtimeTrustSelectionDigest'] = self.runtime_trust_selection_digest
            body['runtimeRetentionPolicy'] = self.runtime_retention_policy
            if self.runtime_baseline_only:
                body.update(runtimeBaselineOnly=True, sudoCaptureLimit=0)
        if self.published_subject_digest is not None:
            body['developmentMode'] = 'PUBLISHED_PLATFORM_PLAN'
            body['publishedSubjectDigest'] = self.published_subject_digest
        if self.userspace_probe_digest is not None:
            body["developmentMode"] = "USERSPACE_PLATFORM_VALIDATION"
            body["userspaceProbeDigest"] = self.userspace_probe_digest
            body["sudoCaptureLimit"] = 0
        return body


def is_development_plan(plan):
    return type(plan) is DevelopmentHarnessPlan


def confirmed_profile_names(body):
    """Closed DEV selection shared by confirmation and owner readback."""
    mode = body.get('developmentMode')
    offline = body.get('runtimeOfflineOnly', False)
    baseline = body.get('runtimeBaselineOnly', False)
    if (type(offline) is not bool
            or type(baseline) is not bool or baseline and (not offline or body.get('sudoCaptureLimit') != 0)
            or mode not in {'CLEAN_PREACCEPTANCE', 'PLATFORM_DIAGNOSTIC', 'PUBLISHED_PLATFORM_PLAN'}
            or offline and mode != 'CLEAN_PREACCEPTANCE'):
        raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
    if offline:
        from scripts.runtime_development_boundary import (
            RuntimeBoundaryError,
            parse_authorization_deadline,
        )
        try:
            parse_authorization_deadline(body.get('runtimeAuthorizationDeadline'))
        except RuntimeBoundaryError:
            raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID') from None
        if (body.get('runtimeRetentionPolicy') != 'STOP_AND_RETAIN'
                or type(body.get('runtimeTrustSelectionDigest')) is not str
                or not h._DIGEST.fullmatch(body['runtimeTrustSelectionDigest'])):
            raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
    elif any(name in body for name in ('runtimeAuthorizationDeadline', 'runtimeTrustSelectionDigest',
                                       'runtimeRetentionPolicy')):
        raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
    return (('RUNTIME_BASE_OFFLINE',) if offline else
            ('FRESH_BASE',) if mode != 'CLEAN_PREACCEPTANCE' else h.PROFILES)


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
                       platform_diagnostic=False, published_subject_digest=None, userspace_probe_digest=None,
                       runtime_offline_only=False, runtime_baseline_only=False, runtime_authorization_deadline=None,
                       runtime_trust_selection_digest=None, runtime_retention_policy=None):
    if (type(plan) is not h.CandidateHarnessPlan
            or h.sha256_bytes(h.canonical_json_bytes(plan.identity_body())) != plan.plan_digest
            or not h._SHA.fullmatch(execution_source_sha)
            or not h._SHA.fullmatch(execution_source_tree)
            or not h._DIGEST.fullmatch(execution_inventory_digest)
            or type(platform_diagnostic) is not bool
            or type(runtime_offline_only) is not bool
            or type(runtime_baseline_only) is not bool or runtime_baseline_only and not runtime_offline_only
            or runtime_offline_only and (platform_diagnostic or published_subject_digest is not None
                                        or userspace_probe_digest is not None)
            or published_subject_digest is not None and (
                not platform_diagnostic or type(published_subject_digest) is not str
                or not h._DIGEST.fullmatch(published_subject_digest))):
        raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
    if userspace_probe_digest is not None and (
            platform_diagnostic or published_subject_digest is not None
            or type(userspace_probe_digest) is not str or not h._DIGEST.fullmatch(userspace_probe_digest)):
        raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
    values = {**plan.__dict__, 'execution_source_sha': execution_source_sha,
              'execution_source_tree': execution_source_tree,
              'execution_inventory_digest': execution_inventory_digest, 'plan_digest': '',
              'platform_diagnostic': platform_diagnostic, 'runtime_offline_only': runtime_offline_only,
              'runtime_baseline_only': runtime_baseline_only,
              'published_subject_digest': published_subject_digest, 'userspace_probe_digest': userspace_probe_digest,
              'runtime_authorization_deadline': runtime_authorization_deadline,
              'runtime_trust_selection_digest': runtime_trust_selection_digest,
              'runtime_retention_policy': runtime_retention_policy}
    if not runtime_offline_only and any(value is not None for value in (
            runtime_authorization_deadline, runtime_trust_selection_digest, runtime_retention_policy)):
        raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
    if platform_diagnostic or userspace_probe_digest is not None or runtime_offline_only:
        if tuple(profile.profile for profile in plan.profiles) != h.PROFILES:
            raise h.CandidateHarnessError('DEVELOPMENT_PLAN_BINDING_INVALID')
        values['profiles'] = plan.profiles[2:] if runtime_offline_only else plan.profiles[:1]
    provisional = DevelopmentHarnessPlan(**values)
    if userspace_probe_digest is None:
        confirmed_profile_names(provisional.identity_body())
    return DevelopmentHarnessPlan(**{**values, 'plan_digest':
        h.sha256_bytes(h.canonical_json_bytes(provisional.identity_body()))})
