from __future__ import annotations

import threading
import time

from . import __version__
from .compatibility import DeploymentContext, plan_switch
from .errors import RequestRejected, StateError
from .protocol import validate_request
from .redaction import redact
from .state import PRE_SWITCH_RECOVERY, TERMINAL_STATES
from .transport import ExplicitTransportPolicy, TransportSourceId


def _policy_for_source(source: str) -> ExplicitTransportPolicy:
    if source == TransportSourceId.GITHUB.value:
        return ExplicitTransportPolicy.github()
    if source == TransportSourceId.OFFICIAL_MIRROR.value:
        return ExplicitTransportPolicy.official_mirror()
    raise RequestRejected("Release transport source is invalid")


def _verified_materials(resolver, version: str, *, refresh: bool = False):
    fetch = getattr(resolver, "fetch_verified_materials", None)
    if not callable(fetch):
        raise StateError("Release Resolver cannot provide verified material identity")
    materials = fetch(
        version,
        updater_version=__version__,
        refresh=refresh,
    )
    manifest = getattr(materials, "manifest", None)
    identity = getattr(materials, "identity_digest", None)
    if (
        not isinstance(manifest, dict)
        or not isinstance(identity, str)
        or len(identity) != 71
        or not identity.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in identity[7:])
    ):
        raise StateError("Verified release materials identity is invalid")
    return materials


class _BoundReleaseResolver:
    def __init__(
        self,
        resolver,
        *,
        manifest: dict[str, object],
        verified_release_identity: str,
        transport_policy: ExplicitTransportPolicy,
    ) -> None:
        self._resolver = resolver
        self._manifest = manifest
        self._verified_release_identity = verified_release_identity
        self.transport_policy = transport_policy

    def fetch_verified_materials(
        self,
        version: str,
        *,
        updater_version: str = "1.0.0",
        refresh: bool = False,
    ):
        materials = self._resolver.fetch_verified_materials(
            version,
            updater_version=updater_version,
            refresh=refresh,
        )
        if (
            getattr(materials, "identity_digest", None)
            != self._verified_release_identity
            or getattr(materials, "manifest", None) != self._manifest
        ):
            raise StateError(
                "Release differs from the operation-bound verified identity"
            )
        return materials

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


