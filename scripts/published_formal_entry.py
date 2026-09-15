"""Fresh-process Formal input readback and explicitly confirmed execution."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

from release.formal_candidate_history import read_published_candidate_history
from release.formal_input_readback import read_published_inputs, verify_current_qualification
from release.formal_vm_controller import FormalExecutionContext, FormalProducerError
from release.formal_windows_pretrust import create_windows_private_directory, hold_windows_private_path_authority
from scripts import candidate_vm_harness as h
from scripts.development_source import acquire_development_source
from scripts.guest_console_capture import WindowsConsoleCapture
from updater.commands import CommandRunner
from updater.source import GitHubPublicRest

WINDOWS_GH_SHA256='e2efa10a5d2ce93cac9bc4b676932b62947c0967c01c8f2c3a9cb4437ad358d3'


class _PinnedWindowsGh(CommandRunner):
    def __init__(self,path):
        super().__init__()
        self.path=Path(path).resolve(strict=True)
        self._check()

    def _check(self):
        if (self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_nlink!=1
                or hashlib.sha256(self.path.read_bytes()).hexdigest()!=WINDOWS_GH_SHA256):
            raise FormalProducerError('FORMAL_WINDOWS_GH_IDENTITY_MISMATCH')

    def run(self,argv,**kwargs):
        if argv!=['/usr/bin/gh','auth','token','--hostname','github.com']:
            raise FormalProducerError('FORMAL_READ_ONLY_GH_COMMAND_REJECTED')
        self._check()
        result=super().run([str(self.path),*argv[1:]],**kwargs)
        self._check()
        return result


def run(args):
    from scripts.formal_vm_harness import (
        _copy_closed_asset, execute_qualified_formal_production, observe_qualified_formal_request,
    )
    from scripts.isolated_guest_validation import _check_checkout
    if args.execute and (not args.confirm_batch or not args.authorization_id):
        raise FormalProducerError('FORMAL_LOCAL_CONFIRMATION_REQUIRED')
    from scripts.guest_batch_scope import RETIRED_FORMAL_AUTHORIZATION
    if args.authorization_id == RETIRED_FORMAL_AUTHORIZATION:
        raise FormalProducerError('FORMAL_RETIRED_SCOPE_REJECTED')
    if not args.output.is_absolute() or args.output.exists() or not args.output.parent.is_dir():
        raise FormalProducerError('FORMAL_ENTRY_OUTPUT_INVALID')
    source_root=Path(__file__).resolve().parents[1]
    tool_sha=subprocess.check_output(['git','-C',str(source_root),'rev-parse','HEAD'],timeout=30).decode().strip()
    tool_tree=subprocess.check_output(['git','-C',str(source_root),'rev-parse','HEAD^{tree}'],timeout=30).decode().strip()
    _check_checkout(tool_sha,tool_tree)
    if args.execute:
        WindowsConsoleCapture().preflight()
    now=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    root=create_windows_private_directory(Path('E:/'),prefix='animemo-published-formal')
    # Public output identifies the private retained evidence directory. No
    # historical scope or Candidate parent capability is opened or recreated.
    report={'schema':'animemo.published-formal-entry/v1','evidence_root':str(root),
        'tool_source_sha':tool_sha,'tool_source_tree':tool_tree,'formal_execution':'NOT_RUN',
        'release_authority_granted':False,'publish_authorized':False}
    pending_output=[]
    try:
        with ExitStack() as stack:
            parent=stack.enter_context(hold_windows_private_path_authority(root,allow_leaf_child_writes=True))
            gh=root/'gh.exe'
            _copy_closed_asset(args.windows_gh,gh,maximum=64*1024*1024)
            rest=GitHubPublicRest(runner=_PinnedWindowsGh(gh))
            archives={}
            for role in ('final','controller','platform'):
                target=root/(role+'.zip')
                _copy_closed_asset(getattr(args,role+'_archive'),target,maximum=16*1024*1024*1024)
                archives[role+'_archive']=target
            loaded,state,qualification=verify_current_qualification(rest=rest,run_id=args.qualification_run_id,
                source_sha=args.source_sha,source_tree=args.source_tree,version=args.version,
                root=root,verified_at=now,**archives)
            if loaded.verified_digest!=args.verified_candidate_digest:
                raise FormalProducerError('FORMAL_EXPECTED_QUALIFICATION_IDENTITY_MISMATCH')
            publication_root=create_windows_private_directory(root,prefix='publication')
            inputs,publication=read_published_inputs(rest=rest,loaded=loaded,asset_root=args.asset_root,
                sidecar_path=args.sidecar,root=publication_root)
            aggregate=root/'candidate-aggregate.json'
            _copy_closed_asset(args.candidate_aggregate,aggregate,maximum=16*1024*1024)
            report['qualification']=qualification
            provider=h.ClosedVmwareProvider()
            with provider.execution_authority(_retain_controller_data=args.execute):
                with h.acquire_candidate_material_authority(loaded.verified_digest,provider=provider,_state_root=state) as material:
                    with provider.bind_candidate_material_authority(material):
                        history=read_published_candidate_history(material_authority=material,aggregate_path=aggregate,
                            expected_aggregate_digest=args.candidate_aggregate_digest)
                        try:
                            request=observe_qualified_formal_request(qualified_candidate=history,provenance_inputs=inputs,
                                publication_input=publication,private_work_root=root,_parent_path_authority=parent)
                            report['published_subject']=request.identity_body()
                            report['history_evidence_digest']=history.candidate_history_evidence_digest
                            if args.execute:
                                with acquire_development_source(provider,source_sha=tool_sha,source_tree=tool_tree) as source:
                                    execution=FormalExecutionContext(accepted_at=now,observed_at=now,
                                        operator_identity=args.authorization_id,run_id='local-'+provider._execution.root.name,
                                        run_attempt=1,correlation_id=provider._execution.root.name,
                                        current_workflow_commit=tool_sha,execution_environment='windows-vmware-private',
                                        tool_identity=source.inventory_digest)
                                    result=execute_qualified_formal_production(qualified_candidate=history,
                                        publication_identity=request.publication_identity,
                                        attestation_claim_identities=request.attestation_claim_identities,
                                        provenance_inputs=inputs,publication_input=publication,execution=execution,
                                        publication_root=publication_root,private_work_root=root,output_root=root/'formal-output',
                                        provider=provider,_parent_path_authority=parent,
                                        local_authorization_id=args.authorization_id,linux_gh_package=args.linux_gh_package,
                                        windows_gh=gh,_output_transaction_sink=pending_output)
                                    audit=result.get('executionReceipt',{}).get('credential_session',{})
                                    resources=audit.get('profile_resources',{})
                                    report.update(formal_execution=('EXECUTED' if result.get('status')=='PASS'
                                        else 'ATTEMPTED' if resources else 'NOT_RUN'),result=result)
                            else:
                                from scripts.formal_product_probe import probe_published_product
                                report['product_preflight']=probe_published_product(loaded=material.loaded,windows_gh=gh,
                                    publication_root=publication_root,output_root=root)
                                report['status']='INPUT_READBACK_ONLY'
                        finally:
                            history.close()
            report['current_holds_released']=True
        # The entry owns the outer material/source/provider/path lifecycles.
        # Their exits must all succeed before any consumable PASS is committed.
        if pending_output:
            transaction,result=pending_output[0]
            transaction.commit(result)
            transaction.cleanup()
            pending_output.clear()
    except BaseException as error:
        for transaction,_result in pending_output:
            try:
                transaction.cleanup()
            except BaseException:
                pass  # Preserve the primary failure and never commit.
        report.update(status='ERROR',failure_code=getattr(error,'code','FORMAL_ENTRY_FAILED'))
        try:
            with args.output.open('x',encoding='utf-8') as stream:
                json.dump(report,stream,ensure_ascii=False,sort_keys=True)
                stream.write('\n')
        except OSError:
            pass
        raise
    with args.output.open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,sort_keys=True)
        stream.write('\n')
    return report
