"""A closed local-development service source, separate from all Q material roots."""
from __future__ import annotations

import hashlib
import importlib
import os
import re
import stat
from pathlib import Path

from scripts.closed_runtime_inventory import closed_runtime_inventory_digest

SCHEMA = 'animemo.local-development-installed-service-source/v1'
PACKAGES = ('durability', 'installer', 'release', 'updater')
ASSETS = {
    'deploy/updater/animemo-updater': '/opt/animemo-updater/launcher',
    'deploy/updater/animemo': '/opt/animemo-updater/animemo-launcher',
    'deploy/updater/animemo-updater@.service': '/etc/systemd/system/animemo-updater@.service',
    'deploy/updater/animemo-updater.sysusers.conf': '/usr/lib/sysusers.d/animemo-updater.conf',
    'deploy/updater/animemo-updater.tmpfiles.conf': '/usr/lib/tmpfiles.d/animemo-updater.conf',
}


class DevelopmentServiceError(ValueError):
    code = 'DEVELOPMENT_SERVICE_SOURCE_MISMATCH'


def _file_digest(path):
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise DevelopmentServiceError()
    with path.open('rb') as source:
        return 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()


def expected_service_observation(root, inventory_digest):
    version = re.findall(r'^__version__ = "([0-9][0-9.]*)"$',
        (root / 'updater/__init__.py').read_text(encoding='utf-8'), flags=re.MULTILINE)
    if len(version) != 1:
        raise DevelopmentServiceError()
    return {'schema': SCHEMA, 'result': 'PASS', 'execution_inventory_digest': inventory_digest,
        'installed_root': '/opt/animemo-updater/releases/' + version[0],
        'package_inventories': {name: closed_runtime_inventory_digest(root / name) for name in PACKAGES},
        'installed_assets': {target: _file_digest(root / name) for name, target in ASSETS.items()}}


class DevelopmentServiceSource:
    __slots__ = ('root', 'inventory_digest', 'verified_candidate_digest', '_expected')

    def __init__(self, *_args, **_kwargs):
        raise TypeError('Development service source must be acquired from a sealed execution tree')

    def __reduce__(self):
        raise TypeError('Development service source cannot be serialized')

    def verify_source(self):
        if (os.name != 'posix' or os.geteuid() != 0
                or self.root != Path('/var/lib/animemo/local-development') / self.inventory_digest.removeprefix('sha256:')
                or self.root.is_symlink() or self.root.stat().st_uid != 0
                or self.root.stat().st_mode & 0o022
                or closed_runtime_inventory_digest(self.root) != self.inventory_digest):
            raise DevelopmentServiceError()

    def observe_installed(self):
        self.verify_source()
        current = Path('/opt/animemo-updater/current')
        if not current.is_symlink() or current.resolve(strict=True) != Path(self._expected['installed_root']):
            raise DevelopmentServiceError()
        root = current.resolve(strict=True)
        observed = {'schema': SCHEMA, 'result': 'PASS', 'execution_inventory_digest': self.inventory_digest,
            'installed_root': str(root),
            'package_inventories': {name: closed_runtime_inventory_digest(root / name) for name in PACKAGES},
            'installed_assets': {target: _file_digest(Path(target)) for target in ASSETS.values()}}
        if observed != self._expected:
            raise DevelopmentServiceError()
        return observed

    def verify_runtime_modules(self, module_names):
        self.verify_source()
        for name in sorted(module_names):
            module = importlib.import_module(name)
            source = getattr(module, '__file__', None)
            if (type(source) is not str
                    or Path(source).resolve(strict=True) != self.root / (name.replace('.', '/') + '.py')):
                raise DevelopmentServiceError()
        self.verify_source()


def acquire_development_service_source(binding):
    digest = binding['execution_inventory_digest']
    if type(digest) is not str or re.fullmatch('sha256:[0-9a-f]{64}', digest) is None:
        raise DevelopmentServiceError()
    value = object.__new__(DevelopmentServiceSource)
    value.root = Path('/var/lib/animemo/local-development') / digest.removeprefix('sha256:')
    value.inventory_digest = digest
    value.verified_candidate_digest = binding['verified_candidate_digest']
    if Path(__file__).resolve().parents[1] != value.root:
        raise DevelopmentServiceError()
    value.verify_source()
    value._expected = expected_service_observation(value.root, digest)
    return value
