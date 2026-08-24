from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

AUTHORITY_PATH = Path(__file__).with_name("dependency-images.json")
_ROLES = ("postgres", "redis")
_TOP_LEVEL_FIELDS = frozenset(("schemaVersion", "images"))
_IMAGE_FIELDS = frozenset(("repository", "digest", "platform"))
_REPOSITORY = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class DependencyImageAuthorityError(ValueError):
    """The canonical dependency-image authority is invalid or unavailable."""


def _fail(code: str) -> NoReturn:
    raise DependencyImageAuthorityError(f"DEPENDENCY_IMAGE_AUTHORITY_{code}")


@dataclass(frozen=True)
class DependencyImage:
    role: str
    repository: str
    digest: str
    platform: str

    @property
    def reference(self) -> str:
        return f"{self.repository}@{self.digest}"


@dataclass(frozen=True)
class DependencyImageAuthority:
    schema_version: int
    postgres: DependencyImage
    redis: DependencyImage
    canonical_bytes: bytes
    identity: str

    @property
    def roles(self) -> tuple[str, str]:
        return _ROLES

    def image(self, role: str) -> DependencyImage:
        if role == "postgres":
            return self.postgres
        if role == "redis":
            return self.redis
        _fail("ROLE_INVALID")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _canonical_bytes(images: dict[str, DependencyImage]) -> bytes:
    payload = {
        "schemaVersion": 1,
        "images": {
            role: {
                "repository": images[role].repository,
                "digest": images[role].digest,
                "platform": images[role].platform,
            }
            for role in _ROLES
        },
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def parse_dependency_image_authority(raw: bytes) -> DependencyImageAuthority:
    if not isinstance(raw, bytes):
        _fail("JSON_INVALID")
    try:
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=_strict_object)
    except DependencyImageAuthorityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DependencyImageAuthorityError(
            "DEPENDENCY_IMAGE_AUTHORITY_JSON_INVALID"
        ) from error

    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        _fail("SCHEMA_INVALID")
    if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1:
        _fail("SCHEMA_INVALID")
    raw_images = payload["images"]
    if not isinstance(raw_images, dict) or set(raw_images) != set(_ROLES):
        _fail("SCHEMA_INVALID")

    images: dict[str, DependencyImage] = {}
    for role in _ROLES:
        raw_image = raw_images[role]
        if not isinstance(raw_image, dict) or set(raw_image) != _IMAGE_FIELDS:
            _fail("SCHEMA_INVALID")
        repository = raw_image["repository"]
        digest = raw_image["digest"]
        platform = raw_image["platform"]
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            _fail("REPOSITORY_INVALID")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            _fail("DIGEST_INVALID")
        if platform != "linux/amd64":
            _fail("PLATFORM_INVALID")
        images[role] = DependencyImage(
            role=role,
            repository=repository,
            digest=digest,
            platform=platform,
        )

    canonical = _canonical_bytes(images)
    identity = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return DependencyImageAuthority(
        schema_version=1,
        postgres=images["postgres"],
        redis=images["redis"],
        canonical_bytes=canonical,
        identity=identity,
    )


def load_dependency_image_authority() -> DependencyImageAuthority:
    try:
        raw = AUTHORITY_PATH.read_bytes()
    except OSError as error:
        raise DependencyImageAuthorityError(
            "DEPENDENCY_IMAGE_AUTHORITY_FILE_UNAVAILABLE"
        ) from error
    return parse_dependency_image_authority(raw)


def github_env_lines(
    authority: DependencyImageAuthority,
) -> tuple[str, str, str]:
    return (
        f"POSTGRES_IMAGE={authority.postgres.reference}",
        f"REDIS_IMAGE={authority.redis.reference}",
        f"DEPENDENCY_IMAGE_AUTHORITY_SHA256={authority.identity}",
    )


AUTHORITY = load_dependency_image_authority()
POSTGRES_REPOSITORY = AUTHORITY.postgres.repository
POSTGRES_DIGEST = AUTHORITY.postgres.digest
POSTGRES_IMAGE = AUTHORITY.postgres.reference
REDIS_REPOSITORY = AUTHORITY.redis.repository
REDIS_DIGEST = AUTHORITY.redis.digest
REDIS_IMAGE = AUTHORITY.redis.reference
DEPENDENCY_IMAGE_AUTHORITY_SHA256 = AUTHORITY.identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project the closed canonical dependency-image authority."
    )
    parser.add_argument("command", choices=("emit-github-env",))
    args = parser.parse_args(argv)
    if args.command == "emit-github-env":
        authority = load_dependency_image_authority()
        sys.stdout.write("\n".join(github_env_lines(authority)) + "\n")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
