from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from durability.instance import (
    DEFAULT_INSTANCE_NAME,
    InstanceLocator,
    InstanceName,
    InstanceSnapshot,
    LocalLocatorStore,
    LocalReadOnlyHost,
    LocatorError,
    ReadOnlyHost,
    instance_locator_digest,
    instance_locator_payload,
    instance_namespace,
    load_instance_snapshot,
    publish_instance_locator,
    release_identity_from_manifest,
)
from durability.managed_config import (
    LocalManagedConfigStore,
    derive_runtime_environment,
)
from durability.ownership import LocalOwnershipReceiptStore

from . import __version__
from .agent import UpdateAgent
from .binding import CanonicalRuntimeBinding
from .deployment import HostPaths, ImmutableComposeDeployment
from .errors import StateError
from .executor import UpdateExecutor
from .local_bundle import LocalBundleReleaseSource
from .plans import PlanStore
from .runtime_state import RuntimeState
from .server import UnixRpcServer
from .slots import ReleaseSlots
from .source import ReleaseResolver
from .state import OperationStore
from .transport import ExplicitTransportPolicy

_DEFAULT_NAMESPACE = instance_namespace()
PRODUCTION_SOCKET_PATH = Path(str(_DEFAULT_NAMESPACE.updater_socket_path))
PRODUCTION_BOOTSTRAP_MANIFEST = Path(
    str(_DEFAULT_NAMESPACE.updater_state_root / "bootstrap/release-manifest.json")
)
PRODUCTION_ADOPTION_REQUEST = Path(
    str(_DEFAULT_NAMESPACE.updater_state_root / "bootstrap/initial-adoption.json")
)


class _InitialAdoptionOnlyBinding:
    def refresh(self):
        raise StateError("Update runtime requires a published canonical locator")

    def replace_release(self, manifest):
        del manifest
        raise StateError("Update runtime requires a published canonical locator")


@dataclass(frozen=True)
class InitialAdoptionRequest:
    locator: InstanceLocator
    manifest: dict[str, object]


@dataclass(frozen=True)
class AdoptionReceipt:
    operation_id: str
    locator_digest: str
    release_identity: Mapping[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "operationId": self.operation_id,
            "locatorDigest": self.locator_digest,
            "releaseIdentity": dict(self.release_identity),
        }


@dataclass(frozen=True)
class CanonicalInstanceRegistry:
    """Discover one canonical AniMemo Instance with no fallback sources."""

    store: ReadOnlyHost
    expected_owner_uid: int | None = None
    expected_owner_gid: int | None = None
    instance_name: InstanceName = DEFAULT_INSTANCE_NAME
    verify_ownership: bool = False

    def __init__(
        self,
        *,
        store: ReadOnlyHost | None = None,
        expected_owner_uid: int | None = None,
        expected_owner_gid: int | None = None,
        instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
        verify_ownership: bool = False,
    ) -> None:
        namespace = instance_namespace(instance_name)
        object.__setattr__(
            self,
            "store",
            store or LocalLocatorStore(instance_name=namespace.name),
        )
        object.__setattr__(self, "expected_owner_uid", expected_owner_uid)
        object.__setattr__(self, "expected_owner_gid", expected_owner_gid)
        object.__setattr__(self, "instance_name", namespace.name)
        object.__setattr__(self, "verify_ownership", verify_ownership)

    def snapshot(self) -> InstanceSnapshot:
        snapshot = load_instance_snapshot(
            self.store,
            instance_name=self.instance_name,
            expected_owner_uid=self.expected_owner_uid,
            expected_owner_gid=self.expected_owner_gid,
        )
        if self.verify_ownership:
            receipt = LocalOwnershipReceiptStore(
                instance_name=self.instance_name
            ).read()
            if (
                receipt.receipt_digest
                != snapshot.locator.ownership_receipt_digest
                or receipt.instance_name != snapshot.locator.instance_name
                or receipt.instance_id != snapshot.locator.instance_id
                or receipt.compose_project != snapshot.locator.compose_project
            ):
                raise LocatorError("OWNERSHIP_RECEIPT_LOCATOR_MISMATCH")
        return snapshot

    @classmethod
    def production(
        cls,
        instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
    ) -> CanonicalInstanceRegistry:
        if __import__("os").name == "nt":
            raise StateError("Canonical production locator requires a POSIX host")
        import grp
        import pwd

        return cls(
            instance_name=instance_name,
            verify_ownership=True,
            expected_owner_uid=pwd.getpwnam("animemo-updater").pw_uid,
            expected_owner_gid=grp.getgrnam("animemo-api").gr_gid,
        )


