from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cramjam import DecompressionError, snappy
from packaging.version import Version

from release.contract import (
    API_REPOSITORY,
    REPOSITORY,
    WEB_REPOSITORY,
    deployment_contract_digest,
    validate_deployment_contract,
    validate_manifest,
)
from release.materials import reject_duplicate_json_keys
from release.publication_evidence import OWNER_ID, REPOSITORY_ID

from . import __version__
from .authority import (
    AttestationEvidence,
    AttestationExecutionObservation,
    AttestationExecutionReceipt,
    AuthorityEvidence,
    ReleaseAssetEvidence,
    ReleaseAuthorityVerifier,
    VerifiedReleaseMaterials,
)
from .commands import CommandRunner
from .errors import CommandFailed, RequestRejected, StateError
from .protocol import CHANNELS, RELEASE_VERSION
from .state import _absolute, _ensure_private_directory
from .transport import (
    AnonymousGitHubTransportSource,
    ExplicitTransportPolicy,
    GitHubAssetPlan,
    GitHubTransportSource,
    OfficialMirrorTransportSource,
    TransportObjectPlan,
    TransportRequest,
    TransportSourceId,
)

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
ATTESTATION_BUNDLE_HOST = "tmaproduction.blob.core.windows.net"
MAX_GITHUB_JSON_BYTES = 8 * 1024 * 1024
EXPECTED_RELEASE_ASSETS = {
    "checksums.txt",
    "deployment-contract.json",
    "installer-materials.tar",
    "release-manifest.json",
}
MAX_RELEASE_ASSET_BYTES = 512 * 1024 * 1024
MAX_RELEASE_TRANSPORT_BYTES = 1024 * 1024 * 1024


def _expected_release_asset_names(version: str) -> set[str]:
    expected = set(EXPECTED_RELEASE_ASSETS)
    if Version(version.removeprefix("v")).release >= (1, 1, 0):
        expected.add(f"animemo-{version}-portable.tar")
    return expected


@dataclass(frozen=True)
class ReleaseAssetInventoryEntry:
    name: str
    state: str
    size: int
    asset_id: int | None = None
    digest: str | None = None


@dataclass(frozen=True)
class ReleaseAssetInventory:
    version: str
    prerelease: bool
    assets: tuple[ReleaseAssetInventoryEntry, ...]
    release_id: int | None = None

    @property
    def github_asset_plans(self):
        by_name = {item.name: item for item in self.assets}
        return tuple(GitHubAssetPlan(name, by_name[name].asset_id, by_name[name].digest)
                     for name in sorted(EXPECTED_RELEASE_ASSETS))

    @property
    def transport_object_plans(self) -> tuple[TransportObjectPlan, ...]:
        by_name = {item.name: item for item in self.assets}
        return tuple(
            TransportObjectPlan(name, by_name[name].size)
            for name in (
                "checksums.txt",
                "deployment-contract.json",
                "installer-materials.tar",
                "release-manifest.json",
            )
        )


