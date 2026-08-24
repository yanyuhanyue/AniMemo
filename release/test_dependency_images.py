from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from release.dependency_images import (
    AUTHORITY_PATH,
    POSTGRES_DIGEST,
    POSTGRES_IMAGE,
    POSTGRES_REPOSITORY,
    REDIS_DIGEST,
    REDIS_IMAGE,
    REDIS_REPOSITORY,
    DependencyImageAuthorityError,
    compose_env_lines,
    github_env_lines,
    load_dependency_image_authority,
    parse_dependency_image_authority,
)

ROOT = Path(__file__).resolve().parents[1]


def authority_bytes(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "images": {
            "postgres": {
                "repository": "registry.example.test/library/postgres",
                "digest": "sha256:" + "a" * 64,
                "platform": "linux/amd64",
            },
            "redis": {
                "repository": "registry.example.test/library/redis",
                "digest": "sha256:" + "b" * 64,
                "platform": "linux/amd64",
            },
        },
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


class DependencyImageAuthorityTests(unittest.TestCase):
    def test_canonical_file_loads_exact_closed_roles(self) -> None:
        authority = load_dependency_image_authority()
        self.assertEqual(authority.schema_version, 1)
        self.assertEqual(authority.roles, ("postgres", "redis"))
        self.assertEqual(authority.image("postgres").reference, POSTGRES_IMAGE)
        self.assertEqual(authority.image("redis").reference, REDIS_IMAGE)
        self.assertEqual(POSTGRES_IMAGE, f"{POSTGRES_REPOSITORY}@{POSTGRES_DIGEST}")
        self.assertEqual(REDIS_IMAGE, f"{REDIS_REPOSITORY}@{REDIS_DIGEST}")

    def test_value_objects_are_frozen(self) -> None:
        image = parse_dependency_image_authority(authority_bytes()).image("postgres")
        with self.assertRaises(FrozenInstanceError):
            image.digest = "sha256:" + "c" * 64  # type: ignore[misc]

    def test_unknown_role_fails_closed(self) -> None:
        authority = parse_dependency_image_authority(authority_bytes())
        with self.assertRaisesRegex(DependencyImageAuthorityError, "ROLE_INVALID"):
            authority.image("mysql")

    def test_schema_version_must_be_exact_integer_one(self) -> None:
        for value in (0, 2, "1", True):
            with self.subTest(value=value), self.assertRaisesRegex(
                DependencyImageAuthorityError, "SCHEMA_INVALID"
            ):
                parse_dependency_image_authority(
                    authority_bytes(schemaVersion=value)
                )

    def test_missing_each_required_role_fails(self) -> None:
        payload = json.loads(authority_bytes())
        for role in ("postgres", "redis"):
            with self.subTest(role=role):
                changed = json.loads(json.dumps(payload))
                del changed["images"][role]
                with self.assertRaisesRegex(
                    DependencyImageAuthorityError, "SCHEMA_INVALID"
                ):
                    parse_dependency_image_authority(json.dumps(changed).encode())

    def test_unknown_role_in_data_fails(self) -> None:
        payload = json.loads(authority_bytes())
        payload["images"]["mysql"] = payload["images"]["postgres"]
        with self.assertRaisesRegex(DependencyImageAuthorityError, "SCHEMA_INVALID"):
            parse_dependency_image_authority(json.dumps(payload).encode())

    def test_unknown_top_level_field_fails(self) -> None:
        with self.assertRaisesRegex(DependencyImageAuthorityError, "SCHEMA_INVALID"):
            parse_dependency_image_authority(authority_bytes(fallback="forbidden"))

    def test_unknown_image_field_fails(self) -> None:
        payload = json.loads(authority_bytes())
        payload["images"]["redis"]["mirror"] = "registry.example.test/redis"
        with self.assertRaisesRegex(DependencyImageAuthorityError, "SCHEMA_INVALID"):
            parse_dependency_image_authority(json.dumps(payload).encode())

    def test_duplicate_json_key_fails(self) -> None:
        duplicated = b'''{
          "schemaVersion": 1,
          "schemaVersion": 1,
          "images": {}
        }'''
        with self.assertRaisesRegex(DependencyImageAuthorityError, "JSON_DUPLICATE_KEY"):
            parse_dependency_image_authority(duplicated)

    def test_non_utf8_and_non_object_json_fail(self) -> None:
        for raw in (b"\xff", b"[]", b"null"):
            with self.subTest(raw=raw), self.assertRaises(
                DependencyImageAuthorityError
            ):
                parse_dependency_image_authority(raw)

    def test_repository_must_be_lowercase_fully_qualified_and_tagless(self) -> None:
        invalid = (
            "postgres",
            "Postgres",
            "docker.io/Postgres",
            "docker.io/library/postgres:16",
            "docker.io/library/postgres@sha256:" + "a" * 64,
            "https://docker.io/library/postgres",
            "docker.io/library/postgres\nINJECTED=1",
        )
        for repository in invalid:
            with self.subTest(repository=repository):
                payload = json.loads(authority_bytes())
                payload["images"]["postgres"]["repository"] = repository
                with self.assertRaisesRegex(
                    DependencyImageAuthorityError, "REPOSITORY_INVALID"
                ):
                    parse_dependency_image_authority(json.dumps(payload).encode())

    def test_digest_must_be_exact_lowercase_sha256(self) -> None:
        invalid = (
            "a" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "A" * 64,
            "sha512:" + "a" * 64,
            "sha256:" + "a" * 64 + "\nINJECTED=1",
        )
        for digest in invalid:
            with self.subTest(digest=digest):
                payload = json.loads(authority_bytes())
                payload["images"]["postgres"]["digest"] = digest
                with self.assertRaisesRegex(
                    DependencyImageAuthorityError, "DIGEST_INVALID"
                ):
                    parse_dependency_image_authority(json.dumps(payload).encode())

    def test_platform_is_exact_linux_amd64(self) -> None:
        for platform in ("linux/arm64", "windows/amd64", "linux/amd64\nX=1"):
            with self.subTest(platform=platform):
                payload = json.loads(authority_bytes())
                payload["images"]["redis"]["platform"] = platform
                with self.assertRaisesRegex(
                    DependencyImageAuthorityError, "PLATFORM_INVALID"
                ):
                    parse_dependency_image_authority(json.dumps(payload).encode())

    def test_reference_is_derived_from_repository_and_digest(self) -> None:
        image = parse_dependency_image_authority(authority_bytes()).image("redis")
        self.assertEqual(image.reference, f"{image.repository}@{image.digest}")

    def test_identity_is_deterministic_across_json_key_order(self) -> None:
        original = parse_dependency_image_authority(authority_bytes())
        payload = json.loads(authority_bytes())
        reordered = {
            "images": {
                "redis": {
                    "platform": payload["images"]["redis"]["platform"],
                    "digest": payload["images"]["redis"]["digest"],
                    "repository": payload["images"]["redis"]["repository"],
                },
                "postgres": payload["images"]["postgres"],
            },
            "schemaVersion": 1,
        }
        second = parse_dependency_image_authority(json.dumps(reordered).encode())
        self.assertEqual(original.identity, second.identity)
        self.assertEqual(original.canonical_bytes, second.canonical_bytes)

    def test_loader_has_no_arbitrary_authority_path(self) -> None:
        self.assertEqual(tuple(inspect.signature(load_dependency_image_authority).parameters), ())
        self.assertEqual(AUTHORITY_PATH, Path(__file__).with_name("dependency-images.json"))

    def test_environment_cannot_override_authority(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "POSTGRES_IMAGE": "attacker.example/postgres@sha256:" + "c" * 64,
                "REDIS_IMAGE": "attacker.example/redis@sha256:" + "d" * 64,
                "DEPENDENCY_IMAGE_AUTHORITY_PATH": "attacker.json",
            },
        ):
            authority = load_dependency_image_authority()
        self.assertEqual(authority.image("postgres").reference, POSTGRES_IMAGE)
        self.assertEqual(authority.image("redis").reference, REDIS_IMAGE)

    def test_github_env_projection_is_fixed_order_single_line_and_complete(self) -> None:
        authority = load_dependency_image_authority()
        lines = github_env_lines(authority)
        self.assertEqual(
            lines,
            (
                f"POSTGRES_IMAGE={POSTGRES_IMAGE}",
                f"REDIS_IMAGE={REDIS_IMAGE}",
                f"DEPENDENCY_IMAGE_AUTHORITY_SHA256={authority.identity}",
            ),
        )
        self.assertTrue(all("\n" not in line and "\r" not in line for line in lines))

    def test_compose_env_projection_is_fixed_order_single_line_and_complete(self) -> None:
        authority = load_dependency_image_authority()
        lines = compose_env_lines(authority)
        self.assertEqual(
            lines,
            (
                f"ANIMEMO_POSTGRES_IMAGE={POSTGRES_IMAGE}",
                f"ANIMEMO_REDIS_IMAGE={REDIS_IMAGE}",
                f"DEPENDENCY_IMAGE_AUTHORITY_SHA256={authority.identity}",
            ),
        )
        self.assertTrue(all("\n" not in line and "\r" not in line for line in lines))

    def test_standalone_isolated_cli_emits_only_fixed_github_env(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", "release/dependency_images.py", "emit-github-env"],
            cwd=ROOT,
            env={**os.environ, "POSTGRES_IMAGE": "ignored", "REDIS_IMAGE": "ignored"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(github_env_lines(load_dependency_image_authority())))
        self.assertEqual(result.stderr, "")

    def test_cli_rejects_arbitrary_paths_roles_and_values(self) -> None:
        for arguments in (
            ["get", "mysql"],
            ["--authority", "other.json"],
            ["emit-github-env", "extra"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-I", "release/dependency_images.py", *arguments],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_loader_contains_no_real_repository_or_digest_literal(self) -> None:
        source = (ROOT / "release" / "dependency_images.py").read_text(encoding="utf-8")
        authority = load_dependency_image_authority()
        for role in authority.roles:
            image = authority.image(role)
            self.assertNotIn(image.repository, source)
            self.assertNotIn(image.digest, source)


if __name__ == "__main__":
    unittest.main()
