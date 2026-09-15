"""Prepare the existing production Stage-0 inside one isolated Formal Guest."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from installer.bootstrap import (
    BOOTSTRAP_AUTHORITY_ROOT, _MATERIALS_FILE, _OFFLINE_CARRIER, _STAGE0_MODEL,
    commit_bootstrap_authorization, parse_gh_cli_version_output,
)

GH_DEB_NAME = 'gh_2.97.0_linux_amd64.deb'
GH_DEB_SHA256 = '7c7fa3bb890db0934baf65910d97b8c0fa437b2e590f7f7daf6bdf82c5c486d7'
GH_EXE_SHA256 = '141507c337e8b202ad398550c3b73d72f5af92e86f71665214538a81efd4c409'


def _regular_bytes(path, maximum):
    before=path.lstat()
    if (path.is_symlink() or not path.is_file() or before.st_uid != 0
            or before.st_nlink != 1 or before.st_mode & 0o022
            or not 0 < before.st_size <= maximum):
        raise ValueError('FORMAL_BOOTSTRAP_FILE_INVALID')
    with path.open('rb') as stream:
        opened=os.fstat(stream.fileno())
        raw=stream.read(maximum+1)
        after=os.fstat(stream.fileno())
    identity=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns)
    if identity(before)!=identity(opened) or identity(opened)!=identity(after) or len(raw)!=before.st_size:
        raise ValueError('FORMAL_BOOTSTRAP_FILE_CHANGED')
    return raw


def prepare_online_gh(authority_root):
    """Install the official fixed Guest package; never change a host tool."""
    if os.name != 'posix' or os.geteuid()!=0:
        raise ValueError('FORMAL_BOOTSTRAP_ROOT_REQUIRED')
    package=Path(authority_root)/GH_DEB_NAME
    raw=_regular_bytes(package,20*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=GH_DEB_SHA256:
        raise ValueError('FORMAL_GH_PACKAGE_IDENTITY_MISMATCH')
    subprocess.run(['/usr/bin/dpkg','--install',str(package)],check=True,timeout=300,
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    executable=_regular_bytes(Path('/usr/bin/gh'),64*1024*1024)
    if hashlib.sha256(executable).hexdigest()!=GH_EXE_SHA256:
        raise ValueError('FORMAL_GH_EXECUTABLE_IDENTITY_MISMATCH')
    result=subprocess.run(['/usr/bin/gh','version'],check=True,timeout=15,
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    if parse_gh_cli_version_output(result.stdout).semantic_version!='2.97.0':
        raise ValueError('FORMAL_GH_VERSION_MISMATCH')


def prepare_offline_stage0(authority_root, authority):
    """Verify the whole actual portable release before committing Stage-0."""
    from installer.production import issue_formal_candidate_bound_offline_verifier
    from updater import __version__
    if os.name!='posix' or os.geteuid()!=0:
        raise ValueError('FORMAL_BOOTSTRAP_ROOT_REQUIRED')
    root=Path(authority_root)
    protected=BOOTSTRAP_AUTHORITY_ROOT/_MATERIALS_FILE
    archive=_regular_bytes(protected,2*1024*1024*1024)
    if 'sha256:'+hashlib.sha256(archive).hexdigest()!=authority.installer_materials_identity:
        raise ValueError('FORMAL_BOOTSTRAP_ARCHIVE_IDENTITY_MISMATCH')
    capability=issue_formal_candidate_bound_offline_verifier(root,
        expected_profile_identity=authority.offline_release_trust_profile_identity)
    verifier,trust_temporary=capability._consume()
    try:
        with tempfile.TemporaryDirectory(prefix='animemo-formal-stage0-') as temporary:
            verified=verifier.verify(payload=root/f'animemo-{authority.rc_tag}-portable.tar',
                sidecar=root/'release-attestation.sigstore.json',destination=Path(temporary)/'verified',
                updater_version=__version__)
            release=verified.materials.manifest['release']
            if (release['version']!=authority.rc_tag or release['commit']!=authority.source_sha
                    or verified.materials.installer_archive_sha256!=authority.installer_materials_identity
                    or verified.publication_identity!=authority.publication_identity):
                raise ValueError('FORMAL_BOOTSTRAP_RELEASE_IDENTITY_MISMATCH')
            return commit_bootstrap_authorization(dict(schemaVersion=1,state='PRIVILEGE_ALLOWED',
                repository=authority.repository,tag=authority.rc_tag,releaseCommit=authority.source_sha,
                # This field is the verified transport-sidecar identity, not a claim digest.
                releaseAttestationIdentity=verified.release_attestation_identity,
                installerMaterials=dict(path=str(protected),sha256=authority.installer_materials_identity,size=len(archive)),
                stage0=dict(model=_STAGE0_MODEL,carrier=_OFFLINE_CARRIER,
                    verifierIdentity=verifier._profile.verifier_identity),
                verifiedAt=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')))
    finally:
        trust_temporary.cleanup()
