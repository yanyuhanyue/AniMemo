from __future__ import annotations

from dataclasses import replace

from durability.instance import (
    InstanceSnapshot,
    release_identity_from_manifest,
    replace_instance_locator,
)
from durability.managed_config import derive_runtime_environment

from .deployment import HostPaths, ImmutableComposeDeployment
from .errors import StateError


class CanonicalRuntimeBinding:
    """Keep locator, managed config, deployment, and CURRENT release coherent."""

    def __init__(self, *, registry, config_store, deployment: ImmutableComposeDeployment):
        self.registry = registry
        self.config_store = config_store
        self.deployment = deployment

    def refresh(self) -> InstanceSnapshot:
        snapshot = self.registry.snapshot()
        config = self.config_store.read()
        locator = snapshot.locator
        if (
            config.instance_id != locator.instance_id
            or config.config_revision != locator.config_revision
            or config.listen.host != locator.listen.host
            or config.listen.port != locator.listen.port
            or config.public_origin != locator.public_origin
        ):
            raise StateError(
                "Managed configuration does not match the canonical locator"
            )
        self.config_store.rebuild_runtime_env(
            expected_revision=config.config_revision
        )
        self.deployment.refresh_binding(
            HostPaths.production(snapshot),
            managed_environment=dict(derive_runtime_environment(config)),
        )
        return snapshot

    def replace_release(self, manifest: dict[str, object]) -> InstanceSnapshot:
        snapshot = self.refresh()
        updated = replace(
            snapshot.locator,
            release_identity=release_identity_from_manifest(manifest),
        )
        return replace_instance_locator(
            updated,
            expected_digest=snapshot.digest,
            store=self.registry.store,
            owner_uid=self.registry.expected_owner_uid,
            owner_gid=self.registry.expected_owner_gid,
        )


__all__ = ["CanonicalRuntimeBinding"]
