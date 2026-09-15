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
    'scripts.development_capture_scope', 'scripts.guest_batch_scope', 'scripts.seams_development_controller'})
STABLE_FILES = (
    'scripts/development_controller.py', 'scripts/development_session_owner.py',
    'scripts/development_capture_scope.py', 'scripts/candidate_batch_session.py',
    'scripts/guest_sudo_session.py', 'scripts/guest_console_capture.py',
    'scripts/candidate_guest_session.py', 'scripts/development_guest_session.py',
    'scripts/local_candidate_development.py', 'scripts/development_source.py',
    'scripts/candidate_vm_harness.py', 'scripts/isolated_guest_validation.py',
    'scripts/candidate_child_process.py', 'release/formal_windows_pretrust.py',
    'scripts/guest_batch_scope.py', 'scripts/seams_development_controller.py',
    'updater/source.py', 'installer/bootstrap.py', 'updater/offline.py',
    'scripts/development_plan.py', 'scripts/formal_plan.py',
    'scripts/candidate_diagnostics.py', 'scripts/closed_runtime_inventory.py',
    'scripts/candidate_workload_root.py', 'scripts/development_workload_root.py',
    'release/candidate.py', 'release/materials.py', 'release/trust_bootstrap.py',
    'updater/authority.py', 'release/formal_candidate_history.py',
    'release/formal_vm_controller.py',
    'scripts/formal_guest_session.py', 'scripts/formal_workload_root.py',
    'scripts/formal_runtime_entry.py', 'scripts/formal_vm_harness.py',
    'installer/formal_bootstrap.py',
    'release/formal_input_readback.py', 'scripts/published_formal_entry.py',
    'scripts/formal_product_probe.py',
    'scripts/linux_attestation_probe.py', 'scripts/development_linux_probe.py',
    'scripts/development_platform_diagnostic.py', 'release/candidate_failure_policy.py',
    'installer/apt_diagnostics.py',
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


def publish_status(control_root, value, *, stopped, cancelled=lambda: False):
    temporary = control_root / 'status.next.json'
    _write_new(temporary, value)
    # Windows returns ACCESS_DENIED for an ordinary read handle without
    # FILE_SHARE_DELETE, as well as the more specific sharing/lock errors.
    # Retry only publication of these already-fsynced public bytes. Persistent
    # access failures and all other errors still close the owner in serve().
    for attempt in range(51):
        if cancelled():
            return
        try:
            os.replace(temporary, control_root / 'status.json')
            return
        except OSError as error:
            if getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 50:
                raise
            if stopped.wait(0.1):
                return


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


def verify_checkout(checkout, *, source_sha, source_tree, common_directory, stable):
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
        if name in stable:
            _require(hashlib.sha256(data).hexdigest() == stable[name])
        if name.endswith('.py') and name.split('/')[0] in PROJECT_PACKAGES:
            files[name] = digest
    for record in filter(None, _git(checkout, 'ls-files', '--stage', '-z').split(b'\0')):
        header, name = record.decode('utf-8').split('\t', 1)
        mode, digest, stage = header.split(' ')
        _require(stage == '0' and name not in index)
        index[name] = (mode, digest)
    _require(index == tree and set(stable) <= set(tree))
    _require(bool(files) and len(files) <= 4096)
    return files


class VerifiedProjectImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, checkout, files, stable):
        self.checkout, self.files, self.stable = checkout, dict(files), dict(stable)

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
        if name in self.stable:
            _require(hashlib.sha256(data).hexdigest() == self.stable[name])
        # Compile the same verified bytes; ordinary import would reopen a path
        # after checking it and permit a replacement between check and execution.
        exec(compile(data, str(self.checkout / name), 'exec'), module.__dict__)  # noqa: S102 - exact Git blob verified above


def validate_request(value, *, owner_record, previous_digest, checkout_parent, stable):
    _require(set(value) == {'schema', 'owner_id', 'round_index', 'previous_result_sha256',
        'checkout', 'source_sha', 'source_tree'} and value['schema'] == 'animemo.local-development-round-request/v1')
    _require(value['owner_id'] == owner_record['owner_id'] and type(value['round_index']) is int
        and value['round_index'] == owner_record['last_reserved_round'] + 1 <= owner_record.get('round_limit', owner_record['capture_limit'])
        and value['previous_result_sha256'] == previous_digest
        and all(type(value[key]) is str and re.fullmatch('[0-9a-f]{40}', value[key])
                for key in ('source_sha', 'source_tree')) and type(value['checkout']) is str)
    checkout = Path(value['checkout'])
    _require(checkout.is_absolute() and checkout.parent == checkout_parent
        and checkout.resolve(strict=True) == checkout and not checkout.is_symlink() and not checkout.is_junction()
        and stable_identities(checkout) == stable)
    return checkout


@contextmanager
def round_modules(checkout, source_inventory, stable):
    """Each round gets one coherent module generation; the owner and lock stay fixed."""
    def selected(name):
        return name not in STABLE_MODULES and name != 'scripts' and any(
            name == prefix or name.startswith(prefix + '.') for prefix in PROJECT_PACKAGES)
    saved = {name: module for name, module in tuple(sys.modules.items()) if selected(name)}
    old_path, old_cwd = list(sys.path), Path.cwd()
    importer = VerifiedProjectImporter(checkout, source_inventory, stable)
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


