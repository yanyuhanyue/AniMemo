"""Fixed Candidate root operation, embedded by the trusted Host controller.

The Host also embeds closed_runtime_inventory in this namespace. No module
from the unprivileged staging tree is imported before its bytes are sealed.
There is deliberately no CLI or arbitrary command dispatcher.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.closed_runtime_inventory import (
        MAXIMUM_PATHS, _directory_state, _file_state, _open_directory_chain,
        closed_runtime_inventory_digest, closed_runtime_total_bytes,
    )


def _root_directory(path):
    """Create/open each component beneath / without following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open('/', flags)
    try:
        for component in Path(path).parts[1:]:
            try:
                os.mkdir(component, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(component, flags, dir_fd=parent)
            metadata = os.fstat(child)
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                os.close(child)
                raise ValueError('CANDIDATE_ROOT_PARENT_UNTRUSTED')
            os.close(parent)
            parent = child
        return parent
    except BaseException:
        os.close(parent)
        raise


def _copy_stage(source, destination):
    """Copy through pinned fds, with no links, special files, or growth."""
    total = count = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    def visit(src, dst):
        nonlocal total, count
        before = os.fstat(src)
        names = sorted(os.listdir(src))
        for name in names:
            if not name or name in {'.', '..'} or '/' in name or '\\' in name:
                raise ValueError('CANDIDATE_STAGE_PATH_INVALID')
            count += 1
            if count > MAXIMUM_PATHS:
                raise ValueError('CANDIDATE_STAGE_SIZE_INVALID')
            metadata = os.stat(name, dir_fd=src, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, directory_flags, dir_fd=src)
                try:
                    if _directory_state(os.fstat(child)) != _directory_state(metadata):
                        raise ValueError('CANDIDATE_STAGE_CHANGED')
                    os.mkdir(name, 0o700, dir_fd=dst)
                    output = os.open(name, directory_flags, dir_fd=dst)
                    try:
                        visit(child, output)
                        os.fchmod(output, 0o500)
                    finally:
                        os.close(output)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError('CANDIDATE_STAGE_FILE_INVALID')
            total = closed_runtime_total_bytes(total, metadata.st_size)
            # NONBLOCK prevents a FIFO substituted between stat and open from
            # blocking the root process; fstat then rejects every special file.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
            try:
                if _file_state(os.fstat(fd)) != _file_state(metadata):
                    raise ValueError('CANDIDATE_STAGE_CHANGED')
                output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=dst)
                try:
                    size = 0
                    while block := os.read(fd, min(1024 * 1024, metadata.st_size + 1 - size)):
                        size += len(block)
                        if size > metadata.st_size:
                            raise ValueError('CANDIDATE_STAGE_CHANGED')
                        view = memoryview(block)
                        while view:
                            written = os.write(output, view)
                            if written <= 0:
                                raise ValueError('CANDIDATE_STAGE_COPY_FAILED')
                            view = view[written:]
                    if size != metadata.st_size or _file_state(os.fstat(fd)) != _file_state(metadata):
                        raise ValueError('CANDIDATE_STAGE_CHANGED')
                    os.fsync(output)
                    os.fchmod(output, 0o500)
                finally:
                    os.close(output)
            finally:
                os.close(fd)
        if sorted(os.listdir(src)) != names or _directory_state(os.fstat(src)) != _directory_state(before):
            raise ValueError('CANDIDATE_STAGE_CHANGED')
    visit(source, destination)
    if count == 0:
        raise ValueError('CANDIDATE_STAGE_EMPTY')


def run_fixed_candidate(*, session_id, profile, input_digest, verified_digest,
                        inventory_digest, context, diagnostic):
    if (os.geteuid() != 0 or re.fullmatch(r'[0-9a-f]{32}', session_id) is None
            or profile not in {'FRESH_BASE', 'DOCKER_BASE', 'RUNTIME_BASE_OFFLINE'}
            or any(re.fullmatch(r'sha256:[0-9a-f]{64}', value) is None
                   for value in (input_digest, verified_digest, inventory_digest))):
        raise ValueError('CANDIDATE_ROOT_SCOPE_INVALID')
    os.umask(0o077)
    os.chdir('/')
    os.environ.clear()
    os.environ.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8')
    stage = Path('/tmp') / ('animemo-candidate-' + session_id + '-' + profile)
    fixed_root = Path('/var/lib/animemo/prepublication-candidates/v2')
    leaf = input_digest.removeprefix('sha256:')
    destination = fixed_root / leaf
    diagnostic.stage('MATERIAL_FINALIZING')
    parent = _root_directory(fixed_root)
    try:
        os.mkdir(leaf, 0o700, dir_fd=parent)  # Never reuse a previous attempt.
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
    if closed_runtime_inventory_digest(destination) != inventory_digest:
        diagnostic.error('MATERIAL_INVENTORY_MISMATCH')
        raise ValueError('CANDIDATE_STAGE_INVENTORY_MISMATCH')
    diagnostic.stage('MATERIAL_VERIFIED')
    receipt = Path('/var/lib/animemo/candidate-acceptance/profile-receipt-draft.json')
    receipt_parent = _root_directory(receipt.parent)
    try:
        try:
            os.stat(receipt.name, dir_fd=receipt_parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('CANDIDATE_RECEIPT_EXISTS')
        root = str(destination / 'installer-root')
        # -I ignores PYTHONPATH. Add exactly the verified root in a fixed
        # interpreter program; all subsequent imports come from sealed bytes.
        program = ('import sys;from pathlib import Path;sys.path.insert(0,' + repr(root)
            + ');from scripts.candidate_runtime_entry import main;raise SystemExit(main(Path('
            + repr(str(receipt.parent / 'python-runtime')) + ')))')
        environment = dict(os.environ)
        environment['ANIMEMO_CANDIDATE_PROFILE_CONTEXT_B64URL'] = base64.urlsafe_b64encode(
            (json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()).decode().rstrip('=')
        diagnostic_fd = os.dup(diagnostic.fd)
        try:
            environment['ANIMEMO_CANDIDATE_DIAGNOSTIC_FD'] = str(diagnostic_fd)
            environment['ANIMEMO_CANDIDATE_DIAGNOSTIC_OPERATION'] = diagnostic.operation
            completed = subprocess.run(['/usr/bin/python3', '-I', '-B', '-c', program,
                '--verified-candidate-digest', verified_digest, '--profile', profile,
                '--public-origin', 'https://candidate.invalid', '--execute'], env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(diagnostic_fd,), timeout=4 * 60 * 60)
        finally:
            os.close(diagnostic_fd)
        diagnostic.exited('RUNTIME_RUNNER', completed.returncode)
        if completed.returncode != 0:
            diagnostic.error('RUNNER_EXECUTION_FAILED')
            raise ValueError('CANDIDATE_PROFILE_EXECUTION_FAILED')
        if not receipt.is_file():
            diagnostic.error('DRAFT_MISSING')
        fd = os.open(receipt.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=receipt_parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != 0
                    or before.st_mode & 0o077 or not 0 < before.st_size <= 8 * 1024 * 1024):
                raise ValueError('CANDIDATE_RECEIPT_INVALID')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                body = stream.read(before.st_size + 1)
            if len(body) != before.st_size or _file_state(os.fstat(fd)) != _file_state(before):
                raise ValueError('CANDIDATE_RECEIPT_CHANGED')
            # The Host parses duplicate keys and the complete canonical schema.
            diagnostic.stage('DRAFT_RETURNED')
            diagnostic.frame(b'R', body)
        finally:
            os.close(fd)
    finally:
        os.close(receipt_parent)
