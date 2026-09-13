"""Keep one development memory owner while consuming closed public round requests."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from scripts.development_session_owner import (
    DevelopmentOwnerError,
    acquire_development_session_owner,
)

STABLE_MODULES = frozenset({'scripts.development_controller', 'scripts.development_session_owner',
    'scripts.development_capture_scope'})
STABLE_FILES = (
    'scripts/development_controller.py', 'scripts/development_session_owner.py',
    'scripts/development_capture_scope.py', 'scripts/candidate_batch_session.py',
    'scripts/guest_sudo_session.py', 'scripts/guest_console_capture.py',
    'scripts/candidate_guest_session.py', 'scripts/development_guest_session.py',
    'scripts/local_candidate_development.py', 'scripts/development_source.py',
    'scripts/candidate_vm_harness.py', 'scripts/isolated_guest_validation.py',
    'scripts/candidate_child_process.py', 'release/formal_windows_pretrust.py',
)
PROJECT_PACKAGES = ('scripts', 'installer', 'updater', 'durability', 'release')
GIT = 'C:/Program Files/Git/cmd/git.exe'


def _require(value):
    if not value:
        raise DevelopmentOwnerError('DEVELOPMENT_CONTROLLER_REQUEST_INVALID')


def _read(path, maximum=8 * 1024 * 1024, *, allow_empty=False):
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and not path.is_symlink() and before.st_nlink == 1
        and (0 <= before.st_size <= maximum if allow_empty else 0 < before.st_size <= maximum))
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    _require(len(data) == before.st_size and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))
    return data


def _json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value)
            value[key] = item
        return value
    value = json.loads(data, object_pairs_hook=pairs)
    _require(type(value) is dict)
    return value


def _write_new(path, value):
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    with path.open('xb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def stable_identities(checkout):
    return {name: hashlib.sha256(_read(checkout / name)).hexdigest() for name in STABLE_FILES}


def _git(checkout, *arguments, codes=(0,)):
    environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'TEMP', 'TMP') if key in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0', PATH='C:/Program Files/Git/cmd;C:/Windows/System32;C:/Windows')
    result = subprocess.run([GIT, '-C', str(checkout), *arguments], stdin=subprocess.DEVNULL,
        capture_output=True, timeout=30, env=environment, check=False,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    _require(result.returncode in codes)
    return result.stdout


def verify_checkout(checkout, *, source_sha, source_tree, common_directory):
    """Verify source before any of its modules are imported into the owner process."""
    _require(not _git(checkout, 'config', '--name-only', '--get-regexp',
        r'^(core\.(fsmonitor|hookspath|attributesfile)|filter\..*\.(clean|process)|include.*\.path)$', codes=(0, 1)))
    common = _git(checkout, 'rev-parse', '--git-common-dir').decode('utf-8').strip()
    _require((checkout / common).resolve(strict=True) == common_directory
        and _git(checkout, 'rev-parse', 'HEAD').decode().strip() == source_sha
        and _git(checkout, 'rev-parse', 'HEAD^{tree}').decode().strip() == source_tree
        and not _git(checkout, 'ls-files', '--others', '--exclude-standard', '-z'))
    tree, index, files = {}, {}, {}
    records = list(filter(None, _git(checkout, 'ls-tree', '-rz', '--full-tree', source_sha).split(b'\0')))
    _require(0 < len(records) <= 20000)
    for record in records:
        header, name = record.decode('utf-8').split('\t', 1)
        mode, kind, digest = header.split(' ')
        _require(mode in {'100644', '100755'} and kind == 'blob'
            and re.fullmatch('[0-9a-f]{40}', digest) is not None)
        tree[name] = (mode, digest)
        # Read/hash raw bytes instead of git status/diff, which could invoke
        # a worktree-configured clean filter or fsmonitor during validation.
        data = _read(checkout / name, 64 * 1024 * 1024, allow_empty=True)
        _require(hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest() == digest)
        if name.endswith('.py') and name.split('/')[0] in PROJECT_PACKAGES:
            files[name] = digest
    for record in filter(None, _git(checkout, 'ls-files', '--stage', '-z').split(b'\0')):
        header, name = record.decode('utf-8').split('\t', 1)
        mode, digest, stage = header.split(' ')
        _require(stage == '0' and name not in index)
        index[name] = (mode, digest)
    _require(index == tree)
    _require(bool(files) and len(files) <= 4096)
    return files


class VerifiedProjectImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, checkout, files):
        self.checkout, self.files = checkout, dict(files)

    def _name(self, fullname):
        relative = fullname.replace('.', '/')
        package = relative + '/__init__.py'
        return package if package in self.files else relative + '.py'

    def find_spec(self, fullname, path=None, target=None):
        if fullname in STABLE_MODULES or not any(fullname == name or fullname.startswith(name + '.') for name in PROJECT_PACKAGES):
            return None
        name = self._name(fullname)
        if name in self.files:
            return importlib.util.spec_from_file_location(fullname, self.checkout / name, loader=self,
                submodule_search_locations=[str((self.checkout / name).parent)] if name.endswith('/__init__.py') else None)
        prefix = fullname.replace('.', '/') + '/'
        if any(name.startswith(prefix) for name in self.files):
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(self.checkout / prefix)]
            return spec
        raise ModuleNotFoundError('DEVELOPMENT_IMPORT_NOT_IN_VERIFIED_SOURCE')

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = self._name(module.__name__)
        data = _read(self.checkout / name, allow_empty=True)
        _require(hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest() == self.files[name])
        # Compile the same verified bytes; ordinary import would reopen a path
        # after checking it and permit a replacement between check and execution.
        exec(compile(data, str(self.checkout / name), 'exec'), module.__dict__)  # noqa: S102 - exact Git blob verified above


def validate_request(value, *, owner_record, previous_digest, checkout_parent, stable):
    _require(set(value) == {'schema', 'owner_id', 'round_index', 'previous_result_sha256',
        'checkout', 'source_sha', 'source_tree'} and value['schema'] == 'animemo.local-development-round-request/v1')
    _require(value['owner_id'] == owner_record['owner_id'] and type(value['round_index']) is int
        and value['round_index'] == owner_record['last_reserved_round'] + 1 <= owner_record['capture_limit']
        and value['previous_result_sha256'] == previous_digest
        and all(type(value[key]) is str and re.fullmatch('[0-9a-f]{40}', value[key])
                for key in ('source_sha', 'source_tree')) and type(value['checkout']) is str)
    checkout = Path(value['checkout'])
    _require(checkout.is_absolute() and checkout.parent == checkout_parent
        and checkout.resolve(strict=True) == checkout and not checkout.is_symlink() and not checkout.is_junction()
        and stable_identities(checkout) == stable)
    return checkout


@contextmanager
def round_modules(checkout, source_inventory):
    """Each round gets one coherent module generation; the owner and lock stay fixed."""
    def selected(name):
        return name not in STABLE_MODULES and name != 'scripts' and any(
            name == prefix or name.startswith(prefix + '.') for prefix in PROJECT_PACKAGES)
    saved = {name: module for name, module in tuple(sys.modules.items()) if selected(name)}
    old_path, old_cwd = list(sys.path), Path.cwd()
    importer = VerifiedProjectImporter(checkout, source_inventory)
    scripts = importlib.import_module('scripts')
    old_namespace = scripts.__path__
    for name in saved:
        del sys.modules[name]
    # Namespace package attributes can otherwise keep a previous generation
    # alive even after its sys.modules entry was removed.
    old_attributes = {name: value for name, value in vars(scripts).copy().items()
        if getattr(value, '__name__', None) in saved}
    for name in old_attributes:
        delattr(scripts, name)
    try:
        # Project imports use the verified finder. A checkout on sys.path would
        # also let an untracked top-level file shadow a standard/dependency module.
        sys.path[:] = [path for path in old_path if path and Path(path).resolve() not in {old_cwd, checkout}]
        sys.meta_path.insert(0, importer)
        scripts.__path__ = [str(checkout / 'scripts')]
        os.chdir(checkout)
        importlib.invalidate_caches()
        yield importlib.import_module('scripts.local_candidate_development')
    finally:
        sys.meta_path.remove(importer)
        for name in tuple(sys.modules):
            if selected(name):
                del sys.modules[name]
        for name, value in vars(scripts).copy().items():
            if selected(getattr(value, '__name__', '')):
                delattr(scripts, name)
        sys.modules.update(saved)
        for name, value in old_attributes.items():
            setattr(scripts, name, value)
        scripts.__path__ = old_namespace
        sys.path[:] = old_path
        os.chdir(old_cwd)
        importlib.invalidate_caches()


def serve(control_root, previous_result):

    from release.formal_windows_pretrust import (
        create_windows_private_named_directory,
        hold_windows_private_path_chain,
    )
    from scripts import development_capture_scope as scope
    from scripts.guest_console_capture import WindowsConsoleCapture
    from scripts.isolated_guest_validation import _check_checkout

    checkout = Path(__file__).resolve().parents[1]
    source_sha = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD']).decode().strip()
    source_tree = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD^{tree}']).decode().strip()
    _check_checkout(source_sha, source_tree)
    stable = stable_identities(checkout)
    common = _git(checkout, 'rev-parse', '--git-common-dir').decode('utf-8').strip()
    common_directory = (checkout / common).resolve(strict=True)
    previous_raw = _read(previous_result)
    previous = _json(previous_raw)
    _require(previous.get('status') == 'FAIL' and previous.get('source_preserved') is True
        and previous.get('cleanup_errors') == [] and previous.get('private_material_root_released') is True
        and previous.get('private_execution_source_root_released') is True)
    _require(previous['credential_session']['development_capture_index'] == 8
        and previous['credential_session']['session_capture_completed'] == 0
        and previous['failure_code'] == 'CREDENTIAL_CAPTURE_CANCELLED')
    WindowsConsoleCapture().preflight()
    material = _json(_read(scope.LEDGER / 'scope.json', 4096))['material_identity']
    _require(material['verified_candidate_digest'] == previous['plan']['verifiedCandidateDigest']
        and material['source_sha'] == previous['plan']['materialSourceSha']
        and material['source_tree'] == previous['plan']['materialSourceTree']
        and material['qualification_run_id'] == previous['plan']['qualificationRunId'])
    control_root = control_root.resolve(strict=False)
    _require(control_root.parent.is_dir() and not control_root.exists())
    create_windows_private_named_directory(control_root.parent, name=control_root.name)
    owner = None
    done = threading.Event()
    status_thread = None
    previous_digest = 'sha256:' + hashlib.sha256(previous_raw).hexdigest()
    try:
        with hold_windows_private_path_chain(control_root, allow_leaf_child_writes=True):
            owner = acquire_development_session_owner(material_identity=material)
            def status():
                try:
                    while not done.wait(0.5):
                        value = {'schema': 'animemo.local-development-controller-status/v1',
                            'owner': owner.record, 'previous_result_sha256': previous_digest,
                            'controller_source_sha': source_sha, 'controller_source_tree': source_tree,
                            'stable_source_identities': stable}
                        temporary = control_root / 'status.next.json'
                        _write_new(temporary, value)
                        os.replace(temporary, control_root / 'status.json')
                except BaseException:  # noqa: BLE001 - status failure must close the memory owner
                    owner.close('DEVELOPMENT_CONTROLLER_STATUS_FAILED')
                    done.set()
            status_thread = threading.Thread(target=status, daemon=True)
            status_thread.start()
            while not owner.closed:
                index = owner.record['last_reserved_round'] + 1
                request_path = control_root / f'request-{index:04d}.json'
                if not request_path.exists():
                    done.wait(0.5)
                    continue
                request = _json(_read(request_path, 8192))
                next_checkout = validate_request(request, owner_record=owner.record, previous_digest=previous_digest,
                    checkout_parent=checkout.parent, stable=stable)
                inventory = verify_checkout(next_checkout, source_sha=request['source_sha'],
                    source_tree=request['source_tree'], common_directory=common_directory)
                result_path = control_root / f'round-{index:04d}-result.json'
                _require(not result_path.exists())
                with round_modules(next_checkout, inventory) as entry:
                    args = SimpleNamespace(execute=True, authorization_id=scope.AUTHORIZATION, result=result_path,
                        verified_candidate_digest=material['verified_candidate_digest'], qualification_run_id=material['qualification_run_id'],
                        material_source_sha=material['source_sha'], material_source_tree=material['source_tree'],
                        execution_source_sha=request['source_sha'], execution_source_tree=request['source_tree'])
                    report = entry.run(args, session_owner=owner)
                    # Persist the immutable round before deciding if it is safe
                    # to retain the session or accept a next-source request.
                    previous_digest = _write_new(result_path, report)
                    owner.finish_round(report)
                _write_new(control_root / f'round-{index:04d}-owner.json', owner.record)
    finally:
        done.set()
        if status_thread is not None:
            status_thread.join(timeout=2)
        if owner is not None:
            owner.dispose()
            _write_new(control_root / 'owner-final.json', owner.record)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-root', required=True, type=Path)
    parser.add_argument('--previous-result', required=True, type=Path)
    arguments = parser.parse_args(argv)
    serve(arguments.control_root, arguments.previous_result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
