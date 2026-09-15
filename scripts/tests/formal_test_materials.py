"""Exact, caller-selected media for the optional local composition test.

The trusted test launcher supplies SelectedMaterials from separately verified
material inventories and enters selected_materials() around unittest.run().
Environment variables are optional selectors, never root or digest authority.
This is a test fixture boundary, not a publication or ACL authority issuer.
"""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

from release.contract import PRERELEASE_TAG, validate_manifest
from release.formal_windows_pretrust import (
    FORMAL_WINDOWS_PRETRUST_FILES, FORMAL_WINDOWS_PRETRUST_PREFIX,
    _WindowsAclAuthority, _hold_windows_directory_component, _hold_windows_fixed_source_file,
)
from release.materials import (
    INITIAL_TRUST_KIT_FILES, INITIAL_TRUST_KIT_PREFIX,
    MaterialContractError, _open_single_link_regular_file,
    hold_bound_release_directory, reject_duplicate_json_keys,
)

SELECTORS = ('ANIMEMO_FORMAL_TEST_CANDIDATE', 'ANIMEMO_FORMAL_TEST_ASSETS',
             'ANIMEMO_FORMAL_TEST_SIDECAR')
KIT_MEMBERS = tuple(sorted(
    [(INITIAL_TRUST_KIT_PREFIX + '/' + name) for name in INITIAL_TRUST_KIT_FILES]
    + [(FORMAL_WINDOWS_PRETRUST_PREFIX + '/' + name) for name in FORMAL_WINDOWS_PRETRUST_FILES]))
MAX_METADATA = 16 * 1024 * 1024
MAX_KIT_FILE = 64 * 1024 * 1024
MAX_ARCHIVE = 4 * 1024 * 1024 * 1024


class TestMaterialsBlocked(ValueError):
    """Explicit local media selection is unavailable or unsafe; never a skip."""


@dataclass(frozen=True)
class MaterialMember:
    role: str
    relative: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SelectedMaterials:
    candidate_root: Path
    assets_root: Path
    sidecar_root: Path
    sidecar_name: str
    members: tuple[MaterialMember, ...]


@dataclass(frozen=True)
class MaterialSnapshot:
    root: Path
    manifest: dict
    payload: Path
    sidecar: Path
    installer_archive: Path


_selected = ContextVar('formal_test_materials', default=None)


@contextmanager
def selected_materials(selection: SelectedMaterials):
    """Register independently selected roots and expected bytes for this run."""
    if type(selection) is not SelectedMaterials:
        raise TestMaterialsBlocked('BLOCKED: trusted material selection required')
    token = _selected.set(selection)
    try:
        yield
    finally:
        _selected.reset(token)


def _parts(value: str, *, absolute=False):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise TestMaterialsBlocked('BLOCKED: invalid material path')
    normalized = value.replace('\\', '/')
    windows = PureWindowsPath(value)
    if normalized.startswith('//') or (windows.drive and os.name != 'nt'):
        raise TestMaterialsBlocked('BLOCKED: UNC or foreign drive')
    parts = normalized.split('/')
    if absolute and os.name == 'nt' and windows.drive:
        parts = parts[1:]
    elif absolute and normalized.startswith('/'):
        parts = parts[1:]
    if any(not p or p in ('.', '..') or ':' in p or p.rstrip(' .') != p
           or re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p)
           for p in parts):
        raise TestMaterialsBlocked('BLOCKED: noncanonical material path')
    if not absolute and (windows.drive or PurePosixPath(normalized).is_absolute()
                         or '\\' in value):
        raise TestMaterialsBlocked('BLOCKED: relative member required')
    return tuple(parts)


def _root(path: Path):
    _parts(str(path), absolute=True)
    if not path.is_absolute():
        raise TestMaterialsBlocked('BLOCKED: absolute selected root required')
    current = Path(path.anchor)
    for component in (current, *(path.parents)[::-1][1:], path):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode) or _linked(info):
            raise TestMaterialsBlocked('BLOCKED: root contains link or reparse point')
    if path.resolve(strict=True) != path:
        raise TestMaterialsBlocked('BLOCKED: root resolution changed')
    return path


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _member_path(root, relative, size, maximum):
    parts = _parts(relative)
    path = root.joinpath(*parts)
    current = root
    for part in parts[:-1]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or _linked(info):
            raise TestMaterialsBlocked('BLOCKED: member parent is unsafe')
    info = path.lstat()
    if (_linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or type(size) is not int or not 0 < size <= maximum or info.st_size != size):
        raise TestMaterialsBlocked('BLOCKED: member type, link count or size invalid')
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_relative_to(root):
        raise TestMaterialsBlocked('BLOCKED: member escapes selected root')
    return path, info