def _validate_release_asset_inventory(
    metadata: object,
    version: str,
    *,
    max_object_bytes: int = MAX_RELEASE_ASSET_BYTES,
    max_total_bytes: int = MAX_RELEASE_TRANSPORT_BYTES,
    require_identity: bool = False,
) -> ReleaseAssetInventory:
    if (
        type(metadata) is not dict
        or metadata.get("tag_name") != version
        or metadata.get("draft") is not False
        or type(metadata.get("prerelease")) is not bool
    ):
        raise RequestRejected("Exact GitHub release metadata is invalid")
    if require_identity and (type(metadata.get("id")) is not int or metadata["id"] <= 0
                             or metadata.get("immutable") is not True):
        raise RequestRejected("Exact immutable public Release ID is required")
    parsed_version = Version(version.removeprefix("v"))
    if metadata["prerelease"] is not parsed_version.is_prerelease:
        raise RequestRejected("Exact GitHub release metadata channel is invalid")
    raw_assets = metadata.get("assets")
    if type(raw_assets) is not list:
        raise RequestRejected("GitHub release assets differ from the release contract")
    entries: list[ReleaseAssetInventoryEntry] = []
    for item in raw_assets:
        if (
            type(item) is not dict
            or not isinstance(item.get("name"), str)
            or item.get("state") != "uploaded"
            or type(item.get("size")) is not int
            or item["size"] <= 0
            or item["size"] > max_object_bytes
        ):
            raise RequestRejected("GitHub release assets differ from the release contract")
        if require_identity and (type(item.get("id")) is not int or item["id"] <= 0
                or type(item.get("digest")) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", item["digest"]) is None):
            raise RequestRejected("Exact public asset ID and digest are required")
        entries.append(
            ReleaseAssetInventoryEntry(
                name=item["name"],
                state=item["state"],
                size=item["size"],
                asset_id=item.get("id"),
                digest=item.get("digest"),
            )
        )
    names = [item.name for item in entries]
    expected_names = _expected_release_asset_names(version)
    if (
        len(names) != len(expected_names)
        or len(names) != len(set(names))
        or set(names) != expected_names
    ):
        raise RequestRejected("GitHub release assets differ from the release contract")
    by_name = {item.name: item for item in entries}
    transport_total = sum(by_name[name].size for name in EXPECTED_RELEASE_ASSETS)
    if transport_total > max_total_bytes:
        raise RequestRejected("GitHub release transport assets exceed the resource limit")
    return ReleaseAssetInventory(
        version=version,
        prerelease=metadata["prerelease"],
        assets=tuple(sorted(entries, key=lambda item: item.name)),
        release_id=metadata.get("id"),
    )


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
ATTESTATION_BUNDLE_PATH = re.compile(
    r"^/attestations/(?P<repository_id>[1-9][0-9]*)/"
    r"[0-9]{4}/[0-9]{2}/[0-9]{2}/[1-9][0-9]*\.json\.sn$"
)


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class GitHubPublicRest:
    _UNRESOLVED = object()

    def __init__(self, *, runner=None, opener=None):
        self.runner = runner or CommandRunner()
        self.opener = opener or build_opener(_RejectRedirects())
        self._token: str | None | object = self._UNRESOLVED

    def configured_token(self) -> str | None:
        if self._token is not self._UNRESOLVED:
            return self._token
        try:
            result = self.runner.run(
                ["/usr/bin/gh", "auth", "token", "--hostname", "github.com"],
                timeout=10,
            )
        except CommandFailed:
            self._token = None
            return None
        token = result.stdout.strip()
        self._token = (
            token
            if token and not any(character.isspace() for character in token)
            else None
        )
        return self._token

    def _request_json(self, path: str, *, label: str, token: str | None):
        if not path.startswith("/") or "://" in path:
            raise RequestRejected(f"{label} path is invalid")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AniMemo-Updater",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"{GITHUB_API_ROOT}{path}",
            headers=headers,
            method="GET",
        )
        return self._open_json(request, label=label)

    def _open_json(self, request: Request, *, label: str):
        encoded, _ = self._open_bytes(request, label=label)
        return self._decode_json(encoded, label=label)

    def _open_bytes(self, request: Request, *, label: str) -> tuple[bytes, str]:
        try:
            with self.opener.open(request, timeout=30) as response:
                encoded = response.read(MAX_GITHUB_JSON_BYTES + 1)
                content_type = (
                    str(
                        getattr(response, "headers", {}).get(
                            "Content-Type",
                            "application/json",
                        )
                    )
                    .partition(";")[0]
                    .strip()
                    .lower()
                )
        except HTTPError:
            raise
        except (OSError, URLError) as error:
            raise RequestRejected(f"{label} is unavailable") from error
        if len(encoded) > MAX_GITHUB_JSON_BYTES:
            raise RequestRejected(f"{label} response is too large")
        return encoded, content_type

    @staticmethod
    def _decode_json(encoded: bytes, *, label: str):
        try:
            return json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestRejected(f"{label} returned invalid JSON") from error

    def get_attestation_bundle(self, url: str, *, repository_id: int):
        if (
            type(repository_id) is not int
            or repository_id <= 0
            or not isinstance(url, str)
        ):
            raise RequestRejected("GitHub artifact attestation bundle URL is invalid")
        parsed = urlsplit(url)
        path_match = ATTESTATION_BUNDLE_PATH.fullmatch(parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.netloc != ATTESTATION_BUNDLE_HOST
            or parsed.fragment
            or not parsed.query
            or path_match is None
            or path_match.group("repository_id") != str(repository_id)
        ):
            raise RequestRejected("GitHub artifact attestation bundle URL is invalid")
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AniMemo-Updater",
            },
            method="GET",
        )
        try:
            encoded, content_type = self._open_bytes(
                request,
                label="GitHub artifact attestation bundle",
            )
        except HTTPError as error:
            raise RequestRejected(
                f"GitHub artifact attestation bundle returned HTTP {error.code}"
            ) from error
        if content_type == "application/x-snappy":
            try:
                if snappy.decompress_raw_len(encoded) > MAX_GITHUB_JSON_BYTES:
                    raise RequestRejected(
                        "GitHub artifact attestation bundle response is too large"
                    )
                encoded = bytes(snappy.decompress_raw(encoded))
            except DecompressionError as error:
                raise RequestRejected(
                    "GitHub artifact attestation bundle is invalid Snappy data"
                ) from error
        elif content_type != "application/json":
            raise RequestRejected(
                "GitHub artifact attestation bundle content type is invalid"
            )
        return self._decode_json(
            encoded,
            label="GitHub artifact attestation bundle",
        )

    def get_json(self, path: str, *, label: str):
        try:
            return self._request_json(path, label=label, token=None)
        except HTTPError as anonymous_error:
            if anonymous_error.code not in {401, 403, 429}:
                raise RequestRejected(
                    f"{label} returned HTTP {anonymous_error.code}"
                ) from anonymous_error
            token = self.configured_token()
            if token is None:
                raise RequestRejected(
                    f"{label} returned HTTP {anonymous_error.code}"
                ) from anonymous_error
            try:
                return self._request_json(path, label=label, token=token)
            except HTTPError as authenticated_error:
                raise RequestRejected(
                    f"{label} returned HTTP {authenticated_error.code}"
                ) from authenticated_error


