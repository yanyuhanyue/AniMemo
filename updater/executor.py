from __future__ import annotations

from contextlib import nullcontext

from .compatibility import DeploymentContext, plan_switch
from .errors import CompatibilityError, StateError
from .state import UpdateLock


class UpdateExecutor:
    """Execute one planned update behind a single crash-aware host lock."""

    def __init__(
        self,
        *,
        store,
        slots,
        release_source,
        deployment,
        runtime_state,
        lock_path,
        updater_version: str,
    ):
        self.store = store
        self.slots = slots
        self.release_source = release_source
        self.deployment = deployment
        self.runtime_state = runtime_state
        self.lock_path = lock_path
        self.updater_version = updater_version

    def acquire_lock(self) -> UpdateLock:
        lease = UpdateLock(self.lock_path)
        lease.__enter__()
        return lease

    def _lock_context(self, *, lock_held: bool):
        return nullcontext() if lock_held else UpdateLock(self.lock_path)

    def _context(self, current_manifest):
        runtime = self.runtime_state.read()
        return DeploymentContext(
            current_manifest=current_manifest,
            database_contract=runtime["databaseContract"],
            configuration_contract=runtime["configurationContract"],
            enabled_plugin_apis=frozenset(runtime["enabledPluginApis"]),
        )

    @staticmethod
    def _live_contracts(runtime: dict[str, object]) -> dict[str, str]:
        return {
            "databaseContract": runtime["databaseContract"],
            "configurationContract": runtime["configurationContract"],
        }

    def _can_restore(self, current) -> bool:
        return plan_switch(self._context(current), current, updater_version=self.updater_version).allowed

    def _resolve_uncertain_contracts(
        self,
        operation_id: str,
        operation: dict[str, object],
        current: dict[str, object],
    ) -> None:
        recovery = operation.get("recovery")
        if recovery is None:
            if operation.get("kind") == "apply_update":
                raise StateError("Apply recovery receipt is missing")
            return
        if not isinstance(recovery, dict):
            raise StateError("Recovery contract state is invalid")
        target = recovery.get("targetManifest")
        pending = recovery.get("pendingContractTransitions")
        if not isinstance(target, dict) or not isinstance(pending, dict):
            raise StateError("Recovery contract state is invalid")
        if not set(pending).issubset({"database", "configuration"}):
            raise StateError("Recovery contract state contains an unknown transition")
        if not pending:
            return

        release = target.get("release")
        metadata = operation.get("metadata")
        if not isinstance(release, dict) or not isinstance(metadata, dict):
            raise StateError("Recovery target identity is invalid")
        version = release.get("version")
        if not isinstance(version, str) or metadata.get("version") != version:
            raise StateError("Recovery target differs from the operation")
        verified = self.release_source.fetch_verified(
            version,
            updater_version=self.updater_version,
            refresh=True,
        )
        if verified != target:
            raise CompatibilityError("Recovery target differs from the verified immutable release")
        self.deployment.pull(target)

        database = pending.get("database")
        if database is not None:
            if (
                not isinstance(database, dict)
                or set(database) != {"before", "after"}
                or database["after"] != target["compatibility"]["database"]["contract"]
            ):
                raise StateError("Pending database contract transition is invalid")
            outcome = self.deployment.inspect_database_transition(current, target)
            if outcome == "target":
                resolved_database = database["after"]
            elif outcome == "current":
                resolved_database = database["before"]
            else:
                raise StateError("Database contract transition is indeterminate")
            runtime = self.runtime_state.read()
            if runtime["databaseContract"] not in {database["before"], database["after"]}:
                raise StateError("Durable database contract conflicts with recovery evidence")
            self.runtime_state.update(databaseContract=resolved_database)
            self.store.resolve_contract_transition(operation_id, "database")

        operation = self.store.get(operation_id)
        pending = operation.get("recovery", {}).get("pendingContractTransitions", {})
        configuration = pending.get("configuration") if isinstance(pending, dict) else None
        if configuration is not None:
            if (
                not isinstance(configuration, dict)
                or set(configuration) != {"before", "after"}
                or configuration["after"]
                != target["compatibility"]["configuration"]["contract"]
            ):
                raise StateError("Pending configuration contract transition is invalid")
            runtime = self.runtime_state.read()
            if runtime["configurationContract"] not in {
                configuration["before"],
                configuration["after"],
            }:
                raise StateError("Durable configuration contract conflicts with recovery evidence")
            plan = plan_switch(
                self._context(current),
                target,
                updater_version=self.updater_version,
            )
            if not plan.allowed:
                raise CompatibilityError(",".join(plan.reasons))
            self.deployment.bootstrap(target)
            target_plugins = sorted(self.deployment.inspect_enabled_plugin_apis(target))
            self.runtime_state.update(
                configurationContract=configuration["after"],
                enabledPluginApis=target_plugins,
            )
            post_bootstrap_plan = plan_switch(
                self._context(current),
                target,
                updater_version=self.updater_version,
            )
            if not post_bootstrap_plan.allowed:
                raise CompatibilityError(",".join(post_bootstrap_plan.reasons))
            self.store.resolve_contract_transition(operation_id, "configuration")

        remaining = self.store.get(operation_id).get("recovery", {}).get(
            "pendingContractTransitions",
        )
        if remaining != {}:
            raise StateError("Recovery contract transition remains unresolved")

    def reconcile(self, operation_id: str) -> dict[str, object]:
        with UpdateLock(self.lock_path):
            operation = self.store.get(operation_id)
            if operation["status"] != "manual_recovery_required":
                raise StateError("Only an unresolved manual recovery operation can be reconciled")
            current = self.slots.read()["current"]
            if current is None:
                raise StateError("CURRENT release identity is not initialized")

            self._resolve_uncertain_contracts(operation_id, operation, current)
            runtime = self.runtime_state.read()
            plugins = sorted(self.deployment.inspect_enabled_plugin_apis(current))
            plan = plan_switch(
                DeploymentContext(
                    current_manifest=current,
                    database_contract=runtime["databaseContract"],
                    configuration_contract=runtime["configurationContract"],
                    enabled_plugin_apis=frozenset(plugins),
                ),
                current,
                updater_version=self.updater_version,
            )
            if not plan.allowed:
                raise CompatibilityError(",".join(plan.reasons))

            live_contracts = self._live_contracts(runtime)
            self.deployment.switch(current, live_contracts=live_contracts)
            self.deployment.verify_health(current, live_contracts=live_contracts)
            inspected_contracts = self.deployment.inspect_runtime_contracts(current)
            plugins = sorted(self.deployment.inspect_enabled_plugin_apis(current))
            if (
                not isinstance(inspected_contracts, dict)
                or inspected_contracts != live_contracts
            ):
                raise StateError("CURRENT does not report the authoritative live contracts")
            runtime = {**runtime, "enabledPluginApis": plugins}
            context = DeploymentContext(
                current_manifest=current,
                database_contract=runtime["databaseContract"],
                configuration_contract=runtime["configurationContract"],
                enabled_plugin_apis=frozenset(plugins),
            )
            plan = plan_switch(context, current, updater_version=self.updater_version)
            if not plan.allowed:
                raise CompatibilityError(",".join(plan.reasons))
            self.runtime_state.write(runtime)
            return self.store.transition(
                operation_id,
                "reconciled",
                detail="host recreated and verified CURRENT against authoritative live contracts",
            )

    def _rollback_after_switch(self, operation_id, current) -> None:
        if not self._can_restore(current):
            self.store.transition(
                operation_id,
                "manual_recovery_required",
                detail="previous application rejects the current live contracts",
            )
            return
        try:
            self.store.transition(operation_id, "rolling_back", detail="restoring previous API and Web images")
            runtime = self.runtime_state.read()
            live_contracts = self._live_contracts(runtime)
            self.deployment.switch(current, live_contracts=live_contracts)
            self.deployment.verify_health(current, live_contracts=live_contracts)
            current_plugins = sorted(self.deployment.inspect_enabled_plugin_apis(current))
            self.runtime_state.update(enabledPluginApis=current_plugins)
            self.store.transition(
                operation_id,
                "rolled_back",
                detail="previous application restored; live data contracts retained",
            )
        except Exception as error:  # noqa: BLE001 - every switch failure must enter recovery state
            self.store.transition(operation_id, "manual_recovery_required", detail=f"application rollback failed: {error}")

    def apply(
        self,
        operation_id: str,
        target_manifest: dict[str, object],
        *,
        lock_held: bool = False,
    ) -> dict[str, object]:
        with self._lock_context(lock_held=lock_held):
            self.store.require_recovery_clear()
            current = self.slots.read()["current"]
            if current is None:
                raise CompatibilityError("CURRENT release identity is not initialized")

            switched = False
            try:
                self.store.transition(operation_id, "preflight", detail="checking fixed AniMemo host resources")
                self.deployment.preflight(target_manifest)
                self.store.transition(operation_id, "fetching", detail="loading exact GitHub release assets")
                verified_manifest = self.release_source.fetch_verified(
                    target_manifest["release"]["version"],
                    updater_version=self.updater_version,
                    refresh=True,
                )
                self.store.transition(operation_id, "verifying", detail="validating release identity and provenance")
                if verified_manifest != target_manifest:
                    raise CompatibilityError("Verified release differs from the planned immutable manifest")
                target_manifest = verified_manifest
                self.store.bind_recovery_target(operation_id, target_manifest)
                current_plugins = sorted(self.deployment.inspect_enabled_plugin_apis(current))
                self.runtime_state.update(enabledPluginApis=current_plugins)
                plan = plan_switch(self._context(current), target_manifest, updater_version=self.updater_version)
                if not plan.allowed:
                    raise CompatibilityError(",".join(plan.reasons))

                if plan.migration_required:
                    self.store.transition(operation_id, "backup", detail="creating a fresh verified database backup")
                    self.deployment.backup_database(operation_id)
                    self.deployment.verify_recent_backup()
                else:
                    self.deployment.verify_recent_backup()

                self.store.transition(operation_id, "pulling", detail="pulling exact API and Web image digests")
                self.deployment.pull(target_manifest)
                if plan.migration_required:
                    runtime = self.runtime_state.read()
                    self.store.mark_contract_transition_pending(
                        operation_id,
                        "database",
                        before=runtime["databaseContract"],
                        after=target_manifest["compatibility"]["database"]["contract"],
                    )
                    self.store.transition(operation_id, "migrating", detail="running target API image migration")
                    self.deployment.migrate(target_manifest)
                    self.runtime_state.update(
                        databaseContract=target_manifest["compatibility"]["database"]["contract"]
                    )
                    self.store.resolve_contract_transition(operation_id, "database")
                runtime = self.runtime_state.read()
                self.store.mark_contract_transition_pending(
                    operation_id,
                    "configuration",
                    before=runtime["configurationContract"],
                    after=target_manifest["compatibility"]["configuration"]["contract"],
                )
                self.store.transition(operation_id, "bootstrapping", detail="applying idempotent target bootstrap state")
                self.deployment.bootstrap(target_manifest)
                self.runtime_state.update(
                    configurationContract=target_manifest["compatibility"]["configuration"]["contract"]
                )
                target_plugins = sorted(self.deployment.inspect_enabled_plugin_apis(target_manifest))
                self.runtime_state.update(enabledPluginApis=target_plugins)
                self.store.resolve_contract_transition(operation_id, "configuration")
                post_bootstrap_plan = plan_switch(
                    self._context(current),
                    target_manifest,
                    updater_version=self.updater_version,
                )
                if not post_bootstrap_plan.allowed:
                    raise CompatibilityError(",".join(post_bootstrap_plan.reasons))

                self.store.transition(operation_id, "switching", detail="replacing only AniMemo API and Web")
                live_contracts = self._live_contracts(self.runtime_state.read())
                self.deployment.switch(target_manifest, live_contracts=live_contracts)
                switched = True
                self.store.transition(operation_id, "verifying_health", detail="observing target health and restart stability")
                self.deployment.verify_health(target_manifest, live_contracts=live_contracts)
                self.slots.promote(target_manifest, operation_id=operation_id)
                return self.store.transition(operation_id, "succeeded", detail="release switch completed")
            except CompatibilityError as error:
                status = self.store.get(operation_id)["status"]
                target = "failed_pre_switch" if status in {"idle", "preflight", "fetching", "verifying", "backup", "pulling"} else "manual_recovery_required"
                return self.store.transition(operation_id, target, detail=str(error))
            except Exception as error:  # noqa: BLE001 - every execution failure must be journaled
                status = self.store.get(operation_id)["status"]
                if status in {"migrating", "bootstrapping"}:
                    return self.store.transition(operation_id, "manual_recovery_required", detail=f"migration or bootstrap failed; database was not reversed: {error}")
                if switched or status in {"switching", "verifying_health"}:
                    self._rollback_after_switch(operation_id, current)
                    return self.store.get(operation_id)
                return self.store.transition(operation_id, "failed_pre_switch", detail=str(error))

    def rollback(
        self,
        operation_id: str,
        previous_manifest: dict[str, object],
        *,
        lock_held: bool = False,
    ) -> dict[str, object]:
        with self._lock_context(lock_held=lock_held):
            self.store.require_recovery_clear()
            current = self.slots.read()["current"]
            if current is None:
                raise CompatibilityError("CURRENT release identity is not initialized")
            try:
                self.store.transition(operation_id, "preflight", detail="checking application rollback compatibility")
                self.deployment.preflight(previous_manifest)
                self.store.transition(operation_id, "fetching", detail="loading PREVIOUS immutable release")
                verified_manifest = self.release_source.fetch_verified(
                    previous_manifest["release"]["version"],
                    updater_version=self.updater_version,
                    refresh=True,
                )
                self.store.transition(operation_id, "verifying", detail="validating PREVIOUS against live runtime contracts")
                if verified_manifest != previous_manifest:
                    raise CompatibilityError("Verified PREVIOUS differs from the stored immutable manifest")
                previous_manifest = verified_manifest
                current_plugins = sorted(self.deployment.inspect_enabled_plugin_apis(current))
                self.runtime_state.update(enabledPluginApis=current_plugins)
                plan = plan_switch(self._context(current), previous_manifest, updater_version=self.updater_version)
                if not plan.allowed:
                    raise CompatibilityError(",".join(plan.reasons))
                self.deployment.verify_recent_backup()
                self.store.transition(operation_id, "pulling", detail="pulling PREVIOUS API and Web image digests")
                self.deployment.pull(previous_manifest)
                self.store.transition(operation_id, "switching", detail="replacing only AniMemo API and Web")
                runtime = self.runtime_state.read()
                live_contracts = self._live_contracts(runtime)
                self.deployment.switch(previous_manifest, live_contracts=live_contracts)
                self.store.transition(operation_id, "verifying_health", detail="observing rollback health")
                self.deployment.verify_health(previous_manifest, live_contracts=live_contracts)
                previous_plugins = sorted(
                    self.deployment.inspect_enabled_plugin_apis(previous_manifest)
                )
                self.runtime_state.update(enabledPluginApis=previous_plugins)
                self.slots.restore_previous(operation_id=operation_id)
                return self.store.transition(
                    operation_id,
                    "rolled_back",
                    detail="application rollback completed; live data contracts retained",
                )
            except CompatibilityError as error:
                return self.store.transition(operation_id, "failed_pre_switch", detail=str(error))
            except Exception as error:  # noqa: BLE001 - every execution failure must be journaled
                status = self.store.get(operation_id)["status"]
                if status in {"switching", "verifying_health"}:
                    try:
                        self.store.transition(operation_id, "rolling_back", detail="restoring pre-rollback current application")
                        runtime = self.runtime_state.read()
                        live_contracts = self._live_contracts(runtime)
                        self.deployment.switch(current, live_contracts=live_contracts)
                        self.deployment.verify_health(current, live_contracts=live_contracts)
                        current_plugins = sorted(
                            self.deployment.inspect_enabled_plugin_apis(current)
                        )
                        self.runtime_state.update(enabledPluginApis=current_plugins)
                        return self.store.transition(operation_id, "rolled_back", detail="rollback attempt reverted to current application")
                    except Exception as rollback_error:  # noqa: BLE001 - failed recovery requires manual state
                        return self.store.transition(operation_id, "manual_recovery_required", detail=f"rollback recovery failed: {rollback_error}")
                return self.store.transition(operation_id, "failed_pre_switch", detail=str(error))