@dataclass
class HostAgentRuntime:
    """Compose the fixed host Agent behind a tiny lifecycle interface."""

    paths: HostPaths
    socket_path: Path
    bootstrap_manifest: Path
    slots: ReleaseSlots
    runtime_state: RuntimeState
    deployment: ImmutableComposeDeployment
    agent: UpdateAgent
    server: UnixRpcServer
    transport_policy: ExplicitTransportPolicy
    registry: CanonicalInstanceRegistry | None = None
    locator_store: LocalLocatorStore | None = None

    @classmethod
    def _build(
        cls,
        *,
        paths: HostPaths,
        socket_path: Path,
        bootstrap_manifest: Path,
        registry: CanonicalInstanceRegistry | None = None,
        managed_environment: dict[str, str] | None = None,
        background: bool = True,
        release_resolver=None,
        resolver_factory=None,
        local_bundle_resolver_factory=None,
        transport_policy: ExplicitTransportPolicy | None = None,
    ) -> HostAgentRuntime:
        state_root = paths.state_root
        slots = ReleaseSlots(state_root / "releases")
        runtime_state = RuntimeState(state_root)
        operations = OperationStore(state_root)
        selected_policy = transport_policy or ExplicitTransportPolicy.github()
        if type(selected_policy) is not ExplicitTransportPolicy:
            raise StateError("Updater transport policy is invalid")
        cache_root = state_root / "cache" / "releases"

        def default_resolver_factory(policy):
            return ReleaseResolver(cache_root, policy=policy)

        configured_factory = resolver_factory or default_resolver_factory

        def default_local_bundle_resolver_factory(
            *,
            payload=None,
            release_attestation=None,
            binding=None,
            expected_rollback_version=None,
        ):
            from .offline import production_offline_release_verifier

            verifier = production_offline_release_verifier()
            local_cache = state_root / "cache" / "local-bundles"
            if binding is not None:
                if (
                    payload is not None
                    or release_attestation is not None
                    or expected_rollback_version is not None
                    or not isinstance(binding, dict)
                ):
                    raise StateError("Local bundle resolver binding is invalid")
                transport_identity = binding.get("transportIdentity")
                bound_rollback_version = binding.get("expectedRollbackVersion")
                if not isinstance(transport_identity, str):
                    raise StateError("Local bundle transport binding is invalid")
                if bound_rollback_version is not None and not isinstance(
                    bound_rollback_version, str
                ):
                    raise StateError("Local bundle rollback binding is invalid")
                return LocalBundleReleaseSource.from_staged(
                    cache_root=local_cache,
                    transport_identity=transport_identity,
                    verifier=verifier,
                    updater_version=__version__,
                    expected_rollback_version=bound_rollback_version,
                )
            if not isinstance(payload, Path) or not isinstance(
                release_attestation, Path
            ):
                raise StateError("Local bundle media pair is required")
            return LocalBundleReleaseSource.from_media(
                payload=payload,
                release_attestation=release_attestation,
                cache_root=local_cache,
                verifier=verifier,
                updater_version=__version__,
                expected_rollback_version=expected_rollback_version,
            )

        configured_local_factory = (
            local_bundle_resolver_factory
            or default_local_bundle_resolver_factory
        )
        source = release_resolver or configured_factory(selected_policy)
        source_policy = getattr(source, "transport_policy", None)
        if (
            type(source_policy) is not ExplicitTransportPolicy
            or source_policy.identity != selected_policy.identity
        ):
            raise StateError("Release Resolver and transport policy differ")
        deployment = ImmutableComposeDeployment(
            paths,
            managed_environment=managed_environment,
        )
        runtime_binding = (
            CanonicalRuntimeBinding(
                registry=registry,
                config_store=LocalManagedConfigStore(
                    instance_name=paths.instance_name
                ),
                deployment=deployment,
            )
            if registry is not None
            else _InitialAdoptionOnlyBinding()
        )
        executor = UpdateExecutor(
            store=operations,
            slots=slots,
            release_source=source,
            deployment=deployment,
            runtime_state=runtime_state,
            runtime_binding=runtime_binding,
            lock_path=state_root / "update.lock",
            updater_version=__version__,
        )
        agent = UpdateAgent(
            source=source,
            operations=operations,
            plans=PlanStore(state_root),
            slots=slots,
            runtime_state=runtime_state,
            executor=executor,
            background=background,
            resolver_factory=configured_factory,
            transport_policy=selected_policy,
            local_bundle_resolver_factory=configured_local_factory,
        )
        server = UnixRpcServer(socket_path, agent)
        return cls(
            paths=paths,
            socket_path=socket_path,
            bootstrap_manifest=bootstrap_manifest,
            slots=slots,
            runtime_state=runtime_state,
            deployment=deployment,
            agent=agent,
            server=server,
            transport_policy=selected_policy,
            registry=registry,
            locator_store=(
                registry.store
                if registry is not None
                and isinstance(registry.store, LocalLocatorStore)
                else LocalLocatorStore.testing(
                    state_root / "instance.json",
                    instance_name=paths.instance_name,
                )
            ),
        )

    @classmethod
    def production(
        cls,
        instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
    ) -> HostAgentRuntime:
        namespace = instance_namespace(instance_name)
        registry = CanonicalInstanceRegistry.production(namespace.name)
        snapshot = registry.snapshot()
        config_store = LocalManagedConfigStore(instance_name=namespace.name)
        config = config_store.read()
        if (
            config.config_revision != snapshot.locator.config_revision
            or config.listen.host != snapshot.locator.listen.host
            or config.listen.port != snapshot.locator.listen.port
            or config.public_origin != snapshot.locator.public_origin
        ):
            raise StateError(
                "Managed configuration does not match the canonical locator"
            )
        config_store.rebuild_runtime_env(
            locator_digest=snapshot.digest,
            expected_revision=config.config_revision
        )
        return cls._build(
            paths=HostPaths.production(snapshot),
            socket_path=Path(str(namespace.updater_socket_path)),
            bootstrap_manifest=Path(
                str(namespace.updater_state_root / "bootstrap/release-manifest.json")
            ),
            registry=registry,
            managed_environment=dict(
                derive_runtime_environment(
                    config, namespace=namespace, locator_digest=snapshot.digest
                )
            ),
        )

    @classmethod
    def testing(
        cls,
        *,
        app_root: Path,
        data_root: Path,
        state_root: Path,
        socket_path: Path,
        bootstrap_manifest: Path,
        background: bool = False,
        release_resolver=None,
        resolver_factory=None,
        local_bundle_resolver_factory=None,
        transport_policy: ExplicitTransportPolicy | None = None,
    ) -> HostAgentRuntime:
        return cls._build(
            paths=HostPaths.testing(app=app_root, data=data_root, state=state_root),
            socket_path=socket_path.resolve(),
            bootstrap_manifest=bootstrap_manifest.resolve(),
            background=background,
            release_resolver=release_resolver,
            resolver_factory=resolver_factory,
            local_bundle_resolver_factory=local_bundle_resolver_factory,
            transport_policy=transport_policy,
        )

    @staticmethod
    def _identity(manifest: dict[str, object]) -> dict[str, object]:
        return {
            "version": manifest["release"]["version"],
            "channel": manifest["release"]["channel"],
            "commit": manifest["release"]["commit"],
            "apiDigest": manifest["images"]["api"]["digest"],
            "webDigest": manifest["images"]["web"]["digest"],
        }

    def adopt_initial_release(self, request: InitialAdoptionRequest) -> AdoptionReceipt:
        if not isinstance(request, InitialAdoptionRequest):
            raise StateError("Initial adoption request is invalid")
        lock_lease = self.agent.executor.acquire_lock(allow_reentrant=True)
        operation = None
        try:
            expected_identity, verified, enabled_plugin_apis = (
                self._verify_initial_adoption(request)
            )
            self.agent.operations.require_recovery_clear()
            slots = self.slots.read()
            if (
                slots["current"] is not None
                or slots["previous"] is not None
                or slots["history"]
            ):
                raise StateError(
                    "CURRENT is already initialized; initial adoption is one-time"
                )
            if self.runtime_state.path.exists():
                raise StateError("Runtime compatibility state already exists")
            try:
                load_instance_snapshot(
                    self.locator_store,
                    instance_name=self.paths.instance_name,
                )
            except Exception as error:
                if not getattr(error, "code", None) == "LOCATOR_MISSING":
                    raise StateError(
                        "Instance locator state is already initialized"
                    ) from error

            operation = self.agent.operations.create(
                "initial_adoption",
                {
                    "version": verified["release"]["version"],
                    "locator": instance_locator_payload(request.locator),
                },
            )
            operation_id = operation["id"]
            self.agent.operations.transition(
                operation_id, "preflight", detail="verified canonical adoption target"
            )
            self.agent.operations.transition(
                operation_id, "fetching", detail="reverified exact Release authority"
            )
            self.agent.operations.transition(
                operation_id, "verifying", detail="verified running release identity"
            )
            self.agent.operations.bind_recovery_target(operation_id, verified)
            self.agent.operations.transition(
                operation_id,
                "adopting",
                detail="publishing initial CURRENT and runtime state",
            )
            self.slots.import_current(verified)
            self.runtime_state.initialize_from_manifest(
                verified,
                enabled_plugin_apis=enabled_plugin_apis,
            )
            published = publish_instance_locator(
                request.locator,
                store=self.locator_store,
                owner_uid=(
                    self.registry.expected_owner_uid
                    if self.registry is not None
                    else None
                ),
                owner_gid=(
                    self.registry.expected_owner_gid
                    if self.registry is not None
                    else None
                ),
            )
            completed = self.agent.operations.transition(
                operation_id,
                "succeeded",
                detail="exact initial release adoption completed",
            )
            return AdoptionReceipt(
                operation_id=completed["id"],
                locator_digest=published.digest,
                release_identity=expected_identity,
            )
        except Exception as error:
            if operation is not None:
                current = self.agent.operations.get(operation["id"])
                if current["status"] == "adopting":
                    self.agent.operations.transition(
                        operation["id"],
                        "manual_recovery_required",
                        detail="initial adoption mutation failed; manual recovery is required",
                    )
                elif current["status"] not in {
                    "failed_pre_switch",
                    "manual_recovery_required",
                }:
                    self.agent.operations.transition(
                        operation["id"],
                        "failed_pre_switch",
                        detail="initial adoption failed before durable state publication",
                    )
            if isinstance(error, StateError):
                raise
            raise StateError("Initial adoption requires manual recovery") from error
        finally:
            lock_lease.__exit__(None, None, None)

    def _verify_initial_adoption(
        self,
        request: InitialAdoptionRequest,
    ) -> tuple[Mapping[str, str], dict[str, object], set[int]]:
        """Reverify every adoption fact while the global operation lock is held."""

        try:
            expected_identity = release_identity_from_manifest(request.manifest)
            if dict(expected_identity) != dict(request.locator.release_identity):
                raise StateError(
                    "Initial adoption release identity differs from the locator"
                )
            verified = self.agent.source.fetch_verified(
                request.manifest["release"]["version"],
                updater_version=__version__,
                refresh=True,
            )
            if verified != request.manifest:
                raise StateError(
                    "Fresh verified release differs from the adoption request"
                )
            if self.paths.locator_digest is not None:
                expected_paths = HostPaths.production(
                    InstanceSnapshot(
                        locator=request.locator,
                        digest=self.paths.locator_digest,
                        storage_digest="",
                    )
                )
                if expected_paths != self.paths:
                    raise StateError(
                        "Initial adoption locator does not match production paths"
                    )
            self.deployment.verify_deployment_contract(verified)
            self.deployment.verify_health(verified)
            live_contracts = self.deployment.inspect_runtime_contracts(verified)
            expected_contracts = {
                "databaseContract": verified["compatibility"]["database"]["contract"],
                "configurationContract": verified["compatibility"]["configuration"][
                    "contract"
                ],
            }
            if live_contracts != expected_contracts:
                raise StateError(
                    "Initial adoption live contracts differ from the release"
                )
            enabled_plugin_apis = self.deployment.inspect_enabled_plugin_apis(verified)
            supported = set(verified["compatibility"]["pluginSdk"]["supportedApis"])
            if not enabled_plugin_apis.issubset(supported):
                raise StateError(
                    "Initial adoption enabled Plugin SDK APIs are unsupported"
                )
            return expected_identity, verified, enabled_plugin_apis
        except StateError:
            raise
        except Exception as error:
            raise StateError(
                "Initial adoption read-only verification failed"
            ) from error

    def status(self) -> dict[str, object]:
        return self.agent.dispatch({"operation": "get_status", "params": {}})

    def reconcile(self, operation_id: str, confirmation: str) -> dict[str, object]:
        if confirmation != f"RECONCILE {operation_id}":
            raise StateError(
                "Host reconciliation confirmation does not match the operation"
            )
        operation = self.agent.operations.get(operation_id)
        if operation.get("kind") == "initial_adoption":
            return self._reconcile_initial_adoption(operation)
        self.agent.bind_operation_resolver(operation)
        return self.agent.executor.reconcile(operation_id)

    def _reconcile_initial_adoption(
        self,
        operation: dict[str, object],
    ) -> dict[str, object]:
        operation_id = str(operation.get("id", ""))
        if operation.get("status") != "manual_recovery_required":
            raise StateError("Initial adoption operation does not require recovery")
        metadata = operation.get("metadata")
        recovery = operation.get("recovery")
        locator_payload = metadata.get("locator") if isinstance(metadata, dict) else None
        manifest = (
            recovery.get("targetManifest") if isinstance(recovery, dict) else None
        )
        if not isinstance(locator_payload, dict) or not isinstance(manifest, dict):
            raise StateError("Initial adoption recovery evidence is incomplete")
        from durability.instance import parse_instance_locator

        request = InitialAdoptionRequest(
            locator=parse_instance_locator(locator_payload),
            manifest=manifest,
        )
        lock_lease = self.agent.executor.acquire_lock()
        try:
            expected_identity, verified, enabled_plugin_apis = (
                self._verify_initial_adoption(request)
            )
            current = self.slots.read()
            if current["previous"] is not None:
                raise StateError("Initial adoption recovery found unexpected PREVIOUS")
            if current["current"] is None:
                self.slots.import_current(verified)
            elif current["current"] != verified:
                raise StateError("Initial adoption recovery CURRENT differs")
            if not self.runtime_state.path.exists():
                self.runtime_state.initialize_from_manifest(
                    verified,
                    enabled_plugin_apis=enabled_plugin_apis,
                )
            else:
                runtime_identity = self.runtime_state.read()
                expected_runtime = {
                    "databaseContract": verified["compatibility"]["database"][
                        "contract"
                    ],
                    "configurationContract": verified["compatibility"][
                        "configuration"
                    ]["contract"],
                    "enabledPluginApis": sorted(enabled_plugin_apis),
                }
                if runtime_identity != expected_runtime:
                    raise StateError("Initial adoption recovery runtime state differs")
            try:
                published = load_instance_snapshot(
                    self.locator_store,
                    instance_name=self.paths.instance_name,
                )
            except LocatorError as error:
                if error.code != "LOCATOR_MISSING":
                    raise StateError("Initial adoption recovery locator is invalid") from error
                published = publish_instance_locator(
                    request.locator,
                    store=self.locator_store,
                    owner_uid=(
                        self.registry.expected_owner_uid
                        if self.registry is not None
                        else None
                    ),
                    owner_gid=(
                        self.registry.expected_owner_gid
                        if self.registry is not None
                        else None
                    ),
                )
            if (
                published.locator != request.locator
                or dict(published.locator.release_identity) != dict(expected_identity)
            ):
                raise StateError("Initial adoption recovery locator differs")
            return self.agent.operations.transition(
                operation_id,
                "reconciled",
                detail="initial adoption recovery reverified CURRENT, runtime, and locator",
            )
        finally:
            lock_lease.__exit__(None, None, None)

    def serve_forever(self) -> None:
        try:
            self.server.serve_forever()
        finally:
            self.agent.close(timeout=30.0)


