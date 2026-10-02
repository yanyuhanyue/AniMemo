"""Kit-owned TUF acquisition and bounded execution of the existing verifier."""
from __future__ import annotations

import json
import math
import os
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from bootstrap_kit.safe_files import (
    create_private_directory,
    directory_identity,
    exclusive_file,
    file_digest,
    held_file,
    remove_owned_directory,
    safe_chain,
)
from installer.apt_diagnostics import capture_process
from release import trust_bootstrap as original
from updater.errors import RequestRejected
from updater.offline import PretrustedTrustMaterial, TrustProfile

_FILES = {
    'trust-profile.json': 64 * 1024, 'offline-release-verifier': 64 * 1024 * 1024,
    'github-trusted-root.jsonl': 16 * 1024 * 1024, 'sigstore-trusted-root.jsonl': 16 * 1024 * 1024,
    'github-tuf-root.json': 16 * 1024 * 1024, 'sigstore-tuf-root.json': 16 * 1024 * 1024,
}
_RETAINED_WORK = {}


class KitTrustError(original.TrustBootstrapError):
    def __init__(self, code, *, secondary_errors=(), cleanup_uncertain=False):
        super().__init__(code)
        self.code, self.secondary_errors = code, tuple(secondary_errors)
        self.cleanup_uncertain = cleanup_uncertain


def _require(condition, code='BOOTSTRAP_KIT_TUF_INPUT_INVALID'):
    if not condition:
        raise KitTrustError(code)


def _remaining(deadline, maximum):
    _require(type(deadline) in (int, float) and math.isfinite(deadline))
    remaining = deadline - time.monotonic()
    _require(remaining > 0, 'BOOTSTRAP_KIT_TUF_TIMEOUT')
    return min(maximum, remaining)


def _strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value, 'BOOTSTRAP_KIT_TUF_JSON_INVALID')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise KitTrustError('BOOTSTRAP_KIT_TUF_JSON_INVALID') from None


def _run_fixed(command, *, deadline):
    environment = {'LANG': 'C', 'LC_ALL': 'C'}
    if os.name == 'nt':
        environment['SystemRoot'] = os.environ['SystemRoot']
    try:
        result = capture_process(tuple(command), timeout=_remaining(deadline, 60), environment=environment)
    except (OSError, RuntimeError) as error:
        raise KitTrustError('BOOTSTRAP_KIT_TUF_VERIFIER_PROCESS_FAILED', cleanup_uncertain=True) from error
    secondary = tuple('BOOTSTRAP_KIT_TUF_' + item if item in {'PROCESS_CLEANUP_FAILED', 'OUTPUT_DRAIN_FAILED'}
        else 'BOOTSTRAP_KIT_TUF_SECONDARY_INVALID' for item in result.secondary_errors)
    if result.outcome != 'EXITED':
        suffix = result.outcome if result.outcome in {'TIMEOUT', 'CANCELLED'} else 'PROCESS_FAILED'
        raise KitTrustError('BOOTSTRAP_KIT_TUF_VERIFIER_' + suffix,
                            secondary_errors=secondary, cleanup_uncertain=bool(secondary))
    if result.returncode != 0:
        raise KitTrustError('BOOTSTRAP_KIT_TUF_VERIFIER_REJECTED',
                            secondary_errors=secondary, cleanup_uncertain=bool(secondary))
    if (secondary or result.stdout_summary['truncated'] or result.stdout_summary['missing']
            or result.stderr_summary['truncated'] or result.stderr_summary['missing'] or result.stderr):
        raise KitTrustError('BOOTSTRAP_KIT_TUF_VERIFIER_OUTPUT_INVALID',
                            secondary_errors=secondary, cleanup_uncertain=bool(secondary))
    return result.stdout