class AnonymousGitHubRest(GitHubPublicRest):
    """Installer reads use the same credential-free, process-supervised transport."""
    configured_token = None
    acquisition_mode = 'ANONYMOUS_INSTALLER'

    def __init__(self, *, http_client=None, private_root=None):
        self._client = http_client
        self._private_root = Path(private_root or tempfile.gettempdir())

    def _supervised_read(self, selection):
        from bootstrap_kit.http_protocol import HttpFailure
        from bootstrap_kit.http_supervisor import SupervisedAnonymousHttp
        client = self._client or SupervisedAnonymousHttp(private_root=self._private_root)
        try:
            with client.fetch(selection, deadline=time.monotonic() + 30) as result:
                return result.read_bytes(maximum=MAX_GITHUB_JSON_BYTES), result.headers.get('content-type', '').partition(';')[0].strip().lower()
        except HttpFailure as error:
            failure = RequestRejected(error.code)
            failure.code, failure.http_status = error.code, error.http_status
            failure.secondary_errors = error.secondary_errors
            raise failure from error

    def get_json(self, path: str, *, label: str):
        from bootstrap_kit.http_protocol import HttpSelection
        raw, content_type = self._supervised_read(HttpSelection.github_json(path))
        if content_type not in {'application/json', 'application/vnd.github+json'}:
            raise RequestRejected('BOOTSTRAP_ANONYMOUS_CONTENT_TYPE_INVALID')
        value = self._decode_json(raw, label=label)
        if path.startswith(f'/repos/{REPOSITORY}/releases/tags/'):
            from installer.anonymous_release_transport import _metadata

            from .archive_handoff import ArchiveHandoffError
            version = path.rsplit('/', 1)[-1]
            metadata = _metadata(value, version)
            # Repository identity comes from the actual response URL, not a run ledger.
            expected_url = f'{GITHUB_API_ROOT}/repos/{REPOSITORY}/releases/{metadata["id"]}'
            if value.get('url') != expected_url:
                raise ArchiveHandoffError('SOURCE_MISMATCH')
            self.release_observation = {'repository': value['url'].split('/repos/', 1)[1].rsplit('/releases/', 1)[0],
                'owner': value['url'].split('/repos/', 1)[1].split('/', 1)[0],
                'request_path': path, 'response_sha256': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                'response_size': len(raw), 'observed_at': datetime.now(timezone.utc).isoformat(),
                'release': metadata}
        return value

    @staticmethod
    def _decode_json(encoded: bytes, *, label: str):
        try:
            return json.loads(encoded, object_pairs_hook=reject_duplicate_json_keys,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeError, ValueError, RecursionError):
            raise RequestRejected('BOOTSTRAP_ANONYMOUS_JSON_INVALID') from None

    def _open_bytes(self, request: Request, *, label: str):
        # Used only by the inherited, strictly closed Actions blob parser.
        from bootstrap_kit.http_protocol import HttpSelection
        parsed = urlsplit(request.full_url)
        if (parsed.scheme != 'https' or parsed.netloc != ATTESTATION_BUNDLE_HOST or parsed.fragment
            or request.get_method() != 'GET' or request.data is not None
            or any(key.lower() in {'authorization', 'cookie', 'proxy-authorization'}
                   for key, _ in request.header_items())):
            raise RequestRejected('BOOTSTRAP_ANONYMOUS_BUNDLE_URL_INVALID')
        return self._supervised_read(HttpSelection.actions_bundle(parsed.path, parsed.query))