def production_runtime(
    instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
) -> HostAgentRuntime:
    return HostAgentRuntime.production(instance_name)


def load_initial_adoption_request(
    instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
) -> InitialAdoptionRequest:
    namespace = instance_namespace(instance_name)
    try:
        if __import__("os").name == "nt":
            raise StateError("Initial adoption requires a POSIX host")
        import grp
        import pwd

        secure = LocalReadOnlyHost().read_secure_bytes(
            namespace.updater_state_root / "bootstrap/initial-adoption.json",
            limit=1024 * 1024,
            expected_owner_uid=pwd.getpwnam("animemo-updater").pw_uid,
            expected_owner_gid=grp.getgrnam("animemo-api").gr_gid,
            required_mode=0o600,
        )
        raw = secure.payload.decode("utf-8")

        def reject_constant(_: str) -> None:
            raise TypeError

        def reject_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        payload = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
        if not isinstance(payload, dict) or set(payload) != {"locator", "manifest"}:
            raise TypeError
        locator_payload = payload["locator"]
        manifest = payload["manifest"]
        if not isinstance(locator_payload, dict) or not isinstance(manifest, dict):
            raise TypeError
        from durability.instance import parse_instance_locator

        return InitialAdoptionRequest(
            locator=parse_instance_locator(locator_payload),
            manifest=manifest,
        )
    except StateError:
        raise
    except Exception as error:
        raise StateError(
            "Fixed initial adoption request is unavailable or invalid"
        ) from error


