"""Synthetic credential boundary for unit tests; never a native capture."""
from datetime import datetime
from release.candidate import canonical_json_bytes, sha256_bytes


def with_test_credentials(delegate):
    class Fixture:
        def __init__(self):
            self.authority = None
            self.observations = {}

        def execute(self, *, authority, profile):
            self.authority = authority
            result = delegate.execute(authority=authority,profile=profile)
            self.observations[profile] = result
            return result

        def finalize_execution(self, execution, *, failure_code=None):
            authority = self.authority
            digest = lambda text: sha256_bytes(text.encode())
            roles = ('BOOTSTRAP_ROTATION','VERIFIED_SUDO','FORMAL_WORKLOAD')
            bases = ('FRESH_BASE','DOCKER_BASE','RUNTIME_BASE_OFFLINE')
            formals = ('FORMAL_FRESH','FORMAL_DOCKER','FORMAL_OFFLINE')
            binding = dict(plan_digest='',session_id='b'*32,source_sha=authority.source_sha,
                source_tree=authority.source_tree,qualification_run_id=1,
                candidate_input_digest=digest('synthetic candidate input'),
                verified_candidate_digest=authority.verified_candidate_digest,
                formal_authority_identity=authority.identity,
                execution_source_sha=execution.current_workflow_commit,execution_source_tree='c'*40)
            plan = dict(schema='animemo.postpublication-formal-vm-plan/v1',purpose='FORMAL_POSTPUBLICATION',
                sessionId=binding['session_id'],sourceSha=binding['source_sha'],sourceTree=binding['source_tree'],
                qualificationRunId=1,candidateInputDigest=binding['candidate_input_digest'],
                verifiedCandidateDigest=authority.verified_candidate_digest,authorityDigest=authority.identity,
                executionSourceSha=execution.current_workflow_commit,executionSourceTree='c'*40,
                executionInventoryDigest=execution.tool_identity,profiles=[])
            resources={}
            for formal,base in zip(formals,bases,strict=True):
                observation=self.observations.get(formal)
                clone=observation.clone_identity if observation else digest(base+' clone')
                snapshot=observation.snapshot_identity if observation else digest(base+' snapshot')
                plan['profiles'].append(dict(profile=base,cloneIdentity=clone,snapshotIdentity=snapshot))
                resources[base]=dict(profile=base,clone_identity=clone,snapshot_identity=snapshot,
                    cleanup_errors=[],lease_released=True,power_state='STOPPED',result='PASS',
                    session_keys_removed=True,known_hosts_removed=True)
            binding['plan_digest']=sha256_bytes(canonical_json_bytes(plan))
            confirmation=dict(schema='animemo.local-batch-confirmation/v1',purpose='FORMAL_POSTPUBLICATION',
                authorization_id=execution.operator_identity,capture_limit=1,round_limit=1,
                initial_plan={**plan,'planDigest':binding['plan_digest']},initial_plan_digest=binding['plan_digest'],
                execution_source_sha=execution.current_workflow_commit,
                confirmed_utc_seconds=datetime.fromisoformat(execution.accepted_at.replace('Z','+00:00')).timestamp())
            batch=dict(authorization_id=execution.operator_identity,purpose='FORMAL_POSTPUBLICATION',
                session_capture_attempts=1,session_capture_completed=1,secret_state='CLOSED',
                secret_cleanup='BEST_EFFORT_COMPLETED',revocation_code=None,binding=binding,
                profiles={base:{role:dict(delivery_attempts=1,delivery_completed=1,target_verified=True,
                    lease_verified=True,operation_result='PASS') for role in roles} for base in bases})
            return execution,dict(schema='animemo.formal-credential-session/v1',confirmation=confirmation,
                batch=batch,cleanup_completed=True,profile_resources=resources)
    return Fixture()
