"""Hold a clean local execution source tree independently of qualified OCI bytes."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess

from release.formal_windows_pretrust import (
    create_windows_private_directory,
    create_windows_private_named_directory,
    hold_windows_private_path_chain,
    hold_windows_private_tree_snapshot,
)
from release.materials import _FIXED_DEPLOYMENT_FILES
from scripts import candidate_vm_harness as h
from scripts.development_plan import is_development_plan
from scripts.isolated_guest_validation import _check_checkout

ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_RUNTIME_FILES = (
    'scripts/development_profile_runner.py',
    'scripts/development_runtime_entry.py',
    'scripts/development_workload_root.py',
)


def require_material_compatibility(material_source_sha, execution_source_sha):
    if any(type(value) is not str or not h._SHA.fullmatch(value)
           for value in (material_source_sha, execution_source_sha)):
        raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_BINDING_INVALID')
    subprocess.run(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor',
        material_source_sha, execution_source_sha], check=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    paths = subprocess.check_output(['git', '-C', str(ROOT), 'diff', '--name-only', '-z',
        material_source_sha, execution_source_sha], timeout=30).decode('utf-8').split('\0')
    for name in filter(None, paths):
        path = PurePosixPath(name)
        python_source = path.suffix == '.py' and path.parts[0] in {
            'scripts', 'installer', 'durability', 'release', 'updater'}
        if not python_source and path.parts[0] != 'docs':
            raise h.CandidateHarnessError('DEVELOPMENT_LOCAL_MATERIAL_REBUILD_REQUIRED')


def execution_file_identities(source_sha, source_tree):
    _check_checkout(source_sha, source_tree)
    raw = subprocess.check_output(['git', '-C', str(ROOT), 'ls-tree', '-rz', '--name-only',
        'HEAD', '--', 'durability', 'release', 'updater', 'installer',
        *_FIXED_DEPLOYMENT_FILES, *DEVELOPMENT_RUNTIME_FILES], timeout=30)
    names = [name for name in raw.decode('utf-8').split('\0')
             if name and not {'tests', '__pycache__'}.intersection(PurePosixPath(name).parts)]
    required = {*_FIXED_DEPLOYMENT_FILES, *DEVELOPMENT_RUNTIME_FILES}
    if not required <= set(names) or not names or len(names) > 1024 or len(names) != len(set(names)):
        raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_INVENTORY_INVALID')
    identities = {}
    total = 0
    for name in names:
        path = ROOT / name
        if (PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                or path.is_symlink() or path.is_junction() or not path.is_file()
                or path.stat().st_nlink != 1):
            raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_FILE_INVALID')
        size = path.stat().st_size
        total += size
        if size > 64 * 1024 * 1024 or total > 256 * 1024 * 1024:
            raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_SIZE_INVALID')
        with path.open('rb') as source:
            identities[name] = 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()
    _check_checkout(source_sha, source_tree)
    return identities


class HeldDevelopmentSource:
    __slots__ = ('_root', '_source_root', '_holds', '_closed', '_execution', 'source_sha', 'source_tree', 'inventory_digest')

    def __init__(self, *_args, **_kwargs):
        raise TypeError('Development source authority is acquired only through its factory')

    def __reduce__(self):
        raise TypeError('Development source authority cannot be serialized')

    @property
    def root(self):
        self.require_open()
        return self._source_root

    def require_open(self):
        if self._closed:
            raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_AUTHORITY_CLOSED')

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._holds.close()
        root = self._root
        # Only this factory's exact disposable source directory is eligible.
        if (root.parent != Path('E:/') or not root.name.startswith('animemo-development-source-')
                or root.is_symlink() or root.is_junction() or root.resolve(strict=True) != root):
            raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_CLEANUP_SCOPE_INVALID')
        shutil.rmtree(root)


@contextmanager
def acquire_development_source(provider, *, source_sha, source_tree):
    if os.name != 'nt' or type(provider) is not h.ClosedVmwareProvider:
        raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_PROVIDER_INVALID')
    provider._require_active_execution_authority()
    if provider._execution is None or getattr(provider, '_development_source_authority', None) is not None:
        raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_PROVIDER_INVALID')
    identities = execution_file_identities(source_sha, source_tree)
    root = create_windows_private_directory(Path('E:/'), prefix='animemo-development-source')
    value = object.__new__(HeldDevelopmentSource)
    value._root, value._closed, value._execution = root, False, provider._execution
    value.source_sha, value.source_tree = source_sha, source_tree
    value._holds = ExitStack()
    try:
        value._holds.enter_context(hold_windows_private_path_chain(root, allow_leaf_child_writes=True))
        value._source_root = create_windows_private_named_directory(root,
            name=hashlib.sha256(h.canonical_json_bytes(identities)).hexdigest())
        value._holds.enter_context(hold_windows_private_tree_snapshot(ROOT,
            expected_file_identities=identities, private_root=value._source_root,
            maximum_files=1024, maximum_file_bytes=64 * 1024 * 1024,
            maximum_total_bytes=256 * 1024 * 1024))
        value.inventory_digest = h._closed_runtime_inventory_digest(value._source_root)
        _check_checkout(source_sha, source_tree)
        provider._development_source_authority = value
        yield value
    finally:
        provider._development_source_authority = None
        value.close()


def require_development_source(provider, plan):
    authority = getattr(provider, '_development_source_authority', None)
    if (not is_development_plan(plan) or type(authority) is not HeldDevelopmentSource
            or authority._execution is not provider._execution
            or authority.source_sha != plan.execution_source_sha
            or authority.source_tree != plan.execution_source_tree
            or authority.inventory_digest != plan.execution_inventory_digest):
        raise h.CandidateHarnessError('DEVELOPMENT_SOURCE_AUTHORITY_INVALID')
    authority.require_open()
    _check_checkout(plan.execution_source_sha, plan.execution_source_tree)
    return authority
