"""Anonymous Release verification using independently provisioned Sigstore trust.

The public local-verification function returns evidence only. Production uses
the fixed operator trust directory; it never discovers a verifier in the asset
that it is about to authorize. Provisioning that first trust is external to
this module and cannot be inferred from an archive's self-consistent hashes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime
from contextlib import ExitStack, contextmanager
from pathlib import Path

from installer.apt_diagnostics import capture_process
from updater.errors import RequestRejected
from updater.offline import PretrustedTrustMaterial, TrustProfile

OPERATOR_TRUST_ROOT = Path('/usr/share/animemo/stage0-trust/v1')
POLICY = 'ANONYMOUS_GITHUB_RELEASE_SIGSTORE_STAGE0_V1'
CARRIER = 'INDEPENDENT_OPERATOR_PRETRUST_TOKENLESS_SIGSTORE_GO'
_REPOSITORY = 'yanyuhanyue/AniMemo'
_ISSUER = object()
_MAX_JSON = 8 * 1024 * 1024
_SECONDARY_CODES = frozenset({
    'BOOTSTRAP_TOKENLESS_CLEANUP_FAILED', 'BOOTSTRAP_TOKENLESS_HANDLE_CLOSE_FAILED',
    'BOOTSTRAP_TOKENLESS_FILE_CHANGED', 'BOOTSTRAP_TOKENLESS_PROCESS_CLEANUP_FAILED',
    'BOOTSTRAP_TOKENLESS_OUTPUT_DRAIN_FAILED', 'BOOTSTRAP_TOKENLESS_SECONDARY_INVALID',
})


class TokenlessStage0Error(ValueError):
    def __init__(self, code, *, returncode=None):
        self.code, self.returncode = code, returncode
        self.secondary_errors = ()
        super().__init__(code)


def _require(value, code='BOOTSTRAP_TOKENLESS_IDENTITY_MISMATCH'):
    if not value:
        raise TokenlessStage0Error(code)


def _bounded_value(value):
    """Bound DTOs before serialization, including cycles and excessive nesting."""
    pending, seen, count, characters = [(value, 0)], set(), 0, 0
    while pending:
        item, depth = pending.pop()
        count += 1
        _require(count <= 10000 and depth <= 64, 'BOOTSTRAP_TOKENLESS_JSON_INVALID')
        if type(item) in (dict, list, tuple):
            _require(id(item) not in seen and len(item) <= 10000, 'BOOTSTRAP_TOKENLESS_JSON_INVALID')
            seen.add(id(item))
            if type(item) is dict:
                _require(all(type(key) is str for key in item), 'BOOTSTRAP_TOKENLESS_JSON_INVALID')
                pending.extend((key, depth + 1) for key in item)
                pending.extend((value, depth + 1) for value in item.values())
            else:
                pending.extend((value, depth + 1) for value in item)
        elif type(item) is str:
            characters += len(item)
            _require(characters <= _MAX_JSON, 'BOOTSTRAP_TOKENLESS_JSON_INVALID')
        else:
            _require(item is None or type(item) is bool or type(item) is int and abs(item) < 2**64,
                     'BOOTSTRAP_TOKENLESS_JSON_INVALID')


def _canonical(value):
    _bounded_value(value)
    try:
        raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(',', ':')) + '\n').encode('utf-8')
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_JSON_INVALID') from None
    _require(len(raw) <= _MAX_JSON, 'BOOTSTRAP_TOKENLESS_JSON_INVALID')
    return raw


def _json(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= _MAX_JSON,
             'BOOTSTRAP_TOKENLESS_JSON_INVALID')
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value, 'BOOTSTRAP_TOKENLESS_JSON_INVALID')
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _bounded_value(value)
        return value
    except (UnicodeError, ValueError, RecursionError):
        raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_JSON_INVALID') from None


def _state(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _same_file_identity(path_state, handle_state):
    # Windows lstat infers executable mode bits from the filename, whereas
    # fstat cannot. It also reports a different ctime notion. Neither affects
    # the stable volume/file ID, type, link count, size, or modification time.
    if os.name == 'nt':
        return (path_state.st_dev, path_state.st_ino, stat.S_IFMT(path_state.st_mode),
                path_state.st_nlink, path_state.st_size, path_state.st_mtime_ns) == (
                handle_state.st_dev, handle_state.st_ino, stat.S_IFMT(handle_state.st_mode),
                handle_state.st_nlink, handle_state.st_size, handle_state.st_mtime_ns)
    return _state(path_state) == _state(handle_state)


def _safe_chain(path, *, production=False):
    for parent in (path, *path.parents):
        _require(not parent.is_symlink() and not parent.is_junction(),
                 'BOOTSTRAP_TOKENLESS_FILE_UNSAFE')
        if production:
            metadata = parent.lstat()
            _require(metadata.st_uid == 0 and not metadata.st_mode & 0o022,
                     'BOOTSTRAP_TOKENLESS_FILE_UNSAFE')


def _secondary_or_raise(primary, code):
    if primary is None:
        raise TokenlessStage0Error(code) from None
    primary.secondary_errors = tuple(dict.fromkeys(
        (*getattr(primary, 'secondary_errors', ()), code)))[:4]


@contextmanager
def _closing_stack():
    stack, primary = ExitStack(), None
    try:
        yield stack
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            stack.close()
        except Exception:  # noqa: BLE001 - handle cleanup cannot replace the primary failure.
            _secondary_or_raise(primary, 'BOOTSTRAP_TOKENLESS_HANDLE_CLOSE_FAILED')


@contextmanager
def _held_file(path, maximum, *, production=False):
    path = Path(path).absolute()
    _safe_chain(path, production=production)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
        and 0 < before.st_size <= maximum, 'BOOTSTRAP_TOKENLESS_FILE_UNSAFE')
    if production:
        _require(os.name == 'posix' and before.st_uid == 0 and not before.st_mode & 0o022,
                 'BOOTSTRAP_TOKENLESS_FILE_UNSAFE')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        | getattr(os, 'O_BINARY', 0))
    primary, opened = None, None
    try:
        opened = os.fstat(descriptor)
        # Windows path and handle APIs disagree about ctime (creation/change).
        # Compare identity across APIs, then retain each API's complete state.
        _require(_same_file_identity(before, opened), 'BOOTSTRAP_TOKENLESS_FILE_CHANGED')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            yield stream
    except BaseException as error:
        primary = error
        raise
    finally:
        secondary = []
        try:
            if (opened is not None and _state(opened) != _state(os.fstat(descriptor))
                    or _state(before) != _state(path.lstat())):
                secondary.append('BOOTSTRAP_TOKENLESS_FILE_CHANGED')
        except OSError:
            secondary.append('BOOTSTRAP_TOKENLESS_FILE_CHANGED')
        try:
            os.close(descriptor)
        except OSError:
            secondary.append('BOOTSTRAP_TOKENLESS_HANDLE_CLOSE_FAILED')
        if secondary and primary is None:
            failure = TokenlessStage0Error(secondary[0])
            failure.secondary_errors = tuple(secondary[1:])
            raise failure from None
        for code in secondary:
            _secondary_or_raise(primary, code)


def _file_digest(stream):
    stream.seek(0)
    digest, size = hashlib.sha256(), 0
    maximum = os.fstat(stream.fileno()).st_size
    _require(0 < maximum <= 2 * 1024 * 1024 * 1024, 'BOOTSTRAP_TOKENLESS_FILE_UNSAFE')
    while chunk := stream.read(min(1024 * 1024, maximum - size + 1)):
        size += len(chunk)
        _require(size <= maximum, 'BOOTSTRAP_TOKENLESS_FILE_CHANGED')
        digest.update(chunk)
    _require(size == maximum, 'BOOTSTRAP_TOKENLESS_FILE_CHANGED')
    return 'sha256:' + digest.hexdigest(), size


@contextmanager
def _scratch(parent=None):
    parent = Path(parent or tempfile.gettempdir()).absolute()
    _safe_chain(parent)
    if os.name == 'nt':
        from bootstrap_kit.safe_files import create_private_directory
        root = create_private_directory(parent, prefix='tokenless-verifier-')
    else:
        root = Path(tempfile.mkdtemp(prefix='animemo-tokenless-', dir=parent))
    created = root.lstat()
    primary = None
    try:
        with _closing_stack() as holds:
            if os.name == 'nt':
                from bootstrap_kit.safe_files import hold_path_chain
                from release.formal_windows_pretrust import assert_windows_private_acl
                holds.enter_context(hold_path_chain(root))
                current = root.lstat()
                _require((created.st_dev, created.st_ino) == (current.st_dev, current.st_ino),
                         'BOOTSTRAP_TOKENLESS_SCRATCH_CHANGED')
                assert_windows_private_acl(root)
            yield root
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            _safe_chain(root)
            current = root.lstat()
            _require(root.parent == parent and (created.st_dev, created.st_ino) ==
                     (current.st_dev, current.st_ino), 'BOOTSTRAP_TOKENLESS_SCRATCH_CHANGED')
            if os.name == 'nt':
                from bootstrap_kit.safe_files import remove_owned_directory
                remove_owned_directory(root, (created.st_dev, created.st_ino))
            else:
                shutil.rmtree(root)
        except Exception:  # noqa: BLE001 - retain the primary failure without cleanup exception text.
            _secondary_or_raise(primary, 'BOOTSTRAP_TOKENLESS_CLEANUP_FAILED')


def _execute(material, bundle, request, *, scratch_parent=None):
    """Copy hash-bound executable and root to a private, zero-account context."""
    _require(type(material) is PretrustedTrustMaterial, 'BOOTSTRAP_TOKENLESS_TRUST_REQUIRED')
    try:
        _require(type(material.profile) is TrustProfile
            and TrustProfile.from_bootstrap_record(material.profile.as_bootstrap_record()) == material.profile,
            'BOOTSTRAP_TOKENLESS_TRUST_REQUIRED')
    except (ValueError, RequestRejected) as error:
        failure = TokenlessStage0Error('BOOTSTRAP_TOKENLESS_TRUST_REQUIRED')
        failure.secondary_errors = tuple(dict.fromkeys(code if code in _SECONDARY_CODES
            else 'BOOTSTRAP_TOKENLESS_SECONDARY_INVALID'
            for code in getattr(error, 'secondary_errors', ())))[:4]
        raise failure from None
    with _scratch(scratch_parent) as scratch:
        executable = scratch / ('verifier.exe' if os.name == 'nt' else 'verifier')
        _require(type(request) is dict and request.get('mode') in
                 {'github-release', 'actions-provenance'}, 'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
        actions = request['mode'] == 'actions-provenance'
        root_path = scratch / ('sigstore-root.json' if actions else 'github-root.json')
        root_source = material.sigstore_trusted_root_path if actions else material.github_trusted_root_path
        root_digest = (material.profile.sigstore_trusted_root_sha256 if actions
                       else material.profile.github_trusted_root_sha256)
        for source, target, expected, maximum in (
            (material.verifier_path, executable, material.profile.verifier_identity, 256 * 1024 * 1024),
            (root_source, root_path, root_digest, _MAX_JSON),
        ):
            with _held_file(source, maximum) as stream, target.open('xb') as output:
                digest, size = hashlib.sha256(), 0
                limit = os.fstat(stream.fileno()).st_size
                while chunk := stream.read(min(1024 * 1024, limit - size + 1)):
                    size += len(chunk)
                    _require(size <= limit, 'BOOTSTRAP_TOKENLESS_TRUST_CHANGED')
                    digest.update(chunk)
                    output.write(chunk)
                _require(size == limit and 'sha256:' + digest.hexdigest() == expected,
                         'BOOTSTRAP_TOKENLESS_TRUST_CHANGED')
            if os.name != 'nt':
                target.chmod(0o500 if target == executable else 0o400)
        bundle_path, request_path = scratch / 'bundle.json', scratch / 'request.json'
        bundle_path.write_bytes(bundle)
        request_path.write_bytes(_canonical(request))
        directories = {key: scratch / key.lower() for key in ('HOME','GH_CONFIG_DIR','XDG_CONFIG_HOME','XDG_CACHE_HOME')}
        for directory in directories.values():
            directory.mkdir(mode=0o700)
        environment = {key: str(path) for key, path in directories.items()}
        environment.update(LANG='C.UTF-8', LC_ALL='C.UTF-8', GH_PROMPT_DISABLED='1')
        if os.name == 'nt':
            environment['SystemRoot'] = os.environ['SystemRoot']
        with _closing_stack() as holds:
            if os.name == 'nt':
                from release.formal_windows_pretrust import hold_windows_private_file
                for path in (executable, root_path, bundle_path, request_path):
                    holds.enter_context(hold_windows_private_file(path))
            result = capture_process((str(executable), '--bundle', str(bundle_path),
                '--trusted-root', str(root_path), '--request', str(request_path)),
                timeout=60, environment=environment)
            if result.outcome != 'EXITED' or result.secondary_errors or result.returncode != 0:
                suffix = ('TIMEOUT' if result.outcome == 'TIMEOUT' else 'CANCELLED'
                    if result.outcome == 'CANCELLED' else 'PROCESS_FAILED'
                    if result.outcome != 'EXITED' else 'SIGNATURE_REJECTED'
                    if result.returncode != 0 else 'PROCESS_FAILED')
                code = 'BOOTSTRAP_TOKENLESS_' + (suffix if suffix == 'SIGNATURE_REJECTED' else 'VERIFIER_' + suffix)
                error = TokenlessStage0Error(code, returncode=result.returncode)
                allowed = {'PROCESS_CLEANUP_FAILED', 'OUTPUT_DRAIN_FAILED'}
                error.secondary_errors = tuple(sorted({
                    'BOOTSTRAP_TOKENLESS_' + item if type(item) is str and item in allowed
                    else 'BOOTSTRAP_TOKENLESS_SECONDARY_INVALID' for item in result.secondary_errors}))[:4]
                raise error
            _require(not result.stderr and not result.stdout_summary['truncated']
                and not result.stdout_summary['missing'] and not result.stderr_summary['missing'],
                'BOOTSTRAP_TOKENLESS_VERIFIER_OUTPUT_INVALID')
            value = _json(result.stdout)
            _require(type(value) is dict and _canonical(value) == result.stdout,
                     'BOOTSTRAP_TOKENLESS_VERIFIER_OUTPUT_INVALID')
            _readback_equal(bundle_path, bundle)
            _readback_equal(request_path, _canonical(request))
            return value


def _readback_equal(path, expected):
    """Re-read beneath the original verifier holds with a fixed byte bound."""
    _require(type(expected) is bytes and 0 < len(expected) <= _MAX_JSON,
             'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
    with _held_file(path, len(expected)) as stream:
        _require(stream.read(len(expected) + 1) == expected, 'BOOTSTRAP_TOKENLESS_FILE_CHANGED')


class TokenlessActionsVerifier:
    """Existing Go Actions policy through the same bounded, offline process port.

    Material must already be independently authenticated by the caller. This
    object does not provision trust or issue a production bootstrap capability.
    """
    def __init__(self, material, *, scratch_parent=None):
        _require(type(material) is PretrustedTrustMaterial, 'BOOTSTRAP_TOKENLESS_TRUST_REQUIRED')
        self.material, self.scratch_parent = material, scratch_parent

    def verify(self, *, bundle, evidence_name, subject_name, subject_sha256,
               workflow, source_commit):
        try:
            return self._verify(bundle=bundle, evidence_name=evidence_name, subject_name=subject_name,
                subject_sha256=subject_sha256, workflow=workflow, source_commit=source_commit)
        except TokenlessStage0Error:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            failure = TokenlessStage0Error('BOOTSTRAP_TOKENLESS_ACTIONS_INPUT_OR_IO_FAILED')
            failure.secondary_errors = tuple(code if code in _SECONDARY_CODES
                else 'BOOTSTRAP_TOKENLESS_SECONDARY_INVALID'
                for code in getattr(error, 'secondary_errors', ()))[:4]
            raise failure from None

    def _verify(self, *, bundle, evidence_name, subject_name, subject_sha256,
                workflow, source_commit):
        from release.publication_evidence import close_actions_provenance_claim, PublicationEvidenceError
        subjects = {'api-image': 'ghcr.io/yanyuhanyue/animemo-api',
                    'web-image': 'ghcr.io/yanyuhanyue/animemo-web',
                    'release-manifest': 'release-manifest.json',
                    'deployment-contract': 'deployment-contract.json',
                    'installer-materials': 'installer-materials.tar'}
        _require(evidence_name in subjects and subject_name == subjects[evidence_name]
            and type(subject_sha256) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', subject_sha256)
            and workflow in {'.github/workflows/release.yml', '.github/workflows/promote-release.yml'}
            and (type(source_commit) is str and re.fullmatch(r'[0-9a-f]{40}', source_commit)
                or source_commit is None and workflow == '.github/workflows/promote-release.yml'
                and evidence_name not in {'api-image', 'web-image'}), 'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
        envelope = _json(bundle)
        _require(type(envelope) is dict and type(envelope.get('dsseEnvelope')) is dict,
                 'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
        request = {'schemaVersion': 1, 'mode': 'actions-provenance', 'evidenceName': evidence_name,
                   'subject': {'name': subject_name, 'sha256': subject_sha256, 'size': 0}, 'workflow': workflow}
        if source_commit is not None:
            request['sourceCommit'] = source_commit
        raw = _execute(self.material, bundle, request, scratch_parent=self.scratch_parent)
        try:
            claim = close_actions_provenance_claim(raw)
        except PublicationEvidenceError:
            raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_ACTIONS_CLAIM_INVALID') from None
        _require(claim.subject_name == subject_name and claim.subject_digest == subject_sha256
            and claim.workflow == workflow and (source_commit is None or claim.source_commit == source_commit),
            'BOOTSTRAP_TOKENLESS_ACTIONS_CLAIM_INVALID')
        return claim


def _verify_release_locally(*, material, inputs, archive, expected_commit, scratch_parent=None):
    """Real crypto and local byte verification; returns no production capability."""
    from installer.anonymous_release_transport import (
        UntrustedReleaseMaterials, AnonymousReleaseError, _metadata, _VERSION,
    )
    _require(type(inputs) is UntrustedReleaseMaterials, 'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
    _require(type(inputs.version) is str and len(inputs.version) <= 128 and _VERSION.fullmatch(inputs.version)
        and all(type(value) is str and re.fullmatch(r'[0-9a-f]{40}', value)
                for value in (inputs.tag_commit, inputs.tag_object, expected_commit)),
        'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
    _require(Path(archive).name == 'installer-materials.tar', 'BOOTSTRAP_TOKENLESS_ASSET_NAME_INVALID')
    _require(inputs.tag_commit == expected_commit)
    try:
        metadata = _metadata(_json(_canonical(inputs.release_metadata)), inputs.version)
    except AnonymousReleaseError:
        raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_INPUT_INVALID') from None
    subjects = [{'name': item['name'], 'sha256': item['digest'], 'size': item['size']}
                for item in metadata['assets']]
    asset = next((item for item in subjects if item['name'] == 'installer-materials.tar'), None)
    _require(asset is not None)
    request = dict(schemaVersion=1, mode='github-release', repository=_REPOSITORY,
        repositoryId='1327429673', ownerId='111261350', tag=inputs.version,
        tagCommit=inputs.tag_commit, tagObject=inputs.tag_object, expectedSubjects=subjects)
    _require(type(inputs.release_bundle) is bytes, 'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
    bundle = inputs.release_bundle
    envelope = _json(bundle)  # Budget and structural ambiguity checks precede any process.
    _require(type(envelope) is dict and type(envelope.get('dsseEnvelope')) is dict,
             'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
    observations = _closed_observations(inputs.observations)
    with _held_file(archive, 2 * 1024 * 1024 * 1024) as stream:
        before = _file_digest(stream)
        _require(before == (asset['sha256'], asset['size']))
        claim = _execute(material, bundle, request, scratch_parent=scratch_parent)
        # This exact payload has now been cryptographically verified. Its
        # additional signed databaseId is compared without changing the
        # existing offline verifier's request/claim format or Actions policy.
        try:
            statement = _json(base64.b64decode(envelope['dsseEnvelope']['payload'], validate=True))
        except (KeyError, TypeError, ValueError):
            raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_JSON_INVALID') from None
        _require(type(statement) is dict and type(statement.get('predicate')) is dict
            and type(statement['predicate'].get('databaseId')) is str
            and statement['predicate']['databaseId'] == str(metadata['id']))
        _closed_claim(claim, request)
        _require(_file_digest(stream) == before, 'BOOTSTRAP_TOKENLESS_FILE_CHANGED')
    observation = dict(schema='animemo.tokenless-stage0-verification/v1',
        authority='NON_AUTHORITATIVE_LOCAL_EVIDENCE', policy=POLICY, repository=_REPOSITORY,
        release_id=metadata['id'], tag=inputs.version, tag_object=inputs.tag_object,
        observed_commit=expected_commit, installer_sha256=before[0], installer_size=before[1],
        bundle_sha256='sha256:' + hashlib.sha256(bundle).hexdigest(),
        verifier_identity=material.profile.verifier_identity,
        trust_profile_identity=material.profile.identity, release_claim=claim,
        anonymous_observations=observations)
    observation['identity'] = 'sha256:' + hashlib.sha256(_canonical(observation)).hexdigest()
    return observation


def verify_release_locally(*, material, inputs, archive, expected_commit, scratch_parent=None):
    try:
        return _verify_release_locally(material=material, inputs=inputs, archive=archive,
            expected_commit=expected_commit, scratch_parent=scratch_parent)
    except TokenlessStage0Error:
        raise
    except OSError as error:
        failure = TokenlessStage0Error('BOOTSTRAP_TOKENLESS_FILE_UNAVAILABLE')
        failure.secondary_errors = getattr(error, 'secondary_errors', ())
        raise failure from None
    except (KeyError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_INPUT_INVALID') from None


def _closed_claim(claim, request):
    fields = {'schemaVersion', 'predicateType', 'immutable', 'repository', 'tag',
        'tagCommit', 'tagObject', 'draft', 'prerelease', 'signedAt', 'certificate',
        'assets', 'transportAssets'}
    _require(type(claim) is dict and set(claim) == fields
        and type(claim['schemaVersion']) is int and claim['schemaVersion'] == 1
        and claim['predicateType'] == 'https://in-toto.io/attestation/release/v0.2'
        and claim['immutable'] is True and claim['draft'] is False
        and claim['prerelease'] is ('-' in request['tag'])
        and all(claim[key] == request[key] for key in ('tag', 'tagObject', 'tagCommit'))
        and claim['repository'] == {'name': _REPOSITORY,
            'repositoryId': '1327429673', 'ownerId': '111261350'}
        and claim['certificate'] == {'identity': 'https://dotcom.releases.github.com',
            'issuerOrganization': 'GitHub, Inc.'})
    _require(type(claim['signedAt']) is str and re.fullmatch(
        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', claim['signedAt']))
    datetime.fromisoformat(claim['signedAt'])
    subjects = {value['name']: value for value in request['expectedSubjects']}
    _require(claim['assets'] == [subjects[name] for name in (
        'checksums.txt', 'deployment-contract.json', 'installer-materials.tar', 'release-manifest.json')])
    portable = subjects['animemo-' + request['tag'] + '-portable.tar']
    _require(claim['transportAssets'] == [{**portable,
        'role': 'PORTABLE_RELEASE_BUNDLE', 'authorityRole': 'TRANSPORT_ONLY'}])


def _closed_observations(observations):
    """Retain only the reader's public, bounded observation vocabulary."""
    from installer.anonymous_release_transport import API_VERSION
    _require(type(observations) is tuple and len(observations) <= 26,
             'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
    output = []
    for item in observations:
        fields = {'endpoint_category', 'http_status', 'body_bytes', 'body_sha256',
            'api_version', 'deprecation', 'sunset', 'authorization_header_present',
            'cookie_header_present', 'credential_helper_called'}
        _require(type(item) is dict and set(item) == fields
            and item['endpoint_category'] in {'RELEASE_METADATA', 'TAG_REFERENCE', 'ANNOTATED_TAG', 'RELEASE_PROOF'}
            and type(item['http_status']) is int and item['http_status'] == 200
            and type(item['body_bytes']) is int and 0 < item['body_bytes'] <= _MAX_JSON
            and type(item['body_sha256']) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', item['body_sha256'])
            and item['api_version'] == API_VERSION and all(item[key] is False for key in (
                'authorization_header_present', 'cookie_header_present', 'credential_helper_called')),
            'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
        for key in ('deprecation', 'sunset'):
            value = item[key]
            _require(value is None or type(value) is dict, 'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
            if value is None:
                continue
            kind = value.get('format')
            _require((kind == 'UNRECOGNIZED' and set(value) == {'format'})
                or (kind == 'BOOLEAN' and set(value) == {'format', 'value'} and type(value['value']) is bool)
                or (kind == 'UNIX_TIMESTAMP' and set(value) == {'format', 'value'}
                    and type(value['value']) is int and 0 <= value['value'] < 10**18)
                or (kind == 'HTTP_DATE' and set(value) == {'format', 'utc'}
                    and type(value['utc']) is str and re.fullmatch(
                        r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value['utc'])),
                'BOOTSTRAP_TOKENLESS_INPUT_INVALID')
        output.append(_json(_canonical(item)))
    return output


class VerifiedStage0Release:
    """One-use result issued exclusively by the fixed production trust path."""
    __slots__ = ('_record', '_used')

    def __init__(self, issuer, record):
        _require(issuer is _ISSUER, 'BOOTSTRAP_TOKENLESS_CAPABILITY_REQUIRED')
        self._record, self._used = _canonical(record), False

    def __reduce__(self):
        raise TypeError('Stage-0 capabilities cannot be serialized')

    def revoke(self):
        self._used = True

    def consume(self, *, version, release_commit, archive_digest, archive_size):
        _require(not self._used, 'BOOTSTRAP_TOKENLESS_CAPABILITY_CONSUMED')
        self._used = True
        record = _json(self._record)
        _require(record['tag'] == version and record['observed_commit'] == release_commit
            and record['installer_sha256'] == archive_digest and record['installer_size'] == archive_size)
        return record


_TEST_ONLY_ISSUER = object()


class TestOnlyStage0Release:
    """One-use DEV result; never a production VerifiedStage0Release."""
    __slots__ = ('_record', '_used')

    def __init__(self, issuer, record):
        _require(issuer is _TEST_ONLY_ISSUER, 'BOOTSTRAP_TOKENLESS_TEST_ONLY_CAPABILITY_REQUIRED')
        _require(type(record) is dict and record.get('authority') == 'NON_AUTHORITATIVE_LOCAL_EVIDENCE',
                 'BOOTSTRAP_TOKENLESS_TEST_ONLY_EVIDENCE_REQUIRED')
        self._record, self._used = _canonical(record), False

    def __reduce__(self):
        raise TypeError('TEST_ONLY Stage-0 results cannot be serialized')

    def revoke(self):
        self._used = True

    def consume(self, *, version, release_commit, archive_digest, archive_size):
        _require(not self._used, 'BOOTSTRAP_TOKENLESS_CAPABILITY_CONSUMED')
        self._used = True
        record = _json(self._record)
        _require(record.get('authority') == 'NON_AUTHORITATIVE_LOCAL_EVIDENCE'
            and record.get('tag') == version and record.get('observed_commit') == release_commit
            and record.get('installer_sha256') == archive_digest and record.get('installer_size') == archive_size,
            'BOOTSTRAP_TOKENLESS_TEST_ONLY_BINDING_MISMATCH')
        return record


def verify_for_test_only(*, material, inputs, archive, expected_commit, scratch_parent=None):
    """Explicit DEV entry always executes real local cryptographic verification."""
    record = verify_release_locally(material=material, inputs=inputs, archive=archive,
        expected_commit=expected_commit, scratch_parent=scratch_parent)
    return TestOnlyStage0Release(_TEST_ONLY_ISSUER, record)


def _production_host_allowed():
    return os.name == 'posix' and os.geteuid() == 0


def verify_for_production(*, version, release_commit, archive):
    """Fail closed without independently installed operator trust; no discovery."""
    from installer.anonymous_release_transport import AnonymousReleaseReader
    _require(_production_host_allowed(),
             'BOOTSTRAP_TOKENLESS_PRODUCTION_ROOT_REQUIRED')
    _safe_chain(OPERATOR_TRUST_ROOT, production=True)
    _require(OPERATOR_TRUST_ROOT.is_dir(), 'BOOTSTRAP_TOKENLESS_INDEPENDENT_TRUST_REQUIRED')
    with _closing_stack() as holds:
        holds.enter_context(_held_file(archive, 2 * 1024 * 1024 * 1024, production=True))
        for name, limit in (
            ('trust-profile.json', 64 * 1024), ('offline-release-verifier', 256 * 1024 * 1024),
            ('github-trusted-root.jsonl', _MAX_JSON), ('sigstore-trusted-root.jsonl', _MAX_JSON),
            ('github-tuf-root.json', _MAX_JSON), ('sigstore-tuf-root.json', _MAX_JSON),
        ):
            holds.enter_context(_held_file(OPERATOR_TRUST_ROOT / name, limit, production=True))
        material = PretrustedTrustMaterial.load(OPERATOR_TRUST_ROOT)
        inputs = AnonymousReleaseReader().fetch(version)
        record = verify_release_locally(material=material, inputs=inputs, archive=archive,
            expected_commit=release_commit)
        _require(PretrustedTrustMaterial.load(OPERATOR_TRUST_ROOT) == material,
                 'BOOTSTRAP_TOKENLESS_TRUST_CHANGED')
    return VerifiedStage0Release(_ISSUER, record)
