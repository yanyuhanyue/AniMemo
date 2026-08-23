from __future__ import annotations

import argparse


POSTGRES_REPOSITORY = "docker.io/library/postgres"
REDIS_REPOSITORY = "docker.io/library/redis"
POSTGRES_DIGEST = (
    "sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571"
)
REDIS_DIGEST = "sha256:9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf"
POSTGRES_IMAGE = f"{POSTGRES_REPOSITORY}@{POSTGRES_DIGEST}"
REDIS_IMAGE = f"{REDIS_REPOSITORY}@{REDIS_DIGEST}"

_IMAGES = {
    "postgres": POSTGRES_IMAGE,
    "redis": REDIS_IMAGE,
}


def dependency_image(role: str) -> str:
    return _IMAGES[role]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print one immutable release dependency image authority."
    )
    parser.add_argument("role", choices=tuple(_IMAGES))
    args = parser.parse_args(argv)
    print(dependency_image(args.role))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
