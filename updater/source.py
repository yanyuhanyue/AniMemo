from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
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
from .commands import CommandRunner
from .authority import (
    AttestationEvidence,
    AuthorityEvidence,
    ReleaseAssetEvidence,
    ReleaseAuthorityVerifier,
    VerifiedReleaseMaterials,
)
from .errors import CommandFailed, RequestRejected, StateError
from .protocol import CHANNELS, RELEASE_VERSION
from .state import _absolute, _ensure_private_directory
from .transport import (
    ExplicitTransportPolicy,
    GitHubTransportSource,
    OfficialMirrorTransportSource,
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


def _expected_release_asset_names(version: str) -> set[str]:
    expected = set(EXPECTED_RELEASE_ASSETS)
    if Version(version.removeprefix("v")).release >= (1, 1, 0):
        expected.add(f"animemo-{version}-portable.tar")
    return expected
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
    ):
        self.cache_root = _absolute(cache_root)
        self.runner = runner or CommandRunner()
        self.rest = rest or GitHubPublicRest(runner=self.runner)
        self.transport_policy = policy or ExplicitTransportPolicy.github()
        if type(self.transport_policy) is not ExplicitTransportPolicy:
            raise RequestRejected("Release transport policy is invalid")
        available = transports or {
            TransportSourceId.GITHUB: GitHubTransportSource(
                runner=self.runner,
                credential_provider=getattr(self.rest, "configured_token", None),
            ),
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

    @staticmethod
    def _verify_attestation_result(
        output: str, expected_name: str, digest: str
    ) -> None:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as error:
            raise RequestRejected(
                "Artifact attestation verification returned invalid JSON"
            ) from error
        expected_digest = digest.removeprefix("sha256:")
        if not isinstance(payload, list) or not payload:
            raise RequestRejected(
                "Artifact attestation verification returned no result"
            )
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
            if not isinstance(subjects, list):
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
                    return
        raise RequestRejected(
            "Artifact attestation subject does not match the release authority"
        )

    def fetch_verified_materials(
        self,
        version: str,
        *,
        updater_version: str = "1.0.0",
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
        try:
            _ensure_private_directory(self.cache_root, self.cache_root)
        except StateError as error:
            raise RequestRejected("Release cache directory is unavailable") from error
        metadata = self.rest.get_json(
            f"/repos/{REPOSITORY}/releases/tags/{version}",
            label="Exact GitHub release metadata",
        )
        if (
            not isinstance(metadata, dict)
            or metadata.get("tag_name") != version
            or metadata.get("draft") is not False
            or not isinstance(metadata.get("prerelease"), bool)
        ):
            raise RequestRejected("Exact GitHub release metadata is invalid")
        with tempfile.TemporaryDirectory(
            prefix=f".{version}.", dir=self.cache_root
        ) as temporary:
            staging = Path(temporary)
            acquired = self.transport_source.acquire(
                TransportRequest.release_bundle(version.removeprefix("v")),
                staging,
            )
            if acquired.receipt.transport_id is not self.transport_policy.source:
                raise RequestRejected("Release transport receipt and policy differ")
            destination = acquired.root
            for name in EXPECTED_RELEASE_ASSETS:
                acquired.material(name)
            environment = self._anonymous_gh_environment(staging / ".authority-runtime")
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
            release_assets = metadata.get("assets")
            if not isinstance(release_assets, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or item.get("state") != "uploaded"
                for item in release_assets
            ):
                raise RequestRejected(
                    "GitHub release assets differ from the release contract"
                )
            asset_names = [item["name"] for item in release_assets]
            expected_release_assets = _expected_release_asset_names(version)
            if (
                len(asset_names) != len(expected_release_assets)
                or len(asset_names) != len(set(asset_names))
                or set(asset_names) != expected_release_assets
            ):
                raise RequestRejected(
                    "GitHub release assets differ from the release contract"
                )
            commit = manifest["release"]["commit"]
            provenance_commit = manifest["provenance"]["sourceCommit"]
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
            for index, (
                subject,
                expected_name,
                digest,
                workflow,
                source_commit,
            ) in enumerate(subjects):
                bundle = destination / f"attestation-{index}.jsonl"
                self._write_attestation_bundle(digest, bundle)
                result = self.runner.run(
                    [
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
                        "--source-digest",
                        source_commit,
                        "--source-ref",
                        "refs/heads/main",
                        "--signer-digest",
                        source_commit,
                        "--predicate-type",
                        "https://slsa.dev/provenance/v1",
                        "--format",
                        "json",
                    ],
                    env=environment,
                    timeout=60,
                )
                self._verify_attestation_result(result.stdout, expected_name, digest)
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
                        source_commit=source_commit,
                        source_ref="refs/heads/main",
                        signer_digest=source_commit,
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
                            name=item["name"],
                            state=item["state"],
                        )
                        for item in release_assets
                        if item["name"] in EXPECTED_RELEASE_ASSETS
                    ),
                    attestations=tuple(attestation_evidence),
                )
                verified = ReleaseAuthorityVerifier().verify(
                    assets={name: (destination / name).read_bytes() for name in EXPECTED_RELEASE_ASSETS},
                    authority=authority,
                    destination=final_root,
                    updater_version=updater_version,
                )
            except (OSError, StateError, RequestRejected) as error:
                raise RequestRejected(
                    "Verified installer materials cannot be published"
                ) from error
        self._verified_cache[version] = (time.monotonic(), verified)
        return verified

    def fetch_verified(
        self,
        version: str,
        *,
        updater_version: str = "1.0.0",
        refresh: bool = False,
    ) -> dict[str, object]:
        return self.fetch_verified_materials(
            version,
            updater_version=updater_version,
            refresh=refresh,
        ).manifest


# Compatibility name for callers migrating from the former GitHub-specific source.
ReleaseResolver = GitHubReleaseSource
