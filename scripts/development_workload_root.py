"""Fixed development root operation, embedded after the shared fd-safe copier."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.closed_runtime_inventory import (
        _file_state, _open_directory_chain, closed_runtime_inventory_digest,
    )
    from scripts.candidate_workload_root import _copy_stage, _root_directory


def _seal_development_tree(stage, parent_path, leaf, inventory_digest):
    parent = _root_directory(parent_path)
    try:
        os.mkdir(leaf, 0o700, dir_fd=parent)
        target = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            source = _open_directory_chain(stage)
            try:
                _copy_stage(source, target)
            finally:
                os.close(source)
            os.fchmod(target, 0o500)
        finally:
            os.close(target)
    finally:
        os.close(parent)
    destination = parent_path / leaf
    if closed_runtime_inventory_digest(destination) != inventory_digest:
        raise ValueError('DEVELOPMENT_STAGE_INVENTORY_MISMATCH')
    return destination


def run_fixed_development(*, profile, input_digest, material_inventory_digest,
                          execution_inventory_digest, binding, context, diagnostic,
                          runtime_deadline=None, preparation_check=None,
                          runtime_inputs_inventory_digest=None):
    session_id = binding.get('session_id')
    workload_mode = binding.get('workload_mode')
    if (os.geteuid() != 0 or type(session_id) is not str
            or re.fullmatch('[0-9a-f]{32}', session_id) is None
            or profile not in {'FRESH_BASE', 'DOCKER_BASE', 'RUNTIME_BASE_OFFLINE'}
            or any(type(value) is not str or re.fullmatch('sha256:[0-9a-f]{64}', value) is None
                   for value in (input_digest, material_inventory_digest, execution_inventory_digest))
            or binding.get('execution_inventory_digest') != execution_inventory_digest):
        raise ValueError('DEVELOPMENT_ROOT_SCOPE_INVALID')
    if (workload_mode not in {'CLEAN_PREACCEPTANCE', 'PLATFORM_DIAGNOSTIC', 'PUBLISHED_PLATFORM_PLAN'}
            or workload_mode in {'PLATFORM_DIAGNOSTIC', 'PUBLISHED_PLATFORM_PLAN'} and profile != 'FRESH_BASE'):
        raise ValueError('DEVELOPMENT_ROOT_SCOPE_INVALID')
    if 'runtime_offline_only' in binding and (
            binding['runtime_offline_only'] is not True or workload_mode != 'CLEAN_PREACCEPTANCE'
            or profile != 'RUNTIME_BASE_OFFLINE' or type(context) is not dict
            or context.get('profile') != 'RUNTIME_BASE_OFFLINE'):
        raise ValueError('DEVELOPMENT_ROOT_SCOPE_INVALID')
    if binding.get('runtime_offline_only'):
        if (runtime_deadline is None or preparation_check is None
                or type(runtime_inputs_inventory_digest) is not str
                or re.fullmatch('sha256:[0-9a-f]{64}', runtime_inputs_inventory_digest) is None):
            raise ValueError('DEVELOPMENT_RUNTIME_BOUNDARY_REQUIRED')
        runtime_deadline.check('ROOT_PREPARATION')
        preparation_check(binding, (
            '/var/lib/animemo/prepublication-candidates/v2/' + input_digest[7:],
            '/var/lib/animemo/local-development/' + execution_inventory_digest[7:],
            '/var/lib/animemo/local-development/runtime-inputs/' + binding['runtime_trust_selection_digest'][7:],
            '/var/lib/animemo/local-development/python-runtime',
            '/var/lib/animemo/local-development/profile-report.json',
            '/var/lib/animemo/local-development/runtime-authority/' + session_id + '.json'))
    os.umask(0o077)
    os.chdir('/')
    os.environ.clear()
    os.environ.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8')
    diagnostic.stage('MATERIAL_FINALIZING')
    material_root = _seal_development_tree(
        Path('/tmp') / ('animemo-candidate-' + session_id + '-' + profile),
        Path('/var/lib/animemo/prepublication-candidates/v2'),
        input_digest.removeprefix('sha256:'), material_inventory_digest)
    execution_root = _seal_development_tree(
        Path('/tmp') / ('animemo-development-' + session_id + '-' + profile),
        Path('/var/lib/animemo/local-development'),
        execution_inventory_digest.removeprefix('sha256:'), execution_inventory_digest)
    if binding.get('runtime_offline_only'):
        runtime_deadline.check('ROOT_TRUST_SEAL')
        _seal_development_tree(Path('/tmp') / ('animemo-runtime-inputs-' + session_id),
            Path('/var/lib/animemo/local-development/runtime-inputs'),
            binding['runtime_trust_selection_digest'][7:], runtime_inputs_inventory_digest)
        channel_parent = _root_directory(Path('/var/lib/animemo/local-development/runtime-authority'))
        try:
            fd = os.open(session_id + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=channel_parent)
            with os.fdopen(fd, 'wb') as channel:
                channel.write((json.dumps(binding, ensure_ascii=False, sort_keys=True,
                    separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8'))
                channel.flush()
                os.fsync(channel.fileno())
        finally:
            os.close(channel_parent)
    if workload_mode == 'PUBLISHED_PLATFORM_PLAN':
        planning = execution_root / 'published-planning'
        subject_raw = (planning / 'subject.json').read_bytes()
        if 'sha256:' + hashlib.sha256(subject_raw).hexdigest() != binding.get('published_subject_digest'):
            raise ValueError('DEVELOPMENT_PUBLISHED_SUBJECT_CHANGED')
        subject = json.loads(subject_raw)
        product = _seal_development_tree(planning / 'published-product',
            Path('/var/lib/animemo/bootstrap-authority/v1'), 'materials', subject['product_inventory_digest'])
        # These are fixed verified Guest writes needed by online Stage-0, not
        # an authorization record. Only production Stage-0 may create that record.
        for name, target, digest in (
            ('installer-materials.tar', product.parent / 'installer-materials.tar', subject['archive_sha256']),
            ('gh_2.97.0_linux_amd64.deb', product / 'gh_2.97.0_linux_amd64.deb', subject['gh_package_sha256']),
        ):
            hashed = hashlib.sha256()
            with (planning / name).open('rb') as source, target.open('xb') as output:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    hashed.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if 'sha256:' + hashed.hexdigest() != digest:
                raise ValueError('DEVELOPMENT_PUBLISHED_INPUT_CHANGED')
    diagnostic.stage('MATERIAL_VERIFIED')
    receipt = Path('/var/lib/animemo/local-development/profile-report.json')
    parent = _root_directory(receipt.parent)
    try:
        try:
            os.stat(receipt.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('DEVELOPMENT_REPORT_EXISTS')
        program = ('import sys;from pathlib import Path;sys.path.insert(0,' + repr(str(execution_root))
            + ');from scripts.development_runtime_entry import main;raise SystemExit(main(Path('
            + repr(str(material_root)) + '),workload_mode=' + repr(workload_mode) + '))')
        if workload_mode == 'PUBLISHED_PLATFORM_PLAN':
            # A separate interpreter loads only original product modules. The
            # reviewed observer is an explicit file, never a same-name package.
            program = ('import runpy;runpy.run_path(' + repr(str(execution_root /
                'scripts/development_published_observer.py')) + ",run_name='__main__')")
        environment = dict(os.environ)
        environment['ANIMEMO_CANDIDATE_PROFILE_CONTEXT_B64URL'] = base64.urlsafe_b64encode(
            (json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()).decode().rstrip('=')
        fd = os.dup(diagnostic.fd)
        try:
            environment['ANIMEMO_CANDIDATE_DIAGNOSTIC_FD'] = str(fd)
            environment['ANIMEMO_CANDIDATE_DIAGNOSTIC_OPERATION'] = diagnostic.operation
            completed = subprocess.run(['/usr/bin/python3', '-I', '-B', '-c', program,
                '--profile', profile, '--binding', json.dumps(binding, sort_keys=True, separators=(',', ':'))],
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, pass_fds=(fd,),
                timeout=runtime_deadline.workload_timeout(4 * 60 * 60) if runtime_deadline else 4 * 60 * 60)
        finally:
            os.close(fd)
        diagnostic.exited('RUNTIME_RUNNER', completed.returncode)
        if completed.returncode:
            diagnostic.error('RUNNER_EXECUTION_FAILED')
            raise ValueError('DEVELOPMENT_PROFILE_EXECUTION_FAILED')
        fd = os.open(receipt.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != 0
                    or before.st_mode & 0o077 or not 0 < before.st_size <= 8 * 1024 * 1024):
                raise ValueError('DEVELOPMENT_REPORT_INVALID')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                body = stream.read(before.st_size + 1)
            if len(body) != before.st_size or _file_state(os.fstat(fd)) != _file_state(before):
                raise ValueError('DEVELOPMENT_REPORT_CHANGED')
            diagnostic.stage('DRAFT_RETURNED')
            diagnostic.frame(b'R', body)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