def supervised_tuf_fetcher(client, *, deadline):
    from bootstrap_kit.http_protocol import HttpFailure, HttpSelection
    from release.trust_bootstrap import (
        _TRACKS,
        TrustBootstrapError,
        TUFMetadataNotFound,
    )

    total = 0
    def fetch(url, maximum):
        nonlocal total
        _remaining(deadline, 30)
        selection = None
        for track, locations in _TRACKS.items():
            if url == locations['bootstrap']:
                selection = HttpSelection.tuf_bootstrap(track)
            elif url.startswith(locations['repository'] + '/'):
                selection = HttpSelection.tuf(track, url[len(locations['repository']) + 1:])
        if selection is None or type(maximum) is not int or not 0 < maximum <= 16 * 1024 * 1024:
            raise TrustBootstrapError('BOOTSTRAP_KIT_TUF_SELECTION_INVALID')
        try:
            with client.fetch(selection, deadline=min(deadline, time.monotonic() + 30)) as result:
                raw = result.read_bytes(maximum=maximum)
            _require(type(raw) is bytes and 0 < len(raw) <= maximum, 'BOOTSTRAP_KIT_TUF_METADATA_INVALID')
            total += len(raw)
            _require(total <= 64 * 1024 * 1024, 'BOOTSTRAP_KIT_TUF_METADATA_BUDGET')
            return raw
        except HttpFailure as error:
            if error.http_status == 404 and not error.secondary_errors:
                raise TUFMetadataNotFound('BOOTSTRAP_KIT_TUF_METADATA_NOT_FOUND') from None
            failure = TrustBootstrapError(error.code)
            failure.secondary_errors = error.secondary_errors
            raise failure from None
    return fetch


def load_local_trust(root):
    """DEV DTO from six seed-authenticated files; production root rules unchanged."""
    root = safe_chain(root)
    identity = directory_identity(root)
    try:
        with ExitStack() as holds:
            _require({path.name for path in root.iterdir()} == set(_FILES), 'BOOTSTRAP_KIT_LOCAL_TRUST_MEMBER_SET')
            streams = {name: holds.enter_context(held_file(root/name, maximum)) for name, maximum in _FILES.items()}
            raw = streams['trust-profile.json'].read(64 * 1024 + 1)
            record = _strict_json(raw)
            _require(original._canonical_json_bytes(record) == raw, 'BOOTSTRAP_KIT_LOCAL_TRUST_PROFILE_INVALID')
            profile = TrustProfile.from_bootstrap_record(record)
            expected = {
                'offline-release-verifier': profile.verifier_identity,
                'github-trusted-root.jsonl': profile.github_trusted_root_sha256,
                'sigstore-trusted-root.jsonl': profile.sigstore_trusted_root_sha256,
                'github-tuf-root.json': profile.github_tuf_root_sha256,
                'sigstore-tuf-root.json': profile.sigstore_tuf_root_sha256,
            }
            for name, digest in expected.items():
                actual, size = file_digest(streams[name])
                _require(actual == digest and size > 0, 'BOOTSTRAP_KIT_LOCAL_TRUST_BYTES_CHANGED')
            _require(directory_identity(root) == identity and {path.name for path in root.iterdir()} == set(_FILES),
                     'BOOTSTRAP_KIT_LOCAL_TRUST_MEMBER_SET')
        return PretrustedTrustMaterial(root=root, profile=profile,
            github_trusted_root_path=root/'github-trusted-root.jsonl', sigstore_trusted_root_path=root/'sigstore-trusted-root.jsonl',
            github_tuf_root_path=root/'github-tuf-root.json', sigstore_tuf_root_path=root/'sigstore-tuf-root.json',
            verifier_path=root/'offline-release-verifier')
    except RequestRejected:
        raise KitTrustError('BOOTSTRAP_KIT_LOCAL_TRUST_PROFILE_INVALID') from None


def _write_held(root, name, raw, holds, maximum):
    _require(type(raw) is bytes and 0 < len(raw) <= maximum, 'BOOTSTRAP_KIT_TUF_INPUT_BUDGET')
    path = root/name
    with exclusive_file(path) as stream:
        stream.write(raw)
    if os.name != 'nt':
        path.chmod(0o500 if name == 'verifier' else 0o400)
    stream = holds.enter_context(held_file(path, maximum))
    _require(file_digest(stream) == (original._digest(raw), len(raw)), 'BOOTSTRAP_KIT_TUF_INPUT_CHANGED')
    return path