def adopt_initial_release(request: InitialAdoptionRequest) -> AdoptionReceipt:
    """Host-only exact initial adoption; the locator is published last."""

    if not isinstance(request, InitialAdoptionRequest):
        raise StateError("Initial adoption request is invalid")
    namespace = instance_namespace(request.locator.instance_name)
    registry = CanonicalInstanceRegistry.production(namespace.name)
    config_store = LocalManagedConfigStore(instance_name=namespace.name)
    config = config_store.read()
    if (
        config.instance_id != request.locator.instance_id
        or config.config_revision != request.locator.config_revision
        or config.listen.host != request.locator.listen.host
        or config.listen.port != request.locator.listen.port
        or config.public_origin != request.locator.public_origin
    ):
        raise StateError("Managed configuration does not match the adoption locator")
    planned_locator_digest = instance_locator_digest(request.locator)
    config_store.rebuild_runtime_env(
        locator_digest=planned_locator_digest,
        expected_revision=config.config_revision,
    )
    runtime = HostAgentRuntime._build(
        paths=HostPaths.initial_adoption(request.locator),
        socket_path=Path(str(namespace.updater_socket_path)),
        bootstrap_manifest=Path(
            str(namespace.updater_state_root / "bootstrap/release-manifest.json")
        ),
        registry=registry,
        managed_environment=dict(
            derive_runtime_environment(
                config,
                namespace=namespace,
                locator_digest=planned_locator_digest,
            )
        ),
        background=False,
    )
    return runtime.adopt_initial_release(request)
