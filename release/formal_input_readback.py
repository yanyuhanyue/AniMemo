"""Fresh read-only Q/publication observations plus canonical local byte checks."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import zipfile

from release.acquisition import validate_attestation_sidecar
from release.candidate import canonical_json_bytes, load_verified_candidate, verify_prepublication_candidate
from release.contract import API_REPOSITORY, WEB_REPOSITORY
from release.formal_vm_controller import FormalProducerError, FormalProvenanceInput
from release.materials import extract_qualification_artifact, read_bounded_release_file
from release.metadata_freshness import validate_qualification_run_metadata
from release.qualification_finalization import _verify_phase_b_controller_authority
from updater.offline import _extract_sigstore_bundle
from updater.source import GitHubPublicRest

REPOSITORY='yanyuhanyue/AniMemo'
BASE='/repos/'+REPOSITORY


def _require(value):
    if not value:
        raise FormalProducerError('FORMAL_INPUT_READBACK_MISMATCH')


def _write(path,value):
    with path.open('xb') as stream:
        stream.write(canonical_json_bytes(value))


def _listed(rest,path,key):
    rows=[]
    seen=set()
    total=None
    for page in range(1,101):
        value=rest.get_json(path+f'?per_page=100&page={page}',label='Formal read-only '+key)
        _require(type(value) is dict and type(value.get(key)) is list
            and type(value.get('total_count')) is int)
        _require(total is None or total==value['total_count'])
        total=value['total_count']
        for item in value[key]:
            _require(type(item) is dict and type(item.get('id')) is int and item['id'] not in seen)
            seen.add(item['id'])
            rows.append(item)
        if len(value[key])<100:
            _require(len(rows)==total)
            return {'total_count':total,key:rows}
    raise FormalProducerError('FORMAL_INPUT_PAGINATION_INCOMPLETE')


def verify_current_qualification(*,rest,run_id,source_sha,source_tree,version,
                                 final_archive,controller_archive,platform_archive,root,verified_at):
    _require(type(rest) is GitHubPublicRest and type(run_id) is int and run_id>0
        and re.fullmatch('[0-9a-f]{40}',source_sha or '')
        and re.fullmatch('[0-9a-f]{40}',source_tree or '')
        and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+-rc\.[1-9][0-9]*',version or ''))
    root=Path(root)
    run=rest.get_json(f'{BASE}/actions/runs/{run_id}',label='Formal Qualification run')
    jobs=_listed(rest,f'{BASE}/actions/runs/{run_id}/jobs','jobs')
    artifacts=_listed(rest,f'{BASE}/actions/runs/{run_id}/artifacts','artifacts')
    selected=validate_qualification_run_metadata(run_metadata=run,jobs_metadata=jobs,
        artifacts_metadata=artifacts,expected_run_id=run_id,expected_sha=source_sha)
    records={}
    for role,name,path in (('final',f'release-qualification-{run_id}',final_archive),
        ('controller',f'controller-authority-{run_id}',controller_archive),
        ('platform',f'platform-qualification-{run_id}',platform_archive)):
        matches=[a for a in artifacts['artifacts'] if a.get('name')==name]
        _require(len(matches)==1)
        item=matches[0]
        _require(item.get('expired') is False and type(item.get('size_in_bytes')) is int)
        before=Path(path).lstat()
        _require(Path(path).is_file() and not Path(path).is_symlink() and before.st_nlink==1
            and before.st_size==item['size_in_bytes'] and before.st_size<=16*1024*1024*1024)
        digest=hashlib.sha256()
        with Path(path).open('rb') as stream:
            while chunk:=stream.read(1024*1024):
                digest.update(chunk)
        _require('sha256:'+digest.hexdigest()==item['digest'])
        records[role]=item
    _require(records['final']['id']==selected['artifactId'])
    qualification=root/'qualification'
    extract_qualification_artifact(Path(final_archive),qualification,qualification_run_id=run_id,
        expected_sha256=records['final']['digest'],require_candidate_contract=True)
    state=root/'verified-candidates'
    result=verify_prepublication_candidate(archive=Path(final_archive),run_metadata=run,jobs_metadata=jobs,
        artifacts_metadata=artifacts,containing_artifact_id=records['final']['id'],
        containing_artifact_api_digest=records['final']['digest'],expected_run_id=run_id,
        expected_source_sha=source_sha,expected_source_tree=source_tree,expected_candidate_version=version,
        verified_at=verified_at,_state_root=state)
    loaded=load_verified_candidate(result['verifiedCandidateDigest'],_state_root=state)
    request=dict(repository=REPOSITORY,candidate_sha=source_sha,candidate_tree=source_tree,
        channel='rc',target_version=version.split('-rc.')[0],release_tag=version,
        workflow=dict(name='Release Producer',path='.github/workflows/release.yml',
            ref=REPOSITORY+'/.github/workflows/release.yml@refs/heads/main',sha=source_sha),
        run=dict(id=str(run_id),attempt=run['run_attempt'],event=run['event']),
        **{role+'_artifact':dict(id=records[role]['id'],name=records[role]['name'],
            api_digest=records[role]['digest']) for role in ('final','controller')})
    phase_b=_verify_phase_b_controller_authority(request,{**artifacts,
        'qualification_archive_path':str(final_archive),'controller_archive_path':str(controller_archive)},
        qualification,loaded.root)
    _require(phase_b['status']=='PASS')
    with zipfile.ZipFile(platform_archive) as archive:
        _require(archive.namelist()==['platform-qualification.json'])
        member=archive.getinfo('platform-qualification.json')
        _require(member.file_size<=8*1024*1024)
        _require(archive.read(member)==(loaded.root/'platform-qualification.json').read_bytes())
    for name,value in (('qualification-run.json',run),('qualification-jobs.json',jobs),
        ('qualification-artifacts.json',artifacts),('phase-b-request.json',request),('phase-b-readback.json',phase_b)):
        _write(root/name,value)
    return loaded,state,result


def read_published_inputs(*,rest,loaded,asset_root,sidecar_path,root):
    """Retain original bytes; the next production preflight verifies every proof."""
    _require(type(rest) is GitHubPublicRest)
    candidate=loaded.candidate_input
    version,source_sha=candidate['candidate_version'],candidate['source_sha']
    publication=rest.get_json(f'{BASE}/releases/tags/{version}',label='Formal immutable release')
    _require(publication.get('tag_name')==version and publication.get('draft') is False
        and publication.get('immutable') is True and publication.get('prerelease') is True
        and publication.get('published_at') is not None)
    tag=rest.get_json(f'{BASE}/git/ref/tags/{version}',label='Formal release tag')
    _require(type(tag) is dict and type(tag.get('object')) is dict and tag['object'].get('type')=='tag')
    tag_object=tag['object']['sha']
    seen=set()
    while tag['object']['type']=='tag':
        digest=tag['object']['sha']
        _require(re.fullmatch('[0-9a-f]{40}',digest or '') and digest not in seen and len(seen)<8)
        seen.add(digest)
        tag=rest.get_json(f'{BASE}/git/tags/{digest}',label='Formal annotated tag')
    _require(tag['object']=={'type':'commit','sha':source_sha,'url':f'https://api.github.com{BASE}/git/commits/{source_sha}'})
    assets=publication.get('assets')
    names={'checksums.txt','deployment-contract.json','installer-materials.tar','release-manifest.json',
        f'animemo-{version}-portable.tar'}
    _require(type(assets) is list and len(assets)==5 and {a.get('name') for a in assets}==names)
    from scripts.formal_vm_harness import _copy_closed_asset
    root=Path(root)
    by_name={item['name']:item for item in assets}
    for name in sorted(names):
        item=by_name[name]
        _require(item.get('state')=='uploaded' and type(item.get('size')) is int
            and 0<item['size']<=4*1024*1024*1024)
        target=root/name
        _copy_closed_asset(Path(asset_root)/name,target,maximum=item['size'])
        digest=hashlib.sha256()
        with target.open('rb') as stream:
            while chunk:=stream.read(1024*1024):
                digest.update(chunk)
        _require(target.stat().st_size==item['size'] and 'sha256:'+digest.hexdigest()==item['digest'])
    for name,key in (('release-manifest.json','release_manifest_sha256'),
        ('deployment-contract.json','deployment_contract_sha256'),('installer-materials.tar','installer_materials_sha256')):
        _require(by_name[name]['digest']==candidate[key])
    sidecar=read_bounded_release_file(Path(sidecar_path),subject='Formal sidecar',maximum=16*1024*1024)
    envelope=validate_attestation_sidecar(sidecar,payload=root/f'animemo-{version}-portable.tar')
    _require(envelope['tag']==version and envelope['commit']==source_sha)
    with (root/'release-attestation.sigstore.json').open('xb') as stream:
        stream.write(sidecar)
    subjects={'api-image':(API_REPOSITORY,candidate['api_oci_digest'],0),
        'web-image':(WEB_REPOSITORY,candidate['web_oci_digest'],0)}
    for role,name in (('release-manifest','release-manifest.json'),
        ('deployment-contract','deployment-contract.json'),('installer-materials','installer-materials.tar')):
        subjects[role]=(name,by_name[name]['digest'],by_name[name]['size'])
    inputs=[]
    for role,(name,digest,size) in subjects.items():
        bundle=root/(role+'.bundle.json')
        request=root/(role+'.request.json')
        _write(bundle,_extract_sigstore_bundle(sidecar,role))
        _write(request,dict(schemaVersion=1,mode='actions-provenance',evidenceName=role,
            subject=dict(name=name,sha256=digest,size=size),workflow='.github/workflows/release.yml',sourceCommit=source_sha))
        inputs.append(FormalProvenanceInput(evidence_name=role,bundle=bundle,request=request,trusted_root=None))
    _require(_extract_sigstore_bundle(sidecar,'portable-asset')==_extract_sigstore_bundle(sidecar,'github-release'))
    bundle,request=root/'github-release.bundle.json',root/'github-release.request.json'
    _write(bundle,_extract_sigstore_bundle(sidecar,'github-release'))
    _write(request,dict(schemaVersion=1,mode='github-release',repository=REPOSITORY,
        repositoryId='1327429673',ownerId='111261350',tag=version,tagCommit=source_sha,tagObject=tag_object,
        expectedSubjects=[dict(name=name,sha256=by_name[name]['digest'],size=by_name[name]['size']) for name in sorted(names)]))
    _write(root/'published-release-readback.json',publication)
    return tuple(inputs),FormalProvenanceInput(evidence_name='github-release',bundle=bundle,request=request,trusted_root=None)
