"""Closed ownership receipt for one instance-scoped deployment."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from uuid import UUID

from .canonical import canonical_json_bytes, sha256_identity
from .instance import (
    InstanceName,
    LocatorError,
    instance_namespace,
    parse_release_identity,
)
from .private_store import AtomicPrivateFile, PrivateStoreError

OWNERSHIP_SCHEMA_VERSION = 1
OWNERSHIP_RECEIPT_NAME = "ownership.json"
MAX_OWNERSHIP_RECEIPT_BYTES = 1024 * 1024
_FIELDS = frozenset(
    {
        "schemaVersion",
        "instanceName",
        "instanceId",
        "composeProject",
        "appRoot",
        "dataRoot",
        "stateRoot",
        "runtimeRoot",
        "updaterService",
        "updaterSocket",
        "listen",
        "ownedContainers",
        "ownedNetworks",
        "ownedVolumes",
        "ownedFiles",
        "releaseIdentity",
        "createdAt",
        "receiptDigest",
    }
)


@dataclass(frozen=True)
class OwnershipReceipt:
    schema_version: int
    instance_name: InstanceName
    instance_id: str
    compose_project: str
    app_root: PurePosixPath
    data_root: PurePosixPath
    state_root: PurePosixPath
    runtime_root: PurePosixPath
    updater_service: str
    updater_socket: PurePosixPath
    listen: Mapping[str, object]
    owned_containers: tuple[str, ...]
    owned_networks: tuple[str, ...]
    owned_volumes: tuple[str, ...]
    owned_files: tuple[str, ...]
    release_identity: Mapping[str, Any]
    created_at: str
    receipt_digest: str


def _body(receipt: OwnershipReceipt) -> dict[str, object]:
    return {
        "schemaVersion": receipt.schema_version,
        "instanceName": str(receipt.instance_name),
        "instanceId": receipt.instance_id,
        "composeProject": receipt.compose_project,
        "appRoot": str(receipt.app_root),
        "dataRoot": str(receipt.data_root),
        "stateRoot": str(receipt.state_root),
        "runtimeRoot": str(receipt.runtime_root),
        "updaterService": receipt.updater_service,
        "updaterSocket": str(receipt.updater_socket),
        "listen": dict(receipt.listen),
        "ownedContainers": list(receipt.owned_containers),
        "ownedNetworks": list(receipt.owned_networks),
        "ownedVolumes": list(receipt.owned_volumes),
        "ownedFiles": list(receipt.owned_files),
        "releaseIdentity": dict(receipt.release_identity),
        "createdAt": receipt.created_at,
    }


def ownership_receipt_payload(receipt: OwnershipReceipt) -> dict[str, object]:
    expected = sha256_identity(canonical_json_bytes(_body(receipt)))
    if receipt.receipt_digest != expected:
        raise LocatorError("OWNERSHIP_RECEIPT_DIGEST_INVALID")
    return {**_body(receipt), "receiptDigest": expected}


def create_ownership_receipt(
    *,
    instance_name: InstanceName | str,
    instance_id: str,
    listen_host: str,
    listen_port: int,
    release_identity: Mapping[str, Any],
    created_at: str,
) -> OwnershipReceipt:
    namespace = instance_namespace(instance_name)
    provisional = OwnershipReceipt(
        schema_version=OWNERSHIP_SCHEMA_VERSION,
        instance_name=namespace.name,
        instance_id=instance_id,
        compose_project=namespace.compose_project,
        app_root=namespace.app_root,
        data_root=namespace.data_root,
        state_root=namespace.updater_state_root,
        runtime_root=namespace.updater_runtime_root,
        updater_service=namespace.updater_service,
        updater_socket=namespace.updater_socket_path,
        listen=MappingProxyType({"host": listen_host, "port": listen_port}),
        owned_containers=tuple(
            f"{namespace.compose_project}-{service}-1"
            for service in ("api", "web", "postgres", "redis")
        ),
        owned_networks=(f"{namespace.compose_project}_animemo",),
        owned_volumes=(),
        owned_files=(
            str(namespace.managed_config_path),
            str(namespace.locator_path),
            str(namespace.managed_env_path),
            str(namespace.updater_state_root / OWNERSHIP_RECEIPT_NAME),
        ),
        release_identity=MappingProxyType(dict(release_identity)),
        created_at=created_at,
        receipt_digest="",
    )
    return replace(
        provisional,
        receipt_digest=sha256_identity(canonical_json_bytes(_body(provisional))),
    )


def parse_ownership_receipt(raw: bytes) -> OwnershipReceipt:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise LocatorError("OWNERSHIP_RECEIPT_CONTENT_INVALID") from None
    if not isinstance(payload, dict) or frozenset(payload) != _FIELDS:
        raise LocatorError("OWNERSHIP_RECEIPT_SCHEMA_INVALID")
    name = InstanceName(payload["instanceName"])
    namespace = instance_namespace(name)
    expected = {
        "composeProject": namespace.compose_project,
        "appRoot": str(namespace.app_root),
        "dataRoot": str(namespace.data_root),
        "stateRoot": str(namespace.updater_state_root),
        "runtimeRoot": str(namespace.updater_runtime_root),
        "updaterService": namespace.updater_service,
        "updaterSocket": str(namespace.updater_socket_path),
    }
    if payload.get("schemaVersion") != OWNERSHIP_SCHEMA_VERSION or any(
        payload.get(field) != value for field, value in expected.items()
    ):
        raise LocatorError("OWNERSHIP_RECEIPT_NAMESPACE_INVALID")
    try:
        instance_id = str(UUID(payload["instanceId"]))
        created_at = datetime.fromisoformat(
            payload["createdAt"].replace("Z", "+00:00")
        )
        listen = payload["listen"]
        canonical_host = ipaddress.ip_address(listen["host"]).compressed
        expected_containers = [
            f"{namespace.compose_project}-{service}-1"
            for service in ("api", "web", "postgres", "redis")
        ]
        expected_networks = [f"{namespace.compose_project}_animemo"]
        expected_files = [
            str(namespace.managed_config_path),
            str(namespace.locator_path),
            str(namespace.managed_env_path),
            str(namespace.updater_state_root / OWNERSHIP_RECEIPT_NAME),
        ]
        release_identity = parse_release_identity(payload["releaseIdentity"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise LocatorError("OWNERSHIP_RECEIPT_SCHEMA_INVALID") from None
    if (
        payload["instanceId"] != instance_id
        or created_at.tzinfo is None
        or not isinstance(listen, dict)
        or set(listen) != {"host", "port"}
        or listen["host"] != canonical_host
        or type(listen["port"]) is not int
        or not 1 <= listen["port"] <= 65535
        or payload["ownedContainers"] != expected_containers
        or payload["ownedNetworks"] != expected_networks
        or payload["ownedVolumes"] != []
        or payload["ownedFiles"] != expected_files
        or not isinstance(release_identity, Mapping)
    ):
        raise LocatorError("OWNERSHIP_RECEIPT_SCHEMA_INVALID")
    try:
        receipt = OwnershipReceipt(
            schema_version=payload["schemaVersion"],
            instance_name=name,
            instance_id=payload["instanceId"],
            compose_project=payload["composeProject"],
            app_root=namespace.app_root,
            data_root=namespace.data_root,
            state_root=namespace.updater_state_root,
            runtime_root=namespace.updater_runtime_root,
            updater_service=namespace.updater_service,
            updater_socket=namespace.updater_socket_path,
            listen=MappingProxyType(dict(payload["listen"])),
            owned_containers=tuple(payload["ownedContainers"]),
            owned_networks=tuple(payload["ownedNetworks"]),
            owned_volumes=tuple(payload["ownedVolumes"]),
            owned_files=tuple(payload["ownedFiles"]),
            release_identity=MappingProxyType(dict(payload["releaseIdentity"])),
            created_at=payload["createdAt"],
            receipt_digest=payload["receiptDigest"],
        )
        ownership_receipt_payload(receipt)
    except (KeyError, TypeError, ValueError):
        raise LocatorError("OWNERSHIP_RECEIPT_SCHEMA_INVALID") from None
    return receipt


class LocalOwnershipReceiptStore:
    def __init__(self, *, instance_name: InstanceName | str) -> None:
        self.namespace = instance_namespace(instance_name)
        self._store = AtomicPrivateFile(
            Path(str(self.namespace.updater_state_root)), OWNERSHIP_RECEIPT_NAME
        )

    @classmethod
    def testing(
        cls,
        state_root: Path,
        *,
        instance_name: InstanceName | str = "default",
    ) -> LocalOwnershipReceiptStore:
        root = Path(state_root).absolute()
        if not root.is_absolute():
            raise ValueError("Testing ownership root must be absolute")
        selected = cls.__new__(cls)
        selected.namespace = instance_namespace(instance_name)
        selected._store = AtomicPrivateFile(root, OWNERSHIP_RECEIPT_NAME)
        return selected

    @property
    def path(self) -> Path:
        return self._store.path

    def publish(self, receipt: OwnershipReceipt) -> None:
        if receipt.instance_name != self.namespace.name:
            raise LocatorError("OWNERSHIP_RECEIPT_INSTANCE_MISMATCH")
        payload = (
            json.dumps(
                ownership_receipt_payload(receipt),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        try:
            self._store.write(payload, must_not_exist=True)
        except PrivateStoreError as error:
            raise LocatorError(error.code) from None

    def read(self) -> OwnershipReceipt:
        try:
            raw = self._store.read(limit=MAX_OWNERSHIP_RECEIPT_BYTES)
        except PrivateStoreError as error:
            raise LocatorError(error.code) from None
        receipt = parse_ownership_receipt(raw)
        if receipt.instance_name != self.namespace.name:
            raise LocatorError("OWNERSHIP_RECEIPT_INSTANCE_MISMATCH")
        return receipt