class GitHubReleaseSource:
    def __init__(
        self,
        cache_root: Path,
        *,
        runner=None,
        rest=None,
        cache_seconds: int = 300,
        policy: ExplicitTransportPolicy | None = None,
        transports: dict[TransportSourceId, object] | None = None,
        actions_verifier=None,
    ):
        self.cache_root = _absolute(cache_root)
        self.runner = runner or CommandRunner()
        self.rest = rest or GitHubPublicRest(runner=self.runner)
        self.transport_policy = policy or ExplicitTransportPolicy.github()
        if type(self.transport_policy) is not ExplicitTransportPolicy:
            raise RequestRejected("Release transport policy is invalid")
        self.anonymous_installation = getattr(self.rest, 'acquisition_mode', None) == 'ANONYMOUS_INSTALLER'
        self._actions_verifier = actions_verifier
        available = transports or {
            TransportSourceId.GITHUB: (AnonymousGitHubTransportSource(
                http_client=getattr(self.rest, '_client', None),
                private_root=getattr(self.rest, '_private_root', None)) if self.anonymous_installation else GitHubTransportSource(
                runner=self.runner, credential_provider=getattr(self.rest, "configured_token", None))),
            TransportSourceId.OFFICIAL_MIRROR: OfficialMirrorTransportSource(),
        }
        selected = available.get(self.transport_policy.source)
        if selected is None or getattr(selected, "transport_id", None) is not self.transport_policy.source:
            raise RequestRejected("Selected release transport is unavailable")
        self.transport_source = selected
        self.cache_seconds = cache_seconds
        self._release_cache: tuple[float, list[dict[str, object]]] | None = None
        self._verified_cache: dict[str, tuple[float, VerifiedReleaseMaterials]] = {}

    @staticmethod
    def _anonymous_gh_environment(root: Path) -> dict[str, str]:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = root / "home"
        temporary = root / "tmp"
        gh_config = root / "gh"
        docker_config = root / "docker"
        for directory in (home, temporary, gh_config, docker_config):
            directory.mkdir(mode=0o700)
        return {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "GH_CONFIG_DIR": str(gh_config),
            "DOCKER_CONFIG": str(docker_config),
            "GH_PROMPT_DISABLED": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def _list_all(self, *, refresh: bool) -> list[dict[str, object]]:
        now = time.monotonic()
        if (
            not refresh
            and self._release_cache
            and now - self._release_cache[0] < self.cache_seconds
        ):
            return self._release_cache[1]
        payload = []
        for page in range(1, 101):
            batch = self.rest.get_json(
                f"/repos/{REPOSITORY}/releases?per_page=100&page={page}",
                label="GitHub release discovery",
            )
            if not isinstance(batch, list):
                raise RequestRejected(
                    "GitHub release discovery returned invalid metadata"
                )
            if any(
                not isinstance(item, dict)
                or not isinstance(item.get("tag_name"), str)
                or not isinstance(item.get("draft"), bool)
                or not isinstance(item.get("prerelease"), bool)
                for item in batch
            ):
                raise RequestRejected(
                    "GitHub release discovery returned invalid metadata"
                )
            payload.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise RequestRejected(
                "GitHub release discovery exceeded the pagination limit"
            )
        releases = [
            item
            for item in payload
            if not item.get("draft")
            and RELEASE_VERSION.fullmatch(str(item.get("tag_name", "")))
        ]
        self._release_cache = (now, releases)
        return releases

    def list_releases(
        self, channel: str, *, refresh: bool = False
    ) -> list[dict[str, object]]:
        if channel not in CHANNELS:
            raise RequestRejected("Invalid release channel")
        accepted = {channel}
        result = []
        for item in self._list_all(refresh=refresh):
            tag = str(item["tag_name"])
            parsed = Version(tag.removeprefix("v"))
            prerelease_channels = {"a": "alpha", "b": "beta", "rc": "rc"}
            item_channel = (
                "stable"
                if not parsed.is_prerelease
                else prerelease_channels.get(str(parsed.pre[0]), str(parsed.pre[0]))
            )
            metadata_matches = item.get("prerelease") is (item_channel != "stable")
            if item_channel in accepted and metadata_matches:
                result.append(
                    {
                        "version": tag,
                        "channel": item_channel,
                        "publishedAt": item.get("published_at"),
                    }
                )
        return sorted(
            result,
            key=lambda item: Version(item["version"].removeprefix("v")),
            reverse=True,
        )

    @staticmethod
    def _verify_checksum(root: Path) -> None:
        try:
            lines = (root / "checksums.txt").read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise RequestRejected("Release checksum asset is unavailable") from error
        expected = {}
        for line in lines:
            digest, separator, name = line.partition("  ")
            if (
                separator != "  "
                or len(digest) != 64
                or name
                not in {
                    "release-manifest.json",
                    "deployment-contract.json",
                    "installer-materials.tar",
                }
            ):
                raise RequestRejected(
                    "Release checksums contain an unexpected artifact"
                )
            if name in expected:
                raise RequestRejected("Release checksums contain a duplicate artifact")
            expected[name] = digest
        if set(expected) != {
            "release-manifest.json",
            "deployment-contract.json",
            "installer-materials.tar",
        }:
            raise RequestRejected(
                "Release checksums do not cover every release contract asset"
            )
        for name, digest in expected.items():
            try:
                actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
            except OSError as error:
                raise RequestRejected(
                    f"Release contract asset is unavailable: {name}"
                ) from error
            if actual != digest:
                raise RequestRejected(f"Release contract checksum mismatch: {name}")

    def _verify_release_tag(self, version: str, expected_commit: str) -> None:
        payload = self.rest.get_json(
            f"/repos/{REPOSITORY}/git/ref/tags/{version}",
            label="GitHub release tag",
        )
        seen = set()
        for _ in range(8):
            if not isinstance(payload, dict) or not isinstance(
                payload.get("object"), dict
            ):
                raise RequestRejected("GitHub release tag is invalid")
            target = payload["object"]
            object_type = target.get("type")
            sha = target.get("sha")
            if not isinstance(sha, str) or not GIT_SHA.fullmatch(sha) or sha in seen:
                raise RequestRejected("GitHub release tag is invalid")
            if object_type == "commit":
                if sha != expected_commit:
                    raise RequestRejected(
                        "GitHub release tag and manifest commit differ"
                    )
                return
            if object_type != "tag":
                raise RequestRejected("GitHub release tag does not resolve to a commit")
            seen.add(sha)
            payload = self.rest.get_json(
                f"/repos/{REPOSITORY}/git/tags/{sha}",
                label="GitHub annotated tag",
            )
        raise RequestRejected("GitHub release tag exceeds the peel limit")

    def _write_attestation_bundle(self, digest: str, path: Path) -> None:
        payload = self.rest.get_json(
            f"/repos/{REPOSITORY}/attestations/{digest}",
            label="GitHub artifact attestations",
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("attestations"), list
        ):
            raise RequestRejected(
                "GitHub artifact attestations returned invalid metadata"
            )
        bundles = []
        for item in payload["attestations"]:
            if not isinstance(item, dict):
                raise RequestRejected(
                    "GitHub artifact attestations returned an invalid bundle"
                )
            bundle = item.get("bundle")
            if bundle is None:
                bundle = self.rest.get_attestation_bundle(
                    item.get("bundle_url"),
                    repository_id=item.get("repository_id"),
                )
            if not isinstance(bundle, dict):
                raise RequestRejected(
                    "GitHub artifact attestations returned an invalid bundle"
                )
            bundles.append(bundle)
        if not bundles:
            raise RequestRejected("Required artifact attestation is unavailable")
        if self.anonymous_installation:
            # This unsigned routing hint selects a proof domain only. The
            # selected envelope still requires the fixed Actions verifier;
            # platform Release evidence is verified separately.
            candidates = []
            for bundle in bundles:
                try:
                    encoded = bundle['dsseEnvelope']['payload']
                    if type(encoded) is not str or len(encoded) > MAX_GITHUB_JSON_BYTES:
                        raise ValueError('invalid bounded payload')
                    statement = json.loads(base64.b64decode(encoded, validate=True),
                        object_pairs_hook=reject_duplicate_json_keys)
                    if type(statement) is not dict:
                        raise ValueError('invalid statement')
                    predicate = statement.get('predicateType')
                    if predicate == 'https://slsa.dev/provenance/v1':
                        candidates.append(bundle)
                    elif predicate != 'https://in-toto.io/attestation/release/v0.2':
                        raise ValueError('unsupported proof domain')
                except (KeyError, TypeError, ValueError, RecursionError):
                    raise RequestRejected('BOOTSTRAP_ANONYMOUS_ACTIONS_BUNDLE_INVALID') from None
            if len(candidates) != 1:
                raise RequestRejected('BOOTSTRAP_ANONYMOUS_ACTIONS_BUNDLE_AMBIGUOUS')
            bundles = candidates
        try:
            path.write_text(
                "".join(
                    json.dumps(bundle, separators=(",", ":"), sort_keys=True) + "\n"
                    for bundle in bundles
                ),
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
        except OSError as error:
            raise RequestRejected(
                "Artifact attestation bundle cannot be staged"
            ) from error

    def _verify_anonymous_actions(self, bundle, *, evidence_name, expected_name,
                                  digest, workflow, source_commit):
        from installer.tokenless_stage0 import (
            OPERATOR_TRUST_ROOT,
            TokenlessActionsVerifier,
            TokenlessStage0Error,
            _production_host_allowed,
            _safe_chain,
        )
        from updater.offline import PretrustedTrustMaterial
        try:
            verifier = self._actions_verifier
            if verifier is None:
                if not _production_host_allowed():
                    raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_INDEPENDENT_TRUST_REQUIRED')
                _safe_chain(OPERATOR_TRUST_ROOT, production=True)
                verifier = TokenlessActionsVerifier(PretrustedTrustMaterial.load(OPERATOR_TRUST_ROOT))
            if type(verifier) is not TokenlessActionsVerifier:
                raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_TRUST_REQUIRED')
            with bundle.open('rb') as stream:
                raw = stream.read(MAX_GITHUB_JSON_BYTES + 1)
            if len(raw) > MAX_GITHUB_JSON_BYTES:
                raise TokenlessStage0Error('BOOTSTRAP_TOKENLESS_INPUT_INVALID')
            claim = verifier.verify(bundle=raw, evidence_name=evidence_name, subject_name=expected_name,
                subject_sha256=digest, workflow=workflow, source_commit=source_commit)
            return claim.source_commit, claim.signer_digest
        except TokenlessStage0Error as error:
            failure = RequestRejected(error.code)
            failure.code, failure.secondary_errors = error.code, error.secondary_errors
            for name in ('cleanup_failed', 'cleanup_uncertain', '_owned_process'):
                if hasattr(error, name):
                    setattr(failure, name, getattr(error, name))
            raise failure from None

    @staticmethod
    def _verify_attestation_result(
        output: str, expected_name: str, digest: str, *,
        expected_workflow: str, expected_source_commit: str | None,
    ) -> tuple[str, str]:
        """Consume fixed gh 2.97 JSON after its checked subprocess succeeds.

        Sigstore's certificate summary flattens the X.509 extensions. Accept
        that one format, and bind the summary again to the request sent to gh.
        This parser is not a cryptographic verifier or a Release capability.
        """
        if (
            type(output) is not str or not output or len(output) > MAX_GITHUB_JSON_BYTES
            or expected_workflow not in {
                ".github/workflows/release.yml", ".github/workflows/promote-release.yml"
            }
            or expected_source_commit is not None
            and (type(expected_source_commit) is not str or GIT_SHA.fullmatch(expected_source_commit) is None)
        ):
            raise RequestRejected("Artifact attestation output contract is invalid")
        try:
            payload = json.loads(output, object_pairs_hook=reject_duplicate_json_keys)
        except ValueError as error:
            raise RequestRejected(
                "Artifact attestation verification returned invalid JSON"
            ) from error
        expected_digest = digest.removeprefix("sha256:")
        if not isinstance(payload, list) or not payload:
            raise RequestRejected(
                "Artifact attestation verification returned no result"
            )
        matches: list[tuple[str, str]] = []
        workflow_identity = f"https://github.com/{REPOSITORY}/{expected_workflow}@refs/heads/main"
        expected_certificate = {
            "sourceRepositoryURI": f"https://github.com/{REPOSITORY}",
            "sourceRepositoryIdentifier": REPOSITORY_ID,
            "sourceRepositoryOwnerURI": f"https://github.com/{REPOSITORY.split('/')[0]}",
            "sourceRepositoryOwnerIdentifier": OWNER_ID,
            "sourceRepositoryRef": "refs/heads/main",
            "issuer": "https://token.actions.githubusercontent.com",
            "subjectAlternativeName": workflow_identity,
            "buildSignerURI": workflow_identity,
            "buildConfigURI": workflow_identity,
        }
        for item in payload:
            if not isinstance(item, dict):
                continue
            verification = item.get("verificationResult")
            statement = (
                verification.get("statement")
                if isinstance(verification, dict)
                else None
            )
            subjects = statement.get("subject") if isinstance(statement, dict) else None
            if (not isinstance(subjects, list)
                    or statement.get("predicateType") != "https://slsa.dev/provenance/v1"):
                continue
            for subject in subjects:
                subject_digest = (
                    subject.get("digest") if isinstance(subject, dict) else None
                )
                if (
                    isinstance(subject_digest, dict)
                    and subject.get("name") == expected_name
                    and subject_digest.get("sha256") == expected_digest
                ):
                    signature = verification.get("signature")
                    certificate = (
                        signature.get("certificate")
                        if isinstance(signature, dict)
                        else None
                    )
                    if (type(certificate) is not dict or "extensions" in certificate
                            or any(certificate.get(key) != value for key, value in expected_certificate.items())):
                        continue
                    source_commit = certificate.get("sourceRepositoryDigest")
                    signer_digest = certificate.get("buildSignerDigest")
                    build_config_digest = certificate.get("buildConfigDigest")
                    if (
                        not isinstance(source_commit, str)
                        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
                        or not isinstance(signer_digest, str)
                        or re.fullmatch(r"[0-9a-f]{40}", signer_digest) is None
                        or build_config_digest != signer_digest
                        or source_commit != signer_digest
                        or expected_source_commit is not None
                        and source_commit != expected_source_commit
                    ):
                        continue
                    matches.append((source_commit, signer_digest))
        if len(matches) == 1:
            return matches[0]
        raise RequestRejected(
            "Artifact attestation subject or execution certificate does not match "
            "the release authority"
        )

    def fetch_verified_materials(
        self,
        version: str,
        *,
        updater_version: str = __version__,
        refresh: bool = False,
    ) -> VerifiedReleaseMaterials:
        if not isinstance(version, str) or not RELEASE_VERSION.fullmatch(version):
            raise RequestRejected("Invalid immutable release version")
        cached = self._verified_cache.get(version)
        if not refresh and cached and time.monotonic() - cached[0] < self.cache_seconds:
            validate_manifest(cached[1].manifest, updater_version=updater_version)
            for identity in cached[1].verified.files:
                cached[1].material(identity.path)
            return cached[1]
        with self._verified_release_transaction(version, updater_version=updater_version) as verified:
            pass
        self._verified_cache[version] = (time.monotonic(), verified)
        return verified

    @contextmanager
    def open_verified_release(self, version, *, updater_version=__version__, evidence_parent=None):
        """Fresh anonymous acquisition with an original-archive lifetime owner.

        The legacy cache contains extracted members only and is never used here.
        """
        if not self.anonymous_installation:
            raise RequestRejected('BOOTSTRAP_ARCHIVE_ANONYMOUS_SOURCE_REQUIRED')
        with self._verified_release_transaction(version, updater_version=updater_version,
                raw_archive=True, evidence_parent=evidence_parent) as transaction:
            yield transaction

    @contextmanager
    def _verified_release_transaction(self, version, *, updater_version,
                                      raw_archive=False, evidence_parent=None):
        from .archive_handoff import release_workspace
        if not isinstance(version, str) or not RELEASE_VERSION.fullmatch(version):
            raise RequestRejected('Invalid immutable release version')
        try:
            _ensure_private_directory(self.cache_root, self.cache_root)
        except StateError as error:
            raise RequestRejected("Release cache directory is unavailable") from error
        metadata = self.rest.get_json(
            f"/repos/{REPOSITORY}/releases/tags/{version}",
            label="Exact GitHub release metadata",
        )
        inventory = _validate_release_asset_inventory(metadata, version, require_identity=self.anonymous_installation)
        with release_workspace(self, self.cache_root) as transaction:
            staging = transaction.root
            transport_request = TransportRequest.release_bundle(
                version.removeprefix("v"),
                object_plans=inventory.transport_object_plans,
                github_release_id=inventory.release_id if self.anonymous_installation else None,
                github_assets=inventory.github_asset_plans if self.anonymous_installation else (),
            )
            acquired = self.transport_source.acquire(transport_request, staging)
            if (
                acquired.receipt.transport_id is not self.transport_policy.source
                or acquired.receipt.request_identity != transport_request.identity
                or tuple(
                    (item.logical_name, item.size) for item in acquired.objects
                )
                != tuple(
                    (item.logical_name, item.expected_size)
                    for item in inventory.transport_object_plans
                )
            ):
                raise RequestRejected("Release transport receipt and policy differ")
            if raw_archive:
                transaction._hold(acquired, transport_request, inventory,
                    getattr(self.rest, 'release_observation', None))
                if evidence_parent is not None:
                    transaction.preserve(evidence_parent)
            destination = acquired.root
            for name in EXPECTED_RELEASE_ASSETS:
                acquired.material(name)
            environment = (None if self.anonymous_installation else
                self._anonymous_gh_environment(staging / ".authority-runtime"))
            self._verify_checksum(destination)
            assets = [
                destination / name
                for name in (
                    "release-manifest.json",
                    "deployment-contract.json",
                    "installer-materials.tar",
                    "checksums.txt",
                )
            ]
            if any(
                path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
                for path in assets
            ):
                raise RequestRejected("Release assets must be private regular files")
            try:
                manifest = json.loads(
                    (destination / "release-manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise RequestRejected("Release manifest is unreadable") from error
            validate_manifest(manifest, updater_version=updater_version)
            try:
                deployment_contract = json.loads(
                    (destination / "deployment-contract.json").read_text(
                        encoding="utf-8"
                    )
                )
                validate_deployment_contract(
                    deployment_contract,
                    installer_materials=destination / "installer-materials.tar",
                )
            except (OSError, json.JSONDecodeError, ValueError) as error:
                raise RequestRejected(
                    "Deployment contract is unreadable or invalid"
                ) from error
            if (
                deployment_contract_digest(deployment_contract)
                != manifest["deployment"]["contractSha256"]
                or deployment_contract["files"] != manifest["deployment"]["files"]
                or deployment_contract["profile"] != manifest["deployment"]["profile"]
                or {
                    key: deployment_contract["archive"][key]
                    for key in ("name", "sha256", "format")
                }
                != manifest["deployment"]["installerMaterials"]
            ):
                raise RequestRejected(
                    "Deployment contract differs from the release manifest"
                )
            if manifest["release"]["version"] != version:
                raise RequestRejected("Release tag and manifest version differ")
            expected_prerelease = manifest["release"]["channel"] != "stable"
            if metadata["prerelease"] != expected_prerelease:
                raise RequestRejected(
                    "GitHub release metadata and manifest channel differ"
                )
            commit = manifest["release"]["commit"]
            logical_provenance_commit = manifest["provenance"]["sourceCommit"]
            provenance_commit = (
                None
                if manifest["release"]["channel"] == "stable"
                else logical_provenance_commit
            )
            self._verify_release_tag(version, commit)
            subjects = [
                (
                    f"oci://{API_REPOSITORY}@{manifest['images']['api']['digest']}",
                    API_REPOSITORY,
                    manifest["images"]["api"]["digest"],
                    ".github/workflows/release.yml",
                    commit,
                ),
                (
                    f"oci://{WEB_REPOSITORY}@{manifest['images']['web']['digest']}",
                    WEB_REPOSITORY,
                    manifest["images"]["web"]["digest"],
                    ".github/workflows/release.yml",
                    commit,
                ),
                (
                    str(destination / "release-manifest.json"),
                    "release-manifest.json",
                    "sha256:"
                    + hashlib.sha256(
                        (destination / "release-manifest.json").read_bytes()
                    ).hexdigest(),
                    manifest["provenance"]["workflow"],
                    provenance_commit,
                ),
                (
                    str(destination / "deployment-contract.json"),
                    "deployment-contract.json",
                    "sha256:"
                    + hashlib.sha256(
                        (destination / "deployment-contract.json").read_bytes()
                    ).hexdigest(),
                    manifest["provenance"]["workflow"],
                    provenance_commit,
                ),
                (
                    str(destination / "installer-materials.tar"),
                    "installer-materials.tar",
                    "sha256:"
                    + hashlib.sha256(
                        (destination / "installer-materials.tar").read_bytes()
                    ).hexdigest(),
                    manifest["provenance"]["workflow"],
                    provenance_commit,
                ),
            ]
            attestation_evidence: list[AttestationEvidence] = []
            execution_observations: list[AttestationExecutionObservation] = []
            for index, (
                subject,
                expected_name,
                digest,
                workflow,
                source_commit,
            ) in enumerate(subjects):
                bundle = destination / f"attestation-{index}.jsonl"
                self._write_attestation_bundle(digest, bundle)
                if raw_archive and evidence_parent is not None:
                    from bootstrap_kit.safe_files import exclusive_file, read_bounded
                    with exclusive_file(transaction._evidence_root / f'actions-{index}.jsonl') as output:
                        output.write(read_bounded(bundle, MAX_GITHUB_JSON_BYTES))
                if self.anonymous_installation:
                    observed_source_commit, observed_signer_digest = self._verify_anonymous_actions(
                        bundle, evidence_name=('api-image', 'web-image', 'release-manifest',
                            'deployment-contract', 'installer-materials')[index], expected_name=expected_name,
                        digest=digest, workflow=workflow, source_commit=source_commit)
                else:
                    verification_command = [
                            "/usr/bin/gh",
                            "attestation",
                            "verify",
                            subject,
                            "--bundle",
                            str(bundle),
                            "--repo",
                            REPOSITORY,
                            "--cert-identity",
                            f"https://github.com/{REPOSITORY}/{workflow}@refs/heads/main",
                            "--cert-oidc-issuer",
                            "https://token.actions.githubusercontent.com",
                            "--source-ref",
                            "refs/heads/main",
                            "--predicate-type",
                            "https://slsa.dev/provenance/v1",
                            "--format",
                            "json",
                        ]
                    if source_commit is not None:
                        predicate_index = verification_command.index("--predicate-type")
                        verification_command[predicate_index:predicate_index] = [
                            "--source-digest",
                            source_commit,
                            "--signer-digest",
                            source_commit,
                        ]
                    result = self.runner.run(
                        verification_command,
                        env=environment,
                        timeout=60,
                    )
                    observed_source_commit, observed_signer_digest = (
                        self._verify_attestation_result(
                            result.stdout,
                            expected_name,
                            digest,
                            expected_workflow=workflow,
                            expected_source_commit=source_commit,
                        )
                    )
                if (
                    source_commit is not None
                    and (
                        observed_source_commit != source_commit
                        or observed_signer_digest != source_commit
                    )
                ):
                    raise RequestRejected(
                        "Artifact attestation execution commit differs from the "
                        "release authority"
                    )
                execution_observations.append(
                    AttestationExecutionObservation(
                        subject_name=expected_name,
                        subject_digest=digest,
                        workflow=workflow,
                        source_commit=observed_source_commit,
                        signer_digest=observed_signer_digest,
                    )
                )
                attestation_evidence.append(
                    AttestationEvidence(
                        subject_name=expected_name,
                        subject_digest=digest,
                        repository=REPOSITORY,
                        workflow=workflow,
                        certificate_identity=(
                            f"https://github.com/{REPOSITORY}/{workflow}"
                            "@refs/heads/main"
                        ),
                        oidc_issuer="https://token.actions.githubusercontent.com",
                        logical_source_commit=(
                            logical_provenance_commit
                            if source_commit is None
                            else source_commit
                        ),
                        source_ref="refs/heads/main",
                        logical_signer_digest=(
                            logical_provenance_commit
                            if source_commit is None
                            else source_commit
                        ),
                        predicate_type="https://slsa.dev/provenance/v1",
                    )
                )
            material_cache = self.cache_root / "verified-materials"
            try:
                _ensure_private_directory(self.cache_root, material_cache)
                final_root = material_cache / (
                    version
                    + "-"
                    + manifest["deployment"]["installerMaterials"][
                        "sha256"
                    ].removeprefix("sha256:")
                )
                authority = AuthorityEvidence(
                    repository=REPOSITORY,
                    version=version,
                    draft=False,
                    prerelease=metadata["prerelease"],
                    tag_commit=commit,
                    assets=tuple(
                        ReleaseAssetEvidence(
                            name=item.name,
                            state=item.state,
                        )
                        for item in inventory.assets
                        if item.name in EXPECTED_RELEASE_ASSETS
                    ),
                    attestations=tuple(attestation_evidence),
                )
                verified = ReleaseAuthorityVerifier().verify(
                    assets={name: (destination / name).read_bytes() for name in EXPECTED_RELEASE_ASSETS},
                    authority=authority,
                    destination=final_root,
                    updater_version=updater_version,
                )
                execution_receipt_payload = {
                    "schema": "animemo.attestation-execution-receipt/v1",
                    "observations": [
                        {
                            "subjectName": item.subject_name,
                            "subjectDigest": item.subject_digest,
                            "workflow": item.workflow,
                            "sourceCommit": item.source_commit,
                            "signerDigest": item.signer_digest,
                        }
                        for item in execution_observations
                    ],
                }
                execution_receipt_identity = "sha256:" + hashlib.sha256(
                    json.dumps(
                        execution_receipt_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                verified = replace(
                    verified,
                    attestation_execution_receipt=AttestationExecutionReceipt(
                        schema=execution_receipt_payload["schema"],
                        observations=tuple(execution_observations),
                        identity=execution_receipt_identity,
                    ),
                )
            except (OSError, StateError, RequestRejected) as error:
                raise RequestRejected(
                    "Verified installer materials cannot be published"
                ) from error
            if raw_archive:
                transaction._bind(verified)
                yield transaction
            else:
                yield verified

    def fetch_verified(
        self,
        version: str,
        *,
        updater_version: str = __version__,
        refresh: bool = False,
    ) -> dict[str, object]:
        return self.fetch_verified_materials(
            version,
            updater_version=updater_version,
            refresh=refresh,
        ).manifest


# Compatibility name for callers migrating from the former GitHub-specific source.
ReleaseResolver = GitHubReleaseSource
