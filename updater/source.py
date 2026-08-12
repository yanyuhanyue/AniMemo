from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from packaging.version import InvalidVersion, Version

from release.contract import API_REPOSITORY, REPOSITORY, WEB_REPOSITORY, validate_manifest

from .commands import CommandRunner
from .errors import RequestRejected
from .protocol import CHANNELS, RELEASE_VERSION


class GitHubReleaseSource:
    def __init__(self, cache_root: Path, *, runner=None, cache_seconds: int = 300):
        self.cache_root = cache_root.resolve()
        self.runner = runner or CommandRunner()
        self.cache_seconds = cache_seconds
        self._release_cache: tuple[float, list[dict[str, object]]] | None = None
        self._verified_cache: dict[str, tuple[float, dict[str, object]]] = {}

    def _list_all(self, *, refresh: bool) -> list[dict[str, object]]:
        now = time.monotonic()
        if not refresh and self._release_cache and now - self._release_cache[0] < self.cache_seconds:
            return self._release_cache[1]
        result = self.runner.run(
            [
                "/usr/bin/gh",
                "api",
                f"repos/{REPOSITORY}/releases",
                "--paginate",
                "--jq",
                ".[] | {tag_name,draft,prerelease,published_at}",
            ],
            timeout=30,
        )
        raw = result.stdout.strip()
        try:
            payload = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line]
        except json.JSONDecodeError as error:
            raise RequestRejected("GitHub release discovery returned invalid metadata") from error
        releases = [item for item in payload if not item.get("draft") and RELEASE_VERSION.fullmatch(str(item.get("tag_name", "")))]
        self._release_cache = (now, releases)
        return releases

    def list_releases(self, channel: str, *, refresh: bool = False) -> list[dict[str, object]]:
        if channel not in CHANNELS:
            raise RequestRejected("Invalid release channel")
        accepted = {"stable"}
        if channel in {"rc", "beta"}:
            accepted.add("rc")
        if channel == "beta":
            accepted.add("beta")
        result = []
        for item in self._list_all(refresh=refresh):
            tag = str(item["tag_name"])
            parsed = Version(tag.removeprefix("v"))
            prerelease_channels = {"a": "alpha", "b": "beta", "rc": "rc"}
            item_channel = "stable" if not parsed.is_prerelease else prerelease_channels.get(str(parsed.pre[0]), str(parsed.pre[0]))
            if item_channel in accepted:
                result.append({"version": tag, "channel": item_channel, "publishedAt": item.get("published_at")})
        return sorted(result, key=lambda item: Version(item["version"].removeprefix("v")), reverse=True)

    @staticmethod
    def _verify_checksum(root: Path) -> None:
        lines = (root / "checksums.txt").read_text(encoding="utf-8").splitlines()
        expected = {}
        for line in lines:
            digest, separator, name = line.partition("  ")
            if separator != "  " or len(digest) != 64 or name not in {"release-manifest.json"}:
                raise RequestRejected("Release checksums contain an unexpected artifact")
            expected[name] = digest
        if set(expected) != {"release-manifest.json"}:
            raise RequestRejected("Release checksums do not cover release-manifest.json")
        actual = hashlib.sha256((root / "release-manifest.json").read_bytes()).hexdigest()
        if actual != expected["release-manifest.json"]:
            raise RequestRejected("Release manifest checksum mismatch")

    def fetch_verified(self, version: str, *, updater_version: str = "1.0.0") -> dict[str, object]:
        if not isinstance(version, str) or not RELEASE_VERSION.fullmatch(version):
            raise RequestRejected("Invalid immutable release version")
        cached = self._verified_cache.get(version)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            validate_manifest(cached[1], updater_version=updater_version)
            return cached[1]
        destination = self.cache_root / version
        if destination.is_symlink():
            raise RequestRejected("Release cache path must not be a symlink")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runner.run(
            [
                "/usr/bin/gh", "release", "download", version,
                "--repo", REPOSITORY,
                "--pattern", "release-manifest.json",
                "--pattern", "checksums.txt",
                "--clobber",
                "--dir", str(destination),
            ],
            timeout=60,
        )
        self._verify_checksum(destination)
        if any((destination / name).is_symlink() for name in ("release-manifest.json", "checksums.txt")):
            raise RequestRejected("Release assets must not be symlinks")
        try:
            manifest = json.loads((destination / "release-manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RequestRejected("Release manifest is unreadable") from error
        validate_manifest(manifest, updater_version=updater_version)
        if manifest["release"]["version"] != version:
            raise RequestRejected("Release tag and manifest version differ")
        commit = manifest["release"]["commit"]
        provenance_commit = manifest["provenance"]["sourceCommit"]
        subjects = [
            (f"oci://{API_REPOSITORY}@{manifest['images']['api']['digest']}", ".github/workflows/release.yml", commit),
            (f"oci://{WEB_REPOSITORY}@{manifest['images']['web']['digest']}", ".github/workflows/release.yml", commit),
            (str(destination / "release-manifest.json"), manifest["provenance"]["workflow"], provenance_commit),
        ]
        for subject, workflow, source_commit in subjects:
            self.runner.run(
                [
                    "/usr/bin/gh", "attestation", "verify", subject,
                    "--repo", REPOSITORY,
                    "--signer-workflow", f"{REPOSITORY}/{workflow}",
                    "--source-digest", source_commit,
                ],
                timeout=60,
            )
        self._verified_cache[version] = (time.monotonic(), manifest)
        return manifest