def validate_previous_session(previous_path):
    """Accept initial cancelled round 8 or a cleaned status-failure restart.

    This is public history validation, not a credential or budget grant. A new
    owner must still acquire the original ledger and match its spent prefix.
    """
    raw = _read(previous_path)
    previous = _json(raw)
    _require(previous.get('status') == 'FAIL' and previous.get('source_preserved') is True
        and previous.get('cleanup_errors') == [] and previous.get('private_material_root_released') is True
        and previous.get('private_execution_source_root_released') is True)
    session = previous['credential_session']
    if previous.get('failure_code') == 'CREDENTIAL_CAPTURE_CANCELLED':
        _require(session.get('development_capture_index') == 8
            and session.get('session_capture_attempts') == 1 and session.get('session_capture_completed') == 0)
        return raw, previous, 8
    _require(previous.get('failure_code') == 'DEVELOPMENT_SESSION_CLOSED'
        and session.get('session_capture_attempts') == session.get('session_capture_completed') == 0
        and 'development_capture_index' not in session)
    prior_owner = _json(_read(previous_path.parent / 'owner-final.json', 8192))
    _require(prior_owner.get('schema') == 'animemo.local-development-memory-owner/v1'
        and prior_owner.get('state') == 'CLOSED'
        and prior_owner.get('close_reason') == 'DEVELOPMENT_CONTROLLER_STATUS_FAILED'
        and prior_owner.get('secret_cleanup') == 'BEST_EFFORT_COMPLETED'
        and prior_owner.get('capture_attempts') == prior_owner.get('capture_completed') == 1
        and prior_owner.get('owner_id') == session.get('development_owner_id')
        and type(prior_owner.get('last_reserved_round')) is int
        and 9 <= prior_owner['last_reserved_round'] < 12)
    profiles = ('FRESH_BASE', 'DOCKER_BASE', 'RUNTIME_BASE_OFFLINE')
    roles = ('BOOTSTRAP_ROTATION', 'VERIFIED_SUDO', 'CANDIDATE_WORKLOAD')
    _require(set(session['profiles']) == set(profiles)
        and all(set(session['profiles'][profile]) == set(roles) for profile in profiles)
        and all(role.get('delivery_attempts') == role.get('delivery_completed') == 0
            and role.get('operation_result') == 'NOT_RUN'
            and role.get('target_verified') is False and role.get('lease_verified') is False
            for profile in session['profiles'].values() for role in profile.values()))
    _require(set(previous['profile_results']) == set(profiles)
        and previous['profile_results']['FRESH_BASE']['status'] == 'ERROR'
        and all(previous['profile_results'][profile]['status'] == 'NOT_RUN_SHARED_BLOCKER' for profile in profiles[1:])
        and set(previous['profile_operations']) == {'FRESH_BASE'})
    operation = previous['profile_operations']['FRESH_BASE']
    _require(operation.get('power_state') == 'STOPPED' and operation.get('clone_disposition') == 'QUARANTINED'
        and operation.get('cleanup_errors') == [] and all(operation.get(key) is True
            for key in ('session_keys_removed', 'known_hosts_removed', 'lease_released')))
    _require(all(not Path(previous[key]).exists() for key in ('private_material_root', 'private_execution_source_root')))
    material = prior_owner['material_identity']
    plan = previous['plan']
    _require(material['verified_candidate_digest'] == plan['verifiedCandidateDigest']
        and material['source_sha'] == plan['materialSourceSha'] and material['source_tree'] == plan['materialSourceTree']
        and material['qualification_run_id'] == plan['qualificationRunId'])
    return raw, previous, prior_owner['last_reserved_round']


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
    previous_raw, previous, expected_spent = validate_previous_session(previous_result)
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
            _require(owner.record['last_reserved_round'] == expected_spent)
            def status():
                try:
                    while not owner.closed and not done.wait(0.5):
                        value = {'schema': 'animemo.local-development-controller-status/v1',
                            'owner': owner.record, 'previous_result_sha256': previous_digest,
                            'controller_source_sha': source_sha, 'controller_source_tree': source_tree,
                            'stable_source_identities': stable}
                        publish_status(control_root, value, stopped=done, cancelled=lambda: owner.closed)
                except BaseException as error:  # noqa: BLE001 - status failure must close the memory owner
                    owner.close('DEVELOPMENT_CONTROLLER_STATUS_FAILED')
                    done.set()
                    try:
                        _write_new(control_root / 'status-failure.json', {
                            'schema': 'animemo.local-development-status-failure/v1',
                            'error_type': type(error).__name__ if type(error).__name__ in {
                                'PermissionError', 'FileExistsError', 'FileNotFoundError',
                                'OSError', 'DevelopmentOwnerError'} else 'UNCLASSIFIED',
                            'winerror': getattr(error, 'winerror', None) if type(getattr(error, 'winerror', None)) is int else None,
                            'errno': getattr(error, 'errno', None) if type(getattr(error, 'errno', None)) is int else None,
                        })
                    except BaseException:  # noqa: BLE001, S110 - diagnostics cannot delay owner cleanup
                        pass
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
                    source_tree=request['source_tree'], common_directory=common_directory, stable=stable)
                result_path = control_root / f'round-{index:04d}-result.json'
                _require(not result_path.exists())
                with round_modules(next_checkout, inventory, stable) as entry:
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
