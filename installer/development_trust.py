"""Independent Linux trust for the single Runtime DEV operation only.

Selections are public, byte-bound inputs, not install capabilities.  A capability
is issued only after the confirmed preparation gate and current real TUF and
Release verification have both succeeded.  Candidate/Formal trust is unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from bootstrap_kit.safe_files import file_digest, held_file, safe_chain
from bootstrap_kit.trust import _enforce_profile_floor, _run_fixed, load_local_trust
from release import trust_bootstrap as tuf
from updater.local_bundle import LocalBundleReleaseSource
from updater.offline import OfflineReleaseVerifier, SigstoreGoEvidenceVerifier

SCHEMA = 'animemo.runtime-development-trust-selection/v1'
PURPOSE = 'SINGLE_RUNTIME_LOCAL_DEVELOPMENT_ONLY'
STATE_ROOT = Path('/var/lib/animemo/local-development')
TRUST_FILES = frozenset(('trust-profile.json', 'offline-release-verifier',
    'github-trusted-root.jsonl', 'sigstore-trusted-root.jsonl',
    'github-tuf-root.json', 'sigstore-tuf-root.json'))
EVIDENCE_FILES = frozenset(('package.json', 'request.json',
    'github-bootstrap.json', 'sigstore-bootstrap.json'))
BUILD_FILES = frozenset(('linux-kit-manifest.json', 'verifier-build-receipt.json'))
FLOORS = {'github': (9, 974, 79, 10), 'sigstore': (15, 795, 165, 14)}
_TOKEN = object()
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_SHA = re.compile(r'[0-9a-f]{40}\Z')


class DevelopmentTrustError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code='DEVELOPMENT_TRUST_INPUT_INVALID'):
    if not condition:
        raise DevelopmentTrustError(code)


def _digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def _json(raw, *, canonical=True):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise DevelopmentTrustError('DEVELOPMENT_TRUST_JSON_INVALID') from None
    _require(not canonical or tuf._canonical_json_bytes(value) == raw)
    return value


def _utc(value):
    _require(type(value) is str and value.endswith('Z'))
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise DevelopmentTrustError('DEVELOPMENT_TRUST_TIME_INVALID') from None
    _require(result.tzinfo is not None)
    return result


def input_binding(binding):
    names = ('execution_source_sha', 'execution_source_tree',
             'execution_inventory_digest', 'verified_candidate_digest')
    _require(type(binding) is dict and all(name in binding for name in names))
    return {name: binding[name] for name in names}


def validate_runtime_trust_selection(value, *, expected_digest, binding, now=None):
    """Validate public selection shape; this never issues installation authority."""
    _require(type(value) is dict and set(value) == {'schema', 'purpose', 'platform',
        'source', 'profile_identity', 'expires_at', 'files', 'product'})
    _require(type(expected_digest) is str and _DIGEST.fullmatch(expected_digest)
        and _digest(tuf._canonical_json_bytes(value)) == expected_digest,
        'DEVELOPMENT_TRUST_SELECTION_CHANGED')
    _require(value['schema'] == SCHEMA and value['purpose'] == PURPOSE
        and value['platform'] == 'linux/amd64', 'DEVELOPMENT_TRUST_PLATFORM_INVALID')
    _require(value['source'] == input_binding(binding), 'DEVELOPMENT_TRUST_SOURCE_MISMATCH')
    _require(all(type(value['source'][name]) is str and _SHA.fullmatch(value['source'][name])
        for name in ('execution_source_sha', 'execution_source_tree')))
    _require(all(type(value['source'][name]) is str and _DIGEST.fullmatch(value['source'][name])
        for name in ('execution_inventory_digest', 'verified_candidate_digest')))
    _require(type(value['profile_identity']) is str and _DIGEST.fullmatch(value['profile_identity']))
    current = datetime.now(timezone.utc) if now is None else now
    _require(current.tzinfo is not None and current < _utc(value['expires_at']),
        'DEVELOPMENT_TRUST_EXPIRED')
    names = {'trust/' + name for name in TRUST_FILES} | {
        'evidence/' + name for name in EVIDENCE_FILES} | {'build/' + name for name in BUILD_FILES}
    _require(type(value['files']) is dict and set(value['files']) == names)
    for record in value['files'].values():
        _require(type(record) is dict and set(record) == {'sha256', 'size'}
            and type(record['size']) is int and 0 < record['size'] <= 64 * 1024 * 1024
            and type(record['sha256']) is str and _DIGEST.fullmatch(record['sha256']))
    product = value['product']
    _require(type(product) is dict and set(product) == {'payload', 'release_attestation', 'release', 'images'})
    for name in ('payload', 'release_attestation'):
        item = product[name]
        _require(type(item) is dict and set(item) == {'sha256', 'size'}
            and type(item['size']) is int and 0 < item['size'] <= 2 * 1024 * 1024 * 1024
            and type(item['sha256']) is str and _DIGEST.fullmatch(item['sha256']))
    release = product['release']
    from installer.runtime import InstallTransportSource, ReleaseEvidence
    _require(type(release) is dict and set(release) == {'version', 'channel', 'commit',
        'manifestDigest', 'materialIdentityDigest', 'deploymentIdentityDigest',
        'deploymentProfile', 'platformProfile', 'transportSource', 'transportPolicyIdentity'})
    evidence = ReleaseEvidence(version=release['version'], channel=release['channel'],
        commit=release['commit'], manifest_digest=release['manifestDigest'],
        material_identity_digest=release['materialIdentityDigest'],
        deployment_identity_digest=release['deploymentIdentityDigest'],
        deployment_profile=release['deploymentProfile'], platform_profile=release['platformProfile'],
        transport_source=InstallTransportSource.LOCAL_BUNDLE,
        transport_policy_identity=release['transportPolicyIdentity'])
    _require(evidence.as_dict() == release)
    _require(type(product['images']) is dict and set(product['images']) == {'api', 'web', 'postgres', 'redis'}
        and all(type(item) is str and _DIGEST.fullmatch(item) for item in product['images'].values()))
    return value


class RuntimeTrustInputs:
    """Held data only.  Not accepted by Installer as an execution capability."""
    __slots__ = (
        '_binding',
        '_closed',
        '_holds',
        '_raw',
        '_streams',
        'digest',
        'payload_path',
        'release_attestation_path',
        'root',
        'selection',
    )

    def __init__(self, token, **values):
        _require(token is _TOKEN, 'DEVELOPMENT_TRUST_INPUT_FORGERY')
        for name, value in values.items():
            setattr(self, name, value)

    def __reduce__(self):
        raise TypeError('Runtime trust inputs cannot be serialized')

    def verify_current(self):
        _require(not self._closed, 'DEVELOPMENT_TRUST_INPUT_CLOSED')
        validate_runtime_trust_selection(self.selection, expected_digest=self.digest, binding=self._binding)
        for name, stream in self._streams.items():
            expected = self._file_records()[name]
            _require(file_digest(stream) == (expected['sha256'], expected['size']),
                'DEVELOPMENT_TRUST_BYTES_CHANGED')
            path = ({'media/portable.tar': self.payload_path,
                     'media/release-attestation.json': self.release_attestation_path}.get(name, self.root / name))
            safe_chain(path)
            opened, current = os.fstat(stream.fileno()), path.lstat()
            _require((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                == (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns),
                'DEVELOPMENT_TRUST_BYTES_CHANGED')
        _require(self.root.joinpath('selection.json').read_bytes() == self._raw,
            'DEVELOPMENT_TRUST_SELECTION_CHANGED')

    def _file_records(self):
        return {**self.selection['files'], 'selection.json': {'sha256': self.digest, 'size': len(self._raw)},
            'media/portable.tar': self.selection['product']['payload'],
            'media/release-attestation.json': self.selection['product']['release_attestation']}

    def staging_files(self):
        self.verify_current()
        paths = {'media/portable.tar': self.payload_path,
                 'media/release-attestation.json': self.release_attestation_path}
        return {name: {'source': paths.get(name, self.root / name), **record}
                for name, record in self._file_records().items()}

    @property
    def guest_inventory_digest(self):
        records = self._file_records()
        directories = sorted({name.split('/')[0] for name in records if '/' in name})
        items = [{'path': name + '/', 'type': 'directory'} for name in directories]
        items.extend({'path': name, 'type': 'file', **record} for name, record in records.items())
        items.sort(key=lambda item: item['path'].removesuffix('/'))
        return _digest(tuf._canonical_json_bytes(items) + b'\n')

    def close(self):
        if not self._closed:
            self._closed = True
            self._holds.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _close_provenance(inputs):
    def read(name):
        stream = inputs._streams[name]
        stream.seek(0)
        return stream.read()
    receipt = _json(read('build/verifier-build-receipt.json'), canonical=False)
    manifest_raw = read('build/linux-kit-manifest.json')
    manifest = _json(manifest_raw, canonical=False)
    from bootstrap_kit.manifest import (
        INITIAL_ROOT_PINS,
        OperatorSelection,
        validate_manifest,
    )
    validate_manifest(manifest_raw, OperatorSelection(
        expected_manifest_sha256=inputs.selection['files']['build/linux-kit-manifest.json']['sha256'],
        expected_platform='linux-amd64-cp312', minimum_sequence=manifest['minimumSequence'],
        minimum_version=manifest['minimumKitVersion'], expected_entry_sha256=manifest['entry']['sha256'],
        expected_entry_size=manifest['entry']['size'], expected_source_commit=manifest['source']['commit'],
        expected_source_tree=manifest['source']['tree']), now=datetime.now(timezone.utc), production=False)
    binary = inputs.selection['files']['trust/offline-release-verifier']
    _require(receipt.get('schema') == 'animemo.independent-verifier-build/v1'
        and receipt.get('status') == 'INDEPENDENT_BUILD_VERIFIED'
        and receipt.get('platform') == 'linux-amd64'
        and receipt.get('recipe', {}).get('GOOS') == 'linux'
        and receipt.get('recipe', {}).get('GOARCH') == 'amd64'
        and receipt.get('recipe', {}).get('CGO_ENABLED') == '0'
        and receipt.get('recipe', {}).get('trimpath') is True
        and receipt.get('recipe', {}).get('mod_readonly') is True
        and receipt.get('binary', {}).get('format') == 'ELF64'
        and {key: receipt.get('binary', {}).get(key) for key in ('sha256', 'size')} == binary,
        'DEVELOPMENT_TRUST_VERIFIER_PROVENANCE_INVALID')
    _require(manifest.get('schema') == 'animemo.bootstrap-trust-kit/v1'
        and manifest.get('classification') == 'DEVELOPMENT_ONLY'
        and manifest.get('platform') == 'linux-amd64-cp312'
        and manifest.get('disabled') is False,
        'DEVELOPMENT_TRUST_KIT_PROVENANCE_INVALID')
    members = [item for item in manifest.get('members', [])
        if item.get('path') == 'trust/offline-release-verifier']
    _require(len(members) == 1 and {key: members[0].get(key) for key in ('sha256', 'size')} == binary,
        'DEVELOPMENT_TRUST_KIT_VERIFIER_MISMATCH')
    tools = [item for item in manifest.get('build', {}).get('toolInputs', [])
        if item.get('name') == 'verifier-build-receipt.json']
    _require(len(tools) == 1 and {key: tools[0].get(key) for key in ('sha256', 'size')}
        == inputs.selection['files']['build/verifier-build-receipt.json'],
        'DEVELOPMENT_TRUST_BUILD_RECEIPT_MISMATCH')
    raw = read('trust/offline-release-verifier')
    _require(raw[:6] == b'\x7fELF\x02\x01' and raw[18:20] == b'\x3e\x00',
        'DEVELOPMENT_TRUST_PLATFORM_INVALID')
    for domain in ('github', 'sigstore'):
        _require(_digest(read('evidence/' + domain + '-bootstrap.json'))
            == manifest.get('trustRoots', {}).get(domain, {}).get('initialSha256') == INITIAL_ROOT_PINS[domain],
            'DEVELOPMENT_TRUST_INITIAL_ROOT_MISMATCH')


def read_runtime_trust_selection(root, *, expected_digest, binding, payload=None, release_attestation=None):
    root = safe_chain(root)
    holds = ExitStack()
    try:
        stream = holds.enter_context(held_file(root / 'selection.json', 128 * 1024))
        raw = stream.read()
        selection = validate_runtime_trust_selection(_json(raw), expected_digest=expected_digest, binding=binding)
        streams = {'selection.json': stream}
        for name, record in selection['files'].items():
            streams[name] = holds.enter_context(held_file(root / name, record['size']))
        payload = root / 'media/portable.tar' if payload is None else Path(payload)
        release_attestation = (root / 'media/release-attestation.json'
            if release_attestation is None else Path(release_attestation))
        for name, key, path in (('media/portable.tar', 'payload', payload),
                ('media/release-attestation.json', 'release_attestation', release_attestation)):
            streams[name] = holds.enter_context(held_file(path, selection['product'][key]['size']))
        inputs = RuntimeTrustInputs(_TOKEN, root=root, selection=selection, digest=expected_digest,
            _holds=holds, _streams=streams, _raw=raw, _binding=dict(binding), _closed=False,
            payload_path=payload, release_attestation_path=release_attestation)
        inputs.verify_current()
        _close_provenance(inputs)
        material = load_local_trust(root / 'trust')
        _require(material.profile.identity == selection['profile_identity'])
        _, tracks = retained_tracks(root / 'evidence')
        _require(_utc(selection['expires_at']) <= retained_expiry(tracks),
            'DEVELOPMENT_TRUST_EXPIRY_MISMATCH')
        for domain, floor in FLOORS.items():
            for suffix, minimum in zip(('root_version', 'timestamp_version', 'snapshot_version', 'targets_version'), floor):
                _require(getattr(material.profile, domain + '_tuf_' + suffix) >= minimum,
                    'DEVELOPMENT_TRUST_VERSION_ROLLBACK')
        return inputs
    except BaseException:
        holds.close()
        raise


load_runtime_trust_inputs = read_runtime_trust_selection


def runtime_material_boundary(inputs):
    _require(type(inputs) is RuntimeTrustInputs)
    inputs.verify_current()
    return {'selection_digest': inputs.digest,
        'release': dict(inputs.selection['product']['release']),
        'images': dict(inputs.selection['product']['images'])}


def retained_tracks(root):
    root = Path(root)
    package_raw = (root / 'package.json').read_bytes()
    request_raw = (root / 'request.json').read_bytes()
    package = _json(package_raw)
    tracks = {}
    try:
        for domain in ('github', 'sigstore'):
            track = package[domain]
            tracks[domain] = ((root / (domain + '-bootstrap.json')).read_bytes(),
                [base64.b64decode(item, validate=True) for item in track['rootChain']],
                *(base64.b64decode(track[name], validate=True)
                    for name in ('timestamp', 'snapshot', 'targets', 'trustedRoot')))
    except (KeyError, TypeError, ValueError):
        raise DevelopmentTrustError('DEVELOPMENT_TRUST_EVIDENCE_INVALID') from None
    identity, expected_package, request = tuf._bootstrap_verification_inputs(tracks)
    _require(tuf._canonical_json_bytes(expected_package) == package_raw
        and tuf._canonical_json_bytes(request) == request_raw,
        'DEVELOPMENT_TRUST_EVIDENCE_INVALID')
    return identity, tracks


def retained_expiry(tracks):
    return min(_utc(_json(raw, canonical=False)['signed']['expires'])
        for track in tracks.values() for raw in ((track[1][-1] if track[1] else track[0]), *track[2:5]))


def verify_retained_trust(inputs, *, verifier_path, expected_verifier_identity, deadline):
    """Real verifier call; host use yields data only, never a Linux capability."""
    inputs.verify_current()
    material = load_local_trust(inputs.root / 'trust')
    with held_file(verifier_path, 64 * 1024 * 1024) as binary:
        _require(file_digest(binary)[0] == expected_verifier_identity,
            'DEVELOPMENT_TRUST_VERIFIER_CHANGED')
        identity, tracks = retained_tracks(inputs.root / 'evidence')
        command = (str(verifier_path), '--trust-update', str(inputs.root / 'evidence/package.json'),
            '--github-tuf-root', str(inputs.root / 'evidence/github-bootstrap.json'),
            '--sigstore-tuf-root', str(inputs.root / 'evidence/sigstore-bootstrap.json'),
            '--request', str(inputs.root / 'evidence/request.json'))
        raw = _run_fixed(command, deadline=deadline)
        claim = _json(raw.rstrip(b'\n'))
        _require(tuf._canonical_json_bytes(claim) + b'\n' == raw)
        claims = tuf._close_claim(claim, bootstrap_identity=identity, tracks=tracks)
        _enforce_profile_floor(claims, material.profile)
        binary.seek(0)
        linux_binary = inputs.root.joinpath('trust/offline-release-verifier').read_bytes()
        profile, files = tuf._bootstrap_material_files(tracks=tracks, claims=claims, verifier_bytes=linux_binary)
        _require(profile.identity == inputs.selection['profile_identity'])
        for name, data in files.items():
            expected = inputs.selection['files']['trust/' + name]
            _require((_digest(data), len(data)) == (expected['sha256'], expected['size']))
        _require(_utc(inputs.selection['expires_at']) <= retained_expiry(tracks))
        inputs.verify_current()
        return {'profile_identity': profile.identity, 'claims': claims,
            'actual_platform': platform.system(), 'verified_at': datetime.now(timezone.utc).isoformat()}


class DevelopmentLocalBundleAuthority:
    __slots__ = ('_closed', '_gate', '_service_source', 'binding', 'inputs', 'material_boundary', 'source')

    def __init__(self, token, **values):
        _require(token is _TOKEN, 'DEVELOPMENT_TRUST_CAPABILITY_FORGERY')
        for name, value in values.items():
            setattr(self, name, value)

    def __reduce__(self):
        raise TypeError('DEV trust capability cannot be serialized')

    def verify_current(self):
        _require(not self._closed, 'DEVELOPMENT_TRUST_CAPABILITY_CLOSED')
        self.inputs.verify_current()
        self._service_source.verify_source()
        self._gate.require_preparation((), binding=self.binding)
        _require(self.material_boundary == runtime_material_boundary(self.inputs))

    def close(self):
        self._closed = True
        self.inputs.close()


def consume_runtime_local_bundle(inputs, *, service_source, preparation_gate, binding=None,
                                 payload=None, release_attestation=None):
    """Actual Linux-only trust and original-product consumer; no test switch."""
    from installer.development import DevelopmentServiceSource
    from installer.development_boundary import DevelopmentExecutionGate
    _require(type(inputs) is RuntimeTrustInputs and type(service_source) is DevelopmentServiceSource
        and type(preparation_gate) is DevelopmentExecutionGate,
        'DEVELOPMENT_TRUST_CAPABILITY_FORGERY')
    binding = inputs._binding if binding is None else binding
    _require(os.name == 'posix' and os.getuid() == 0 and os.geteuid() == 0
        and platform.system() == 'Linux' and platform.machine() in {'x86_64', 'amd64'},
        'DEVELOPMENT_TRUST_RUNTIME_PLATFORM_INVALID')
    service_source.verify_source()
    inputs.verify_current()
    _require(input_binding(binding) == inputs.selection['source']
        and service_source.inventory_digest == binding['execution_inventory_digest']
        and service_source.verified_candidate_digest == binding['verified_candidate_digest']
        and inputs.root == STATE_ROOT / 'runtime-inputs' / inputs.digest[7:],
        'DEVELOPMENT_TRUST_SOURCE_MISMATCH')
    for path in (inputs.root, *inputs.root.rglob('*')):
        metadata = path.lstat()
        _require(metadata.st_uid == 0 and not metadata.st_mode & 0o022,
            'DEVELOPMENT_TRUST_RUNTIME_OWNERSHIP_INVALID')
    payload_path = inputs.root / 'media/portable.tar'
    sidecar_path = inputs.root / 'media/release-attestation.json'
    _require(payload is None or payload == payload_path)
    _require(release_attestation is None or release_attestation == sidecar_path)
    session = binding.get('session_id')
    _require(type(session) is str and re.fullmatch('[0-9a-f]{32}', session))
    cache = STATE_ROOT / 'runtime-cache' / inputs.digest[7:] / session
    preparation_gate.require_preparation((cache, cache / 'proofs'), binding=binding)
    material = load_local_trust(inputs.root / 'trust')
    deadline = time.monotonic() + preparation_gate.remaining_seconds()
    verify_retained_trust(inputs, verifier_path=material.verifier_path,
        expected_verifier_identity=material.profile.verifier_identity, deadline=deadline)
    with ExitStack() as media_holds:
        for name, path in (('payload', payload_path), ('release_attestation', sidecar_path)):
            expected = inputs.selection['product'][name]
            stream = media_holds.enter_context(held_file(path, expected['size']))
            _require(file_digest(stream) == (expected['sha256'], expected['size']),
                'DEVELOPMENT_TRUST_PRODUCT_CHANGED')
        preparation_gate.require_preparation((cache, cache / 'proofs'), binding=binding)
        from scripts.candidate_workload_root import _root_directory
        parent = _root_directory(cache.parent)
        try:
            os.mkdir(cache.name, 0o700, dir_fd=parent)
        finally:
            os.close(parent)
        proof_parent = _root_directory(cache)
        try:
            os.mkdir('proofs', 0o700, dir_fd=proof_parent)
        finally:
            os.close(proof_parent)
        def run(command, **options):
            preparation_gate.require_preparation((), binding=binding)
            stdout = _run_fixed(command, deadline=min(deadline, time.monotonic() + options['timeout']))
            return subprocess.CompletedProcess(command, 0, stdout, b'')
        verifier = OfflineReleaseVerifier(trust_profile=material.profile,
            external_verifier=SigstoreGoEvidenceVerifier(material, runner=run, temporary_root=cache / 'proofs'))
        from updater import __version__
        source = LocalBundleReleaseSource.from_media(payload=payload_path,
            release_attestation=sidecar_path, cache_root=cache / 'bundle',
            verifier=verifier, updater_version=__version__)
        from installer.production import ProductionReleasePort
        from installer.runtime import InstallTransportSource, ReleaseSelector
        release = ProductionReleasePort(source=source,
            transport_source=InstallTransportSource.LOCAL_BUNDLE).resolve(
                ReleaseSelector(version=inputs.selection['product']['release']['version']), refresh=False)
        _require(release.as_dict() == inputs.selection['product']['release'],
            'DEVELOPMENT_TRUST_ORIGINAL_RELEASE_MISMATCH')
        actual = source.release_binding(release.version)
        _require({role: actual[role + 'Digest'] for role in ('api', 'web', 'postgres', 'redis')}
            == inputs.selection['product']['images'], 'DEVELOPMENT_TRUST_IMAGE_MISMATCH')
    authority = DevelopmentLocalBundleAuthority(_TOKEN, inputs=inputs, source=source,
        material_boundary=runtime_material_boundary(inputs), binding=dict(binding),
        _service_source=service_source, _gate=preparation_gate, _closed=False)
    authority.verify_current()
    return authority


class DevelopmentBootstrapPrivilegeGate:
    """Consume verified DEV source/product; never provision original Q trust."""
    def __init__(self, authority):
        _require(type(authority) is DevelopmentLocalBundleAuthority)
        self.authority = authority

    def verify_runtime_source(self, *, version, release_commit):
        from installer.bootstrap import _REQUIRED_RUNTIME_MODULES
        self.authority.verify_current()
        self.authority._service_source.verify_runtime_modules(_REQUIRED_RUNTIME_MODULES)
        release = self.authority.material_boundary['release']
        _require(version == release['version'] and release_commit == release['commit'])
        return {'purpose': PURPOSE, 'selection_digest': self.authority.inputs.digest}

    def consume(self, *, version, release_commit):
        return self.verify_runtime_source(version=version, release_commit=release_commit)