def _enforce_profile_floor(claims, profile):
    """Retain caller-authenticated version floors after fresh Go verification."""
    if profile is None:
        return
    _require(type(profile) is TrustProfile, 'BOOTSTRAP_KIT_TUF_FLOOR_INVALID')
    for domain in ('github', 'sigstore'):
        current = claims[domain]
        for field, suffix in (('tufRootVersion', 'root_version'), ('timestampVersion', 'timestamp_version'),
                              ('snapshotVersion', 'snapshot_version'), ('targetsVersion', 'targets_version')):
            floor = getattr(profile, domain + '_tuf_' + suffix)
            _require(current[field] >= floor, 'BOOTSTRAP_KIT_TUF_VERSION_ROLLBACK')
        if current['tufRootVersion'] == getattr(profile, domain + '_tuf_root_version'):
            _require(current['tufRootSha256'] == getattr(profile, domain + '_tuf_root_sha256'),
                     'BOOTSTRAP_KIT_TUF_SAME_VERSION_ROOT_CHANGED')
        if current['targetsVersion'] == getattr(profile, domain + '_tuf_targets_version'):
            _require(current['trustedRootSha256'] == getattr(profile, domain + '_trusted_root_sha256'),
                     'BOOTSTRAP_KIT_TUF_SAME_VERSION_TARGET_CHANGED')


class _ProtocolEvidence:
    """Bounded public TUF originals and actual Go output; no stderr or headers."""
    def __init__(self, parent, holds, minimum_profile, verifier_identity):
        self.root = create_private_directory(parent, prefix='tuf-evidence-')
        self.holds, self.materials, self.total, self.public_count = holds, [], 0, 0
        self.status = 'UNVERIFIED'
        self.verification_window = None
        self.minimum_profile_identity = minimum_profile.identity if minimum_profile is not None else None
        self.verifier_identity = verifier_identity
        if minimum_profile is not None:
            self.save('authenticated-minimum-profile.json',
                      original._canonical_json_bytes(minimum_profile.as_bootstrap_record()), 64 * 1024)

    def save(self, name, raw, maximum):
        path = _write_held(self.root, name, raw, self.holds, maximum)
        record = {'name': name, 'size': len(raw), 'sha256': original._digest(raw)}
        self.materials.append(record)
        return path

    def observe_fetch(self, fetch):
        def observed(url, maximum):
            # The original supervised fetcher validates the exact two domains,
            # target grammar and shared deadline before returning any bytes.
            raw = fetch(url, maximum)
            _require(self.public_count < 74 and self.total + len(raw) <= 64 * 1024 * 1024,
                     'BOOTSTRAP_KIT_TUF_EVIDENCE_BUDGET')
            self.total += len(raw)
            self.public_count += 1
            self.save(f'public-{self.public_count:03d}.json', raw, maximum)
            self.materials[-1]['source'] = url
            return raw
        return observed

    def save_input(self, name, path, maximum):
        with held_file(path, maximum) as stream:
            self.save(name, stream.read(maximum + 1), maximum)

    def finish(self):
        value = {'schema': 'animemo.tuf-protocol-evidence/v1',
            'authority': 'LOCAL_EXECUTION_OBSERVATION_NOT_SIGNED_ATTESTATION',
            'status': self.status, 'minimumProfileIdentity': self.minimum_profile_identity,
            'verifierIdentity': self.verifier_identity, 'verifierVersion': original._VERIFIER_VERSION,
            'verificationWindow': self.verification_window,
            'clock': 'Each domain fixes its own Go trustedmetadata.New RefTime during this actual verifier call; exact RefTime is not exported.',
            'independentPrior': original._TRACKS, 'materials': self.materials,
            'rawMetadataBytes': self.total}
        self.save('evidence-index.json', original._canonical_json_bytes(value), 256 * 1024)


