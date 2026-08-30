from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "release" / "producer-toolchain.lock.json"
DOCKERFILE_PATH = ROOT / "deploy" / "release-producer.Dockerfile"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


class ProducerToolchainError(ValueError):
    pass


def _reject() -> None:
    raise ProducerToolchainError("PRODUCER_TOOLCHAIN_RECEIPT_INVALID")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _strict_object(path: Path) -> dict[str, Any]:
    def pairs(items):
        value = {}
        for key, item in items:
            if type(key) is not str or key in value:
                _reject()
            value[key] = item
        return value

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: _reject(),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProducerToolchainError(
            "PRODUCER_TOOLCHAIN_RECEIPT_INVALID"
        ) from error
    if type(value) is not dict:
        _reject()
    return value


def validate_producer_toolchain_receipt(
    receipt_path: Path,
    *,
    expected_candidate_sha: str,
) -> dict[str, Any]:
    if _GIT_SHA.fullmatch(expected_candidate_sha) is None:
        _reject()
    receipt = _strict_object(Path(receipt_path))
    lock = _strict_object(LOCK_PATH)
    if set(receipt) != {
        "schemaVersion",
        "candidateSha",
        "runner",
        "byteAuthority",
        "toolchainLockSha256",
    }:
        _reject()
    if (
        receipt["schemaVersion"]
        != "animemo.release-producer-toolchain-receipt.v1"
        or receipt["candidateSha"] != expected_candidate_sha
        or receipt["toolchainLockSha256"] != _sha256(LOCK_PATH)
    ):
        _reject()

    runner = receipt["runner"]
    if type(runner) is not dict or set(runner) != {
        "label",
        "os",
        "arch",
        "imageOS",
        "imageVersion",
        "observationOnly",
    }:
        _reject()
    if (
        runner["label"] != lock["githubHostedRunner"]["label"]
        or runner["observationOnly"] is not True
        or any(
            type(runner[field]) is not str or not runner[field]
            for field in ("os", "arch", "imageOS", "imageVersion")
        )
    ):
        _reject()

    authority = receipt["byteAuthority"]
    expected_keys = {
        "releaseProducer",
        "python",
        "go",
        "buildx",
        "buildkit",
        "buildkitImage",
        "backendImage",
        "nodeImage",
        "npm",
    }
    if type(authority) is not dict or set(authority) != expected_keys:
        _reject()
    producer = authority["releaseProducer"]
    if (
        type(producer) is not dict
        or set(producer) != {"imageId", "dockerfileSha256"}
        or type(producer["imageId"]) is not str
        or _DIGEST.fullmatch(producer["imageId"]) is None
        or producer["dockerfileSha256"] != _sha256(DOCKERFILE_PATH)
    ):
        _reject()
    byte_lock = lock["byteAuthority"]
    if (
        authority["python"] != byte_lock["python"]["hostedRuntimeVersion"]
        or authority["go"] != f"go{byte_lock['go']['version']}"
        or authority["buildx"] != byte_lock["buildx"]["version"]
        or authority["buildkit"] != byte_lock["buildkit"]["version"]
        or authority["buildkitImage"] != byte_lock["buildkit"]["image"]
        or authority["backendImage"] != byte_lock["python"]["backendImage"]
        or authority["nodeImage"] != byte_lock["node"]["image"]
        or authority["npm"] != byte_lock["npm"]
    ):
        _reject()
    return receipt
