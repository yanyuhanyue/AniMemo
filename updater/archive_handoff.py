"""Transaction-scoped original asset ownership, separate from extracted members.

This object carries files and observations, never installation authority.
"""
from __future__ import annotations

import json
import os
from contextlib import ExitStack, contextmanager
from dataclasses import asdict

from bootstrap_kit.safe_files import (
    create_private_directory,
    directory_identity,
    exclusive_file,
    file_digest,
    held_file,
    remove_owned_directory,
)

_RETAINED = {}
_ARCHIVE = 'installer-materials.tar'
_MAXIMUM = 512 * 1024 * 1024


class ArchiveHandoffError(ValueError):
    def __init__(self, suffix):
        self.code = 'BOOTSTRAP_ARCHIVE_' + suffix
        super().__init__(self.code)


def _require(condition, suffix='IDENTITY_MISMATCH'):
    if not condition:
        raise ArchiveHandoffError(suffix)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise ArchiveHandoffError('IDENTITY_MISMATCH') from None


def _file_state(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _uncertain(error):
    return bool(error is not None and (getattr(error, 'cleanup_failed', False)
        or getattr(error, 'cleanup_uncertain', False)
        or getattr(error, '_owned_process', None) is not None
        or any(type(code) is str and ('CLEANUP' in code or 'HANDLE_CLOSE' in code or 'OUTPUT_DRAIN' in code)
               for code in getattr(error, 'secondary_errors', ()))))


class _ArchiveTransaction:
    def __init__(self, source, root):
        self._source, self.root = source, root
        self._holds = ExitStack()
        self._active, self._used, self._borrowing = True, False, False
        self._stream = self._path = self._materials = None
        self._snapshot = None
        self._observation = None

    def _hold(self, acquired, request, inventory, observation):
        _require(self._active and self._stream is None, 'UNAVAILABLE')
        _require(acquired.receipt.request_identity == request.identity
            and acquired.receipt.objects == acquired.objects)
        assets = [item for item in inventory.assets if item.name == _ARCHIVE]
        receipts = [item for item in acquired.objects if item.logical_name == _ARCHIVE]
        selections = [item for item in request.github_assets if item.logical_name == _ARCHIVE]
        plans = [item for item in request.object_plans if item.logical_name == _ARCHIVE]
        _require(len(assets) == len(receipts) == len(selections) == len(plans) == 1)
        asset, receipt = assets[0], receipts[0]
        _require(asset.asset_id is not None and inventory.release_id == request.github_release_id
            and asset.size == receipt.size == plans[0].expected_size
            and asset.digest == 'sha256:' + receipt.sha256 == selections[0].sha256
            and asset.asset_id == selections[0].asset_id and receipt.relative_path == _ARCHIVE)
        self._path = acquired.material(_ARCHIVE)
        self._stream = self._holds.enter_context(held_file(self._path, _MAXIMUM))
        self._path_state = _file_state(self._path.lstat())
        self._handle_state = _file_state(os.fstat(self._stream.fileno()))
        self._directory_identity = directory_identity(self._path.parent)
        self._identity = (asset.digest, asset.size)
        _require(file_digest(self._stream) == self._identity)
        _require(type(observation) is dict and observation['release']['id'] == inventory.release_id
            and observation['release']['tag_name'] == inventory.version)
        self._observation = json.loads(_canonical(observation))
        self._observation_bytes = _canonical(self._observation)
        self._asset = {'id': asset.asset_id, 'name': _ARCHIVE, 'size': asset.size,
                       'digest': asset.digest, 'state': asset.state}
        _require(self._asset in self._observation['release']['assets'])
        self.transport_identity = acquired.receipt.identity

    def _check_file(self):
        _require(self._active and self._stream is not None and not self._stream.closed, 'UNAVAILABLE')
        try:
            _require(_file_state(self._path.lstat()) == self._path_state
                and _file_state(os.fstat(self._stream.fileno())) == self._handle_state
                and directory_identity(self._path.parent) == self._directory_identity)
            _require(file_digest(self._stream) == self._identity)
        except (OSError, ValueError) as error:
            if isinstance(error, ArchiveHandoffError):
                raise
            raise ArchiveHandoffError('UNAVAILABLE') from None

    @staticmethod
    def _material_identity(materials):
        return _canonical({'manifest': materials.manifest, 'contract': materials.deployment_contract,
            'identity': materials.identity_digest,
            'execution': asdict(materials.attestation_execution_receipt)
                if materials.attestation_execution_receipt is not None else None})

    def _bind(self, materials):
        from .authority import VerifiedReleaseMaterials
        self._check_file()
        _require(type(materials) is VerifiedReleaseMaterials and self._materials is None)
        _require(materials.installer_archive_sha256 == self._identity[0]
            and materials.deployment_contract['archive']['size'] == self._identity[1])
        self._materials, self._snapshot = materials, self._material_identity(materials)

    @property
    def observation(self):
        # Public diagnostics cannot mutate the binding used by a live borrow.
        return json.loads(self._observation_bytes)

    @property
    def materials(self):
        _require(self._active and self._materials is not None, 'UNAVAILABLE')
        return self._materials

    @contextmanager
    def archive_for(self, materials, *, source):
        _require(not self._used, 'ALREADY_BORROWED')
        self._used = True
        _require(self._active and source is self._source and materials is self._materials,
                 'TRANSACTION_MISMATCH')
        self._check_file()
        _require(self._material_identity(materials) == self._snapshot
            and _canonical(self._observation) == self._observation_bytes)
        self._borrowing = True
        primary = None
        try:
            yield self._path
        except BaseException as error:
            primary = error
            raise
        finally:
            self._borrowing = False
            try:
                self._check_file()
                _require(self._material_identity(materials) == self._snapshot
                    and _canonical(self._observation) == self._observation_bytes)
            except (OSError, ValueError):
                if primary is None:
                    raise
                primary.secondary_errors = (*getattr(primary, 'secondary_errors', ()),
                    'BOOTSTRAP_ARCHIVE_POSTREAD_FAILED')[:4]

    def check_platform_source(self, inputs):
        from installer.anonymous_release_transport import UntrustedReleaseMaterials
        _require(self._active and self._borrowing, 'UNAVAILABLE')
        _require(_canonical(self._observation) == self._observation_bytes)
        _require(type(inputs) is UntrustedReleaseMaterials
            and inputs.version == self.observation['release']['tag_name']
            and inputs.tag_commit == self._materials.manifest['release']['commit'], 'SOURCE_MISMATCH')
        metadata = inputs.release_metadata
        first = self.observation['release']
        _require(all(metadata.get(key) == first[key]
            for key in ('id', 'tag_name', 'draft', 'prerelease', 'immutable')), 'SOURCE_MISMATCH')
        _require([item for item in metadata['assets'] if item['name'] == _ARCHIVE] == [self._asset],
                 'SOURCE_MISMATCH')

    def preserve(self, parent):
        """One evidence copy, not a new trusted product or a lifetime extension."""
        _require(not hasattr(self, '_evidence_root'), 'EVIDENCE_ALREADY_SAVED')
        self._check_file()
        self._evidence_root = create_private_directory(parent, prefix='archive-evidence-')
        target = self._evidence_root / _ARCHIVE
        self._stream.seek(0)
        with exclusive_file(target) as output:
            remaining = self._identity[1]
            while remaining:
                chunk = self._stream.read(min(1048576, remaining))
                _require(bool(chunk))
                output.write(chunk)
                remaining -= len(chunk)
        with held_file(target, _MAXIMUM) as saved:
            _require(file_digest(saved) == self._identity)
        self._check_file()
        with exclusive_file(self._evidence_root / 'identity.json') as output:
            output.write(_canonical({'authority': 'UNTRUSTED_PUBLIC_ASSET_EVIDENCE',
                'source': self.observation, 'asset': self._asset,
                'transport_receipt': self.transport_identity}))
        return self._evidence_root


@contextmanager
def release_workspace(source, parent):
    root = create_private_directory(parent, prefix='release-')
    identity = directory_identity(root)
    owner = _ArchiveTransaction(source, root)
    primary = None
    try:
        yield owner
    except BaseException as error:
        primary = error
        raise
    finally:
        # Revoke before cleanup; uncertain process owners keep the original holds.
        owner._active = False
        if _uncertain(primary):
            _RETAINED[identity] = (owner, primary)
        else:
            try:
                owner._holds.close()
            except (OSError, ValueError):
                _RETAINED[identity] = (owner, primary)
                if primary is None:
                    raise ArchiveHandoffError('HANDLE_CLOSE_FAILED') from None
                primary.secondary_errors = (*getattr(primary, 'secondary_errors', ()),
                    'BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED')[:4]
            else:
                try:
                    remove_owned_directory(root, identity)
                except (OSError, ValueError):
                    _RETAINED[identity] = (owner, primary)
                    if primary is None:
                        raise ArchiveHandoffError('CLEANUP_FAILED') from None
                    primary.secondary_errors = (*getattr(primary, 'secondary_errors', ()),
                        'BOOTSTRAP_ARCHIVE_CLEANUP_FAILED')[:4]
