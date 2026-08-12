from __future__ import annotations

from contextlib import nullcontext

from .compatibility import DeploymentContext, plan_switch
from .errors import CompatibilityError
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

    def _can_restore(self, current) -> bool:
        return plan_switch(self._context(current), current, updater_version=self.updater_version).allowed

    def _rollback_after_switch(self, operation_id, current, target, *, migration_applied: bool) -> None:
        if not self._can_restore(current):
            self.store.transition(operation_id, "manual_recovery_required", detail="previous application rejects the current database contract")
            return
        try:
            self.store.transition(operation_id, "rolling_back", detail="restoring previous API and Web images")
            self.deployment.switch(current)
            self.deployment.verify_health(current)
            self.store.transition(operation_id, "rolled_back", detail="previous application restored; database retained")
        except Exception as error:
            self.store.transition(operation_id, "manual_recovery_required", detail=f"application rollback failed: {error}")

    def apply(
        self,
        operation_id: str,
        target_manifest: dict[str, object],
        *,
        lock_held: bool = False,
    ) -> dict[str, object]:
        with self._lock_context(lock_held=lock_held):
            current = self.slots.read()["current"]
            if current is None:
                raise CompatibilityError("CURRENT release identity is not initialized")

            migrated = False
            switched = False
            try:
                self.store.transition(operation_id, "preflight", detail="checking fixed AniMemo host resources")
                self.deployment.preflight(target_manifest)
                self.store.transition(operation_id, "fetching", detail="loading exact GitHub release assets")
                self.store.transition(operation_id, "verifying", detail="validating release identity and provenance")
                current_plugins = sorted(self.deployment.inspect_enabled_plugin_apis(current))
                self.runtime_state.update(enabledPluginApis=current_plugins)
                plan = plan_switch(self._context(current), target_manifest, updater_version=self.updater_version)
                if not plan.allowed:
                    raise CompatibilityError(",".join(plan.reasons))

                if plan.migration_required:
                    self.store.transition(operation_id, "backup", detail="creating a fresh verified database backup")
                    self.deployment.backup_database(operation_id)
                else:
                    self.deployment.verify_recent_backup()

                self.store.transition(operation_id, "pulling", detail="pulling exact API and Web image digests")
                self.deployment.pull(target_manifest)
                if plan.migration_required:
                    self.store.transition(operation_id, "migrating", detail="running target API image migration")
                    self.deployment.migrate(target_manifest)
                    migrated = True
                    self.runtime_state.update(
                        databaseContract=target_manifest["compatibility"]["database"]["contract"]
                    )
                self.deployment.bootstrap(target_manifest)
                self.runtime_state.update(
                    enabledPluginApis=sorted(self.deployment.inspect_enabled_plugin_apis(target_manifest))
                )

                self.store.transition(operation_id, "switching", detail="replacing only AniMemo API and Web")
                self.deployment.switch(target_manifest)
                switched = True
                self.store.transition(operation_id, "verifying_health", detail="observing target health and restart stability")
                self.deployment.verify_health(target_manifest)
                self.slots.promote(target_manifest, operation_id=operation_id)
                return self.store.transition(operation_id, "succeeded", detail="release switch completed")
            except CompatibilityError as error:
                return self.store.transition(operation_id, "failed_pre_switch", detail=str(error))
            except Exception as error:
                status = self.store.get(operation_id)["status"]
                if status == "migrating":
                    return self.store.transition(operation_id, "manual_recovery_required", detail=f"migration failed; database was not reversed: {error}")
                if switched or status in {"switching", "verifying_health"}:
                    self._rollback_after_switch(operation_id, current, target_manifest, migration_applied=migrated)
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
            current = self.slots.read()["current"]
            if current is None:
                raise CompatibilityError("CURRENT release identity is not initialized")
            try:
                self.store.transition(operation_id, "preflight", detail="checking application rollback compatibility")
                self.deployment.preflight(previous_manifest)
                self.store.transition(operation_id, "fetching", detail="loading PREVIOUS immutable release")
                self.store.transition(operation_id, "verifying", detail="validating PREVIOUS against live runtime contracts")
                plan = plan_switch(self._context(current), previous_manifest, updater_version=self.updater_version)
                if not plan.allowed:
                    raise CompatibilityError(",".join(plan.reasons))
                self.deployment.verify_recent_backup()
                self.store.transition(operation_id, "pulling", detail="pulling PREVIOUS API and Web image digests")
                self.deployment.pull(previous_manifest)
                self.store.transition(operation_id, "switching", detail="replacing only AniMemo API and Web")
                self.deployment.switch(previous_manifest)
                self.store.transition(operation_id, "verifying_health", detail="observing rollback health")
                self.deployment.verify_health(previous_manifest)
                self.slots.restore_previous(operation_id=operation_id)
                return self.store.transition(operation_id, "rolled_back", detail="application rollback completed; database retained")
            except CompatibilityError as error:
                return self.store.transition(operation_id, "failed_pre_switch", detail=str(error))
            except Exception as error:
                status = self.store.get(operation_id)["status"]
                if status in {"switching", "verifying_health"}:
                    try:
                        self.store.transition(operation_id, "rolling_back", detail="restoring pre-rollback current application")
                        self.deployment.switch(current)
                        self.deployment.verify_health(current)
                        return self.store.transition(operation_id, "rolled_back", detail="rollback attempt reverted to current application")
                    except Exception as rollback_error:
                        return self.store.transition(operation_id, "manual_recovery_required", detail=f"rollback recovery failed: {rollback_error}")
                return self.store.transition(operation_id, "failed_pre_switch", detail=str(error))