@contextmanager
def _source(path, maximum, *, expected=None):
    # Windows deny-write/delete handles are real even where composition later
    # simulates root ownership. POSIX uses a no-follow directory-bound leaf.
    with ExitStack() as stack:
        if os.name == 'nt':
            authority = _WindowsAclAuthority()
            authority.assert_fixed_non_reparse_chain(path)
            # Test media are content-bound, not trusted by their ACL. LIST
            # access plus no SHARE_DELETE locks ancestors without requesting
            # DELETE or accepting a permissive ACL fallback.
            for component in (*path.parent.parents[::-1], path.parent):
                stack.enter_context(_hold_windows_directory_component(
                    component, allow_child_writes=True, request_delete=False,
                    request_list=True))
            authority.assert_fixed_non_reparse_chain(path)
            stack.enter_context(_hold_windows_fixed_source_file(path))
            stream, _ = stack.enter_context(_open_single_link_regular_file(
                path, subject='Selected test material', maximum=maximum, allow_empty=False))
        else:
            parent = stack.enter_context(hold_bound_release_directory(
                path.parent, subject='Selected test material'))
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
            stream = stack.enter_context(os.fdopen(descriptor, 'rb'))
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not 0 < opened.st_size <= maximum:
            raise TestMaterialsBlocked('BLOCKED: opened material is unsafe')
        before = path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_nlink)
        if (_linked(before) or identity(before) != identity(opened)
                or (expected is not None and identity(expected) != identity(opened))):
            raise TestMaterialsBlocked('BLOCKED: material replaced before read')
        yield stream
        after = os.fstat(stream.fileno())
        if (identity(after) != identity(opened) or after.st_ctime_ns != opened.st_ctime_ns
                or identity(path.lstat()) != identity(opened)):
            raise TestMaterialsBlocked('BLOCKED: material changed during read')


def _copy_member(root, member, destination, maximum):
    path, expected = _member_path(root, member.relative, member.size, maximum)
    digest = hashlib.sha256()
    total = 0
    with _source(path, maximum, expected=expected) as source, destination.open('xb') as target:
        while True:
            chunk = source.read(min(1024 * 1024, member.size + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > member.size:
                raise TestMaterialsBlocked('BLOCKED: material grew while copying')
            digest.update(chunk)
            target.write(chunk)
    if total != member.size or 'sha256:' + digest.hexdigest() != member.sha256:
        raise TestMaterialsBlocked('BLOCKED: material differs from selected inventory')


def snapshot_selected_materials(destination: Path, *, environment=None):
    """Return None only for an unselected optional test; unsafe input is BLOCKED.

    destination must be a fresh child of the test's own TemporaryDirectory.
    All consumers receive only the verified snapshot, never source paths.
    """
    environment = os.environ if environment is None else environment
    selection = _selected.get()
    selectors = tuple(environment.get(name) for name in SELECTORS)
    if selection is None:
        if any(selectors):
            raise TestMaterialsBlocked('BLOCKED: environment cannot authorize material roots')
        return None
    expected = (selection.candidate_root, selection.assets_root,
                selection.sidecar_root / selection.sidecar_name)
    for supplied, trusted in zip(selectors, expected):
        if supplied is not None:
            _parts(supplied, absolute=True)
            if Path(supplied) != trusted:
                raise TestMaterialsBlocked('BLOCKED: selector differs from independently selected material')
    try:
        roots = {name: _root(path) for name, path in zip(
            ('candidate', 'assets', 'sidecar'), expected[:2] + (selection.sidecar_root,))}
        _parts(selection.sidecar_name)
        if Path(selection.sidecar_name).name != selection.sidecar_name:
            raise TestMaterialsBlocked('BLOCKED: fixed sidecar basename required')
        members = {}
        for item in selection.members:
            if (type(item) is not MaterialMember or item.role not in roots
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', item.sha256)):
                raise TestMaterialsBlocked('BLOCKED: invalid selected inventory')
            _parts(item.relative)
            key = (item.role, item.relative)
            if key in members:
                raise TestMaterialsBlocked('BLOCKED: duplicate selected member')
            members[key] = item
        manifest_member = members[('assets', 'release-manifest.json')]
        destination.mkdir(mode=0o700)  # exclusive; never accepts an existing tree
        manifest_path = destination / 'release-manifest.json'
        _copy_member(roots['assets'], manifest_member, manifest_path, MAX_METADATA)
        manifest = json.loads(manifest_path.read_bytes(), object_pairs_hook=reject_duplicate_json_keys)
        validate_manifest(manifest)
        version = manifest['release']['version']
        if not PRERELEASE_TAG.fullmatch(version):
            raise TestMaterialsBlocked('BLOCKED: candidate version required')
        portable = f'animemo-{version}-portable.tar'
        _parts(portable)
        expected_members = {('assets', 'release-manifest.json'), ('assets', portable),
                            ('assets', 'installer-materials.tar'), ('sidecar', selection.sidecar_name)}
        expected_members.update(('candidate', 'installer-root/' + name) for name in KIT_MEMBERS)
        if set(members) != expected_members:
            raise TestMaterialsBlocked('BLOCKED: exact declared test member set required')
        # Precheck the complete selected set before copying any remaining input.
        for key, item in members.items():
            maximum = MAX_ARCHIVE if key in {('assets', portable), ('assets', 'installer-materials.tar')} else MAX_KIT_FILE
            _member_path(roots[item.role], item.relative, item.size, maximum)
        for key in sorted(expected_members - {('assets', 'release-manifest.json')}):
            item = members[key]
            relative = item.relative.removeprefix('installer-root/') if item.role == 'candidate' else (
                'release-attestation.sigstore.json' if item.role == 'sidecar' else item.relative)
            target = destination.joinpath(*_parts(relative))
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            maximum = MAX_ARCHIVE if item.role == 'assets' else MAX_KIT_FILE
            _copy_member(roots[item.role], item, target, maximum)
        return MaterialSnapshot(destination, manifest, destination / portable,
                                destination / 'release-attestation.sigstore.json',
                                destination / 'installer-materials.tar')
    except (OSError, ValueError, KeyError, TypeError, MaterialContractError) as error:
        raise TestMaterialsBlocked('BLOCKED: selected test material validation failed') from error