def refresh_trust(*, verifier, output, client, deadline, retain_evidence=False, minimum_profile=None):
    """Original Go TUF verification; only six DEVELOPMENT_ONLY data files."""
    output = Path(output).absolute()
    parent = safe_chain(output.parent)
    _require(type(retain_evidence) is bool and (minimum_profile is None or type(minimum_profile) is TrustProfile),
             'BOOTSTRAP_KIT_TUF_OPTIONS_INVALID')
    _require(not output.exists() and not output.is_symlink(), 'BOOTSTRAP_KIT_TUF_OUTPUT_EXISTS')
    _remaining(deadline, 60)
    with held_file(verifier, 64 * 1024 * 1024) as stream:
        verifier_size = os.fstat(stream.fileno()).st_size
        verifier_bytes = stream.read(verifier_size + 1)
        _require(0 < len(verifier_bytes) == verifier_size, 'BOOTSTRAP_KIT_TUF_VERIFIER_CHANGED')
    scratch = create_private_directory(parent, prefix='tuf-verifier-')
    identity = directory_identity(scratch)
    holds, primary, retained = ExitStack(), None, False
    material_root, material_identity = None, None
    evidence = None
    try:
        if retain_evidence:
            evidence = _ProtocolEvidence(parent, holds, minimum_profile, original._digest(verifier_bytes))
        executable = _write_held(scratch, 'verifier.exe' if os.name == 'nt' else 'verifier',
                                 verifier_bytes, holds, 64 * 1024 * 1024)
        _require(_run_fixed((str(executable), '--version'), deadline=deadline)
                 == (original._VERIFIER_VERSION + '\n').encode('ascii'), 'BOOTSTRAP_KIT_TUF_VERIFIER_VERSION_INVALID')
        fetcher = supervised_tuf_fetcher(client, deadline=deadline)
        if evidence is not None:
            fetcher = evidence.observe_fetch(fetcher)
        tracks = {role: original._acquire_track(role, fetcher=fetcher) for role in ('github', 'sigstore')}
        bootstrap_identity, package, request = original._bootstrap_verification_inputs(tracks)
        package_path = _write_held(scratch, 'package.json', original._canonical_json_bytes(package), holds, 96 * 1024 * 1024)
        request_path = _write_held(scratch, 'request.json', original._canonical_json_bytes(request), holds, 64 * 1024)
        github_root = _write_held(scratch, 'github-root.json', tracks['github'][0], holds, 16 * 1024 * 1024)
        sigstore_root = _write_held(scratch, 'sigstore-root.json', tracks['sigstore'][0], holds, 16 * 1024 * 1024)
        if evidence is not None:
            for name, path, maximum in (('package.json', package_path, 96 * 1024 * 1024),
                ('request.json', request_path, 64 * 1024), ('github-bootstrap.json', github_root, 16 * 1024 * 1024),
                ('sigstore-bootstrap.json', sigstore_root, 16 * 1024 * 1024)):
                evidence.save_input(name, path, maximum)
            evidence.verification_window = {'startedAt': datetime.now(timezone.utc).isoformat()}
        raw = _run_fixed((str(executable), '--trust-update', str(package_path),
            '--github-tuf-root', str(github_root), '--sigstore-tuf-root', str(sigstore_root),
            '--request', str(request_path)), deadline=deadline)
        if evidence is not None:
            evidence.verification_window['endedAt'] = datetime.now(timezone.utc).isoformat()
        claim = _strict_json(raw)
        _require(raw == original._canonical_json_bytes(claim) + b'\n', 'BOOTSTRAP_KIT_TUF_CLAIM_INVALID')
        claims = original._close_claim(claim, bootstrap_identity=bootstrap_identity, tracks=tracks)
        _enforce_profile_floor(claims, minimum_profile)
        if evidence is not None:
            evidence.save('actual-go-claim.json', raw, 64 * 1024)
            evidence.save('verified-version-floors.json', original._canonical_json_bytes({
                'schema': 'animemo.tuf-verified-floors/v1', 'tracks': claims,
                'verifierIdentity': original._digest(verifier_bytes),
                'bootstrapIdentity': bootstrap_identity}), 64 * 1024)
            evidence.status = 'VERIFIED_PROTOCOL'
        profile, files = original._bootstrap_material_files(tracks=tracks, claims=claims, verifier_bytes=verifier_bytes)
        _require(set(files) == set(_FILES), 'BOOTSTRAP_KIT_LOCAL_TRUST_MEMBER_SET')
        material_root = create_private_directory(parent, prefix='tuf-materials-')
        material_identity = directory_identity(material_root)
        for name, raw in files.items():
            with exclusive_file(material_root/name) as stream:
                stream.write(raw)
            if os.name != 'nt':
                (material_root/name).chmod(0o500 if name == 'offline-release-verifier' else 0o400)
        _require(load_local_trust(material_root).profile == profile, 'BOOTSTRAP_KIT_LOCAL_TRUST_PROFILE_INVALID')
        _require(material_root.resolve().parent == parent and output.parent == parent
                 and not output.exists() and not output.is_symlink(), 'BOOTSTRAP_KIT_TUF_OUTPUT_EXISTS')
        if os.name == 'nt':
            material_root.rename(output)
            material_root = output
        else:
            # Exclusive destination creation, never POSIX replacement rename.
            output.mkdir(mode=0o700)
            output_identity = directory_identity(output)
            try:
                for name, raw in files.items():
                    with exclusive_file(output/name) as stream:
                        stream.write(raw)
                    (output/name).chmod(0o500 if name == 'offline-release-verifier' else 0o400)
            except BaseException:
                remove_owned_directory(output, output_identity)
                raise
            remove_owned_directory(material_root, material_identity)
            material_root, material_identity = output, output_identity
        result = {'authority': 'DEVELOPMENT_ONLY', 'state': 'TEST_ONLY', 'status': 'VERIFIED',
            'files': 6, 'profileIdentity': profile.identity, 'verifierIdentity': original._digest(verifier_bytes),
            'bootstrapIdentity': bootstrap_identity, 'tracks': claims, 'observedAt': datetime.now(timezone.utc).isoformat(),
            'materialFiles': [{'name': name, 'sha256': original._digest(raw), 'size': len(raw)} for name, raw in sorted(files.items())]}
        if evidence is not None:
            evidence.status = 'VERIFIED_CURRENT'
            evidence.finish()
            result['protocolEvidence'] = {'root': str(evidence.root),
                'indexSha256': original._digest((evidence.root/'evidence-index.json').read_bytes())}
        return result
    except BaseException as error:
        primary = error
        if evidence is not None and evidence.verification_window is not None:
            evidence.verification_window.setdefault('endedAt', datetime.now(timezone.utc).isoformat())
        if evidence is not None and not (evidence.root/'evidence-index.json').exists():
            try:
                evidence.finish()
            except (OSError, ValueError):
                error.secondary_errors = (*getattr(error, 'secondary_errors', ()), 'BOOTSTRAP_KIT_TUF_EVIDENCE_WRITE_FAILED')
        if getattr(error, 'cleanup_uncertain', False):
            _RETAINED_WORK[identity] = (scratch, holds)
            retained = True
        if material_root is not None:
            try:
                remove_owned_directory(material_root, material_identity)
            except (OSError, ValueError):
                error.secondary_errors = (*getattr(error, 'secondary_errors', ()), 'BOOTSTRAP_KIT_TUF_OUTPUT_CLEANUP_FAILED')
        raise
    finally:
        if not retained:
            try:
                holds.close()
                remove_owned_directory(scratch, identity)
            except (OSError, ValueError):
                if primary is None:
                    raise KitTrustError('BOOTSTRAP_KIT_TUF_CLEANUP_FAILED') from None
                primary.secondary_errors = (*getattr(primary, 'secondary_errors', ()), 'BOOTSTRAP_KIT_TUF_CLEANUP_FAILED')