class UpdateAgent:
    def __init__(
        self,
        *,
        source,
        operations,
        plans,
        slots,
        runtime_state,
        executor,
        background: bool = True,
        runtime_refresh_seconds: int = 30,
        resolver_factory=None,
        transport_policy: ExplicitTransportPolicy | None = None,
    ):
        self.source = source
        self.operations = operations
        self.plans = plans
        self.slots = slots
        self.runtime_state = runtime_state
        self.executor = executor
        self.background = background
        self.runtime_refresh_seconds = runtime_refresh_seconds
        self.transport_policy = (
            transport_policy
            or getattr(source, "transport_policy", None)
            or ExplicitTransportPolicy.github()
        )
        if type(self.transport_policy) is not ExplicitTransportPolicy:
            raise StateError("Updater transport policy is invalid")
        source_policy = getattr(source, "transport_policy", self.transport_policy)
        if (
            type(source_policy) is not ExplicitTransportPolicy
            or source_policy.identity != self.transport_policy.identity
        ):
            raise StateError("Release Resolver and transport policy differ")
        self.resolver_factory = resolver_factory
        self._resolvers = {self.transport_policy.identity: source}
        self._runtime_refreshed_at = 0.0

    def _resolver_for(self, policy: ExplicitTransportPolicy):
        cached = self._resolvers.get(policy.identity)
        if cached is not None:
            return cached
        if self.resolver_factory is None:
            raise RequestRejected("Requested release transport source is unavailable")
        resolver = self.resolver_factory(policy)
        resolver_policy = getattr(resolver, "transport_policy", None)
        if (
            type(resolver_policy) is not ExplicitTransportPolicy
            or resolver_policy.identity != policy.identity
            or resolver_policy.source is not policy.source
        ):
            raise StateError("Release Resolver and requested transport policy differ")
        self._resolvers[policy.identity] = resolver
        return resolver

    @staticmethod
    def _release_binding(materials, policy: ExplicitTransportPolicy):
        return {
            "source": policy.source.value,
            "transportPolicyIdentity": policy.identity,
            "verifiedReleaseIdentity": materials.identity_digest,
        }

    def _bound_resolver(
        self,
        binding: object,
        manifest: dict[str, object],
    ) -> _BoundReleaseResolver:
        if not isinstance(binding, dict):
            raise StateError("Operation release binding is missing")
        policy = _policy_for_source(str(binding.get("source", "")))
        if binding.get("transportPolicyIdentity") != policy.identity:
            raise StateError("Operation transport policy binding is invalid")
        identity = binding.get("verifiedReleaseIdentity")
        if (
            not isinstance(identity, str)
            or len(identity) != 71
            or not identity.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in identity[7:])
        ):
            raise StateError("Operation verified release identity is invalid")
        return _BoundReleaseResolver(
            self._resolver_for(policy),
            manifest=manifest,
            verified_release_identity=identity,
            transport_policy=policy,
        )

    @staticmethod
    def _policy_from_binding(binding: object) -> ExplicitTransportPolicy:
        if not isinstance(binding, dict):
            raise StateError(
                "Release transport binding is unavailable; explicit migration is required"
            )
        policy = _policy_for_source(str(binding.get("source", "")))
        if binding.get("transportPolicyIdentity") != policy.identity:
            raise StateError("Release transport policy binding is invalid")
        verified_identity = binding.get("verifiedReleaseIdentity")
        if (
            not isinstance(verified_identity, str)
            or len(verified_identity) != 71
            or not verified_identity.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in verified_identity[7:]
            )
        ):
            raise StateError("Release verified identity binding is invalid")
        return policy

    def _current_release_policy(self, current: dict[str, object]):
        current_version = current["release"]["version"]
        candidates = sorted(
            self.operations.list(),
            key=lambda item: (item["updatedAt"], item["id"]),
            reverse=True,
        )
        for operation in candidates:
            metadata = operation.get("metadata")
            if (
                operation.get("status") not in {"succeeded", "rolled_back", "reconciled"}
                or not isinstance(metadata, dict)
                or metadata.get("version") != current_version
            ):
                continue
            return self._policy_from_binding(metadata.get("releaseBinding"))
        raise StateError(
            "CURRENT release transport binding is unavailable; "
            "explicit migration is required"
        )

    def recover(self) -> list[str]:
        for operation in self.operations.list():
            if (
                operation.get("kind") in {"apply_update", "rollback_previous"}
                and operation.get("status") not in TERMINAL_STATES
            ):
                metadata = operation.get("metadata")
                if not isinstance(metadata, dict):
                    raise StateError(
                        "Incomplete update operation binding is unavailable; "
                        "explicit migration is required"
                    )
                self._policy_from_binding(metadata.get("releaseBinding"))
        return self.operations.recover_incomplete()

    def bind_operation_resolver(self, operation: object) -> None:
        if not isinstance(operation, dict):
            raise StateError("Recovery operation is invalid")
        metadata = operation.get("metadata")
        if not isinstance(metadata, dict):
            raise StateError("Recovery operation binding is unavailable")
        version = metadata.get("version")
        binding = metadata.get("releaseBinding")
        if not isinstance(version, str):
            raise StateError("Recovery operation release version is invalid")
        candidates: list[dict[str, object]] = []
        recovery = operation.get("recovery")
        if isinstance(recovery, dict):
            target = recovery.get("targetManifest")
            if isinstance(target, dict):
                candidates.append(target)
        slots = self.slots.read()
        for name in ("current", "previous"):
            candidate = slots.get(name)
            if isinstance(candidate, dict):
                candidates.append(candidate)
        for historical in slots.get("history", []):
            if isinstance(historical, dict):
                candidate = historical.get("manifest")
                if isinstance(candidate, dict):
                    candidates.append(candidate)
        matches = [
            candidate
            for candidate in candidates
            if candidate.get("release", {}).get("version") == version
        ]
        if not matches or any(candidate != matches[0] for candidate in matches[1:]):
            raise StateError("Recovery operation manifest binding is unavailable")
        self.executor.release_source = self._bound_resolver(binding, matches[0])

    def _context(self, *, refresh_plugins: bool = True) -> DeploymentContext:
        slots = self.slots.read()
        current = slots["current"]
        if current is None:
            raise RequestRejected("CURRENT release identity is not initialized")
        runtime = (
            self._refresh_enabled_plugin_apis(current)
            if refresh_plugins
            else self.runtime_state.read()
        )
        return DeploymentContext(
            current_manifest=current,
            database_contract=runtime["databaseContract"],
            configuration_contract=runtime["configurationContract"],
            enabled_plugin_apis=frozenset(runtime["enabledPluginApis"]),
        )

    @staticmethod
    def _identity(manifest):
        if manifest is None:
            return None
        return {
            "version": manifest["release"]["version"],
            "channel": manifest["release"]["channel"],
            "commit": manifest["release"]["commit"],
            "apiDigest": manifest["images"]["api"]["digest"],
            "webDigest": manifest["images"]["web"]["digest"],
        }

    def _refresh_enabled_plugin_apis(self, current) -> dict[str, object]:
        now = time.monotonic()
        if now - self._runtime_refreshed_at >= self.runtime_refresh_seconds:
            enabled = sorted(self.executor.deployment.inspect_enabled_plugin_apis(current))
            runtime = self.runtime_state.update(enabledPluginApis=enabled)
            self._runtime_refreshed_at = now
            return runtime
        return self.runtime_state.read()

    def _status(self):
        slots = self.slots.read()
        operations = sorted(self.operations.list(), key=lambda item: item["createdAt"], reverse=True)
        blocked = self.operations.recovery_block()
        context = self._context(refresh_plugins=blocked is None)
        runtime = self.runtime_state.read()
        previous_compatibility = (
            plan_switch(context, slots["previous"], updater_version=__version__).as_dict()
            if slots["previous"] is not None
            else None
        )
        return {
            "updaterVersion": __version__,
            "current": self._identity(slots["current"]),
            "previous": self._identity(slots["previous"]),
            "previousCompatibility": previous_compatibility,
            "runtime": runtime,
            "recoveryBlock": (
                {
                    "required": True,
                    "operationId": blocked["id"],
                    "since": blocked["updatedAt"],
                    "detail": blocked["events"][-1]["detail"],
                }
                if blocked is not None
                else None
            ),
            "operation": operations[0] if operations else None,
            "history": [
                {
                    **self._identity(item["manifest"]),
                    "deployment": item["deployment"],
                    "compatibility": plan_switch(context, item["manifest"], updater_version=__version__).as_dict(),
                }
                for item in reversed(slots["history"])
            ],
        }

    def _list_releases(self, params):
        releases = self.source.list_releases(params["channel"], refresh=params.get("refresh", False))
        current = self._context(refresh_plugins=self.operations.recovery_block() is None)
        planned = []
        for release in releases:
            try:
                manifest = self.source.fetch_verified(release["version"], updater_version=__version__)
                switch = plan_switch(current, manifest, updater_version=__version__)
                planned.append({**release, "compatibility": switch.as_dict()})
            except Exception as error:  # noqa: BLE001 - one bad Release must not abort discovery
                planned.append({
                    **release,
                    "compatibility": {
                        "allowed": False,
                        "decision": "blocked",
                        "rollbackMode": "blocked",
                        "migrationRequired": False,
                        "migrationPolicy": "unknown",
                        "reasons": [redact(error)],
                    },
                })
        return {"channel": params["channel"], "releases": planned}

    def _plan_update(self, params):
        self.operations.require_recovery_clear()
        version = params["version"]
        policy = _policy_for_source(params.get("source", "github"))
        materials = _verified_materials(self._resolver_for(policy), version)
        manifest = materials.manifest
        switch = plan_switch(self._context(), manifest, updater_version=__version__)
        binding = self._release_binding(materials, policy)
        stored = self.plans.create(
            manifest,
            switch.as_dict(),
            release_binding=binding,
        )
        current = self.slots.read()["current"]
        return {
            "planId": stored["id"],
            "expiresAt": stored["expiresAt"],
            "from": self._identity(current),
            "to": self._identity(manifest),
            "compatibility": switch.as_dict(),
            "affectedServices": ["api", "web"],
            "databaseRollback": False,
            "source": binding["source"],
            "transportPolicyIdentity": binding["transportPolicyIdentity"],
            "verifiedReleaseIdentity": binding["verifiedReleaseIdentity"],
        }

    def _run_apply(self, operation_id, manifest, lock_lease):
        try:
            self.executor.apply(operation_id, manifest, lock_held=True)
        except Exception as error:  # noqa: BLE001 - worker failures must become durable operation state
            operation = self.operations.get(operation_id)
            if operation["status"] in TERMINAL_STATES:
                return
            target = "failed_pre_switch" if operation["status"] in PRE_SWITCH_RECOVERY else "manual_recovery_required"
            self.operations.transition(operation_id, target, detail=f"background update worker failed: {redact(error)}")
        finally:
            lock_lease.__exit__(None, None, None)

    def _run_rollback(self, operation_id, manifest, lock_lease):
        try:
            self.executor.rollback(operation_id, manifest, lock_held=True)
        except Exception as error:  # noqa: BLE001 - worker failures must become durable operation state
            operation = self.operations.get(operation_id)
            if operation["status"] in TERMINAL_STATES:
                return
            target = "failed_pre_switch" if operation["status"] in PRE_SWITCH_RECOVERY else "manual_recovery_required"
            self.operations.transition(operation_id, target, detail=f"background rollback worker failed: {redact(error)}")
        finally:
            lock_lease.__exit__(None, None, None)

    def _apply(self, params):
        self.operations.require_recovery_clear()
        stored = self.plans.get(params["planId"])
        version = stored["manifest"]["release"]["version"]
        if params["confirmation"] != f"APPLY {version}":
            raise RequestRejected("Update confirmation does not match the planned release")
        if not stored["plan"]["allowed"]:
            raise RequestRejected("Blocked update plans cannot be applied")
        bound_resolver = self._bound_resolver(
            stored.get("releaseBinding"),
            stored["manifest"],
        )
        lock_lease = self.executor.acquire_lock()
        handed_off = False
        try:
            self.operations.require_recovery_clear()
            stored = self.plans.consume(params["planId"])
            self.executor.release_source = bound_resolver
            operation = self.operations.create(
                "apply_update",
                {
                    "version": version,
                    "planId": stored["id"],
                    "releaseBinding": stored["releaseBinding"],
                },
            )
            if self.background:
                worker = threading.Thread(
                    target=self._run_apply,
                    args=(operation["id"], stored["manifest"], lock_lease),
                    name=f"animemo-update-{operation['id'][:8]}",
                    daemon=True,
                )
                worker.start()
                handed_off = True
                return {"operation": operation}
            return {"operation": self.executor.apply(operation["id"], stored["manifest"], lock_held=True)}
        finally:
            if not handed_off:
                lock_lease.__exit__(None, None, None)

    def _rollback_previous(self):
        lock_lease = self.executor.acquire_lock()
        handed_off = False
        try:
            self.operations.require_recovery_clear()
            slots = self.slots.read()
            previous = slots["previous"]
            if previous is None:
                raise RequestRejected("PREVIOUS release is not available")
            switch = plan_switch(self._context(), previous, updater_version=__version__)
            if not switch.allowed:
                raise RequestRejected("PREVIOUS release is incompatible with the live runtime contracts")
            policy = self._current_release_policy(slots["current"])
            materials = _verified_materials(
                self._resolver_for(policy),
                previous["release"]["version"],
                refresh=True,
            )
            if materials.manifest != previous:
                raise StateError(
                    "PREVIOUS differs from the verified immutable release"
                )
            binding = self._release_binding(materials, policy)
            self.executor.release_source = self._bound_resolver(binding, previous)
            operation = self.operations.create(
                "rollback_previous",
                {
                    "version": previous["release"]["version"],
                    "releaseBinding": binding,
                },
            )
            if self.background:
                worker = threading.Thread(
                    target=self._run_rollback,
                    args=(operation["id"], previous, lock_lease),
                    name=f"animemo-rollback-{operation['id'][:8]}",
                    daemon=True,
                )
                worker.start()
                handed_off = True
                return {"operation": operation}
            return {"operation": self.executor.rollback(operation["id"], previous, lock_held=True)}
        finally:
            if not handed_off:
                lock_lease.__exit__(None, None, None)

    def dispatch(self, request: object):
        request = validate_request(request)
        operation = request["operation"]
        params = request["params"]
        if operation == "get_status":
            return self._status()
        if operation == "list_releases":
            return self._list_releases(params)
        if operation == "check_update":
            releases = self._list_releases(params)["releases"]
            current_version = self._status()["current"]["version"]
            latest = next((item for item in releases if item["version"] != current_version), None)
            return {"currentVersion": current_version, "latest": latest}
        if operation == "plan_update":
            return self._plan_update(params)
        if operation == "apply_update":
            return self._apply(params)
        if operation == "rollback_previous":
            return self._rollback_previous()
        if operation == "get_operation":
            return self.operations.get(params["operationId"])
        if operation == "get_logs":
            payload = self.operations.get(params["operationId"])
            return {"operationId": payload["id"], "events": payload["events"][-params.get("limit", 100):]}
        raise RequestRejected("Operation is not implemented")
