from __future__ import annotations

import threading
import time

from . import __version__
from .compatibility import DeploymentContext, plan_switch
from .errors import RequestRejected
from .protocol import validate_request
from .redaction import redact
from .state import PRE_SWITCH_RECOVERY, TERMINAL_STATES


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
    ):
        self.source = source
        self.operations = operations
        self.plans = plans
        self.slots = slots
        self.runtime_state = runtime_state
        self.executor = executor
        self.background = background
        self.runtime_refresh_seconds = runtime_refresh_seconds
        self._runtime_refreshed_at = 0.0

    def recover(self) -> list[str]:
        return self.operations.recover_incomplete()

    def _context(self) -> DeploymentContext:
        slots = self.slots.read()
        current = slots["current"]
        if current is None:
            raise RequestRejected("CURRENT release identity is not initialized")
        runtime = self._refresh_enabled_plugin_apis(current)
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
        context = self._context()
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
        current = self._context()
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

    def _plan_update(self, version: str):
        manifest = self.source.fetch_verified(version, updater_version=__version__)
        switch = plan_switch(self._context(), manifest, updater_version=__version__)
        stored = self.plans.create(manifest, switch.as_dict())
        current = self.slots.read()["current"]
        return {
            "planId": stored["id"],
            "expiresAt": stored["expiresAt"],
            "from": self._identity(current),
            "to": self._identity(manifest),
            "compatibility": switch.as_dict(),
            "affectedServices": ["api", "web"],
            "databaseRollback": False,
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
        stored = self.plans.get(params["planId"])
        version = stored["manifest"]["release"]["version"]
        if params["confirmation"] != f"APPLY {version}":
            raise RequestRejected("Update confirmation does not match the planned release")
        if not stored["plan"]["allowed"]:
            raise RequestRejected("Blocked update plans cannot be applied")
        lock_lease = self.executor.acquire_lock()
        handed_off = False
        try:
            stored = self.plans.consume(params["planId"])
            operation = self.operations.create("apply_update", {"version": version, "planId": stored["id"]})
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
            slots = self.slots.read()
            previous = slots["previous"]
            if previous is None:
                raise RequestRejected("PREVIOUS release is not available")
            switch = plan_switch(self._context(), previous, updater_version=__version__)
            if not switch.allowed:
                raise RequestRejected("PREVIOUS release is incompatible with the live runtime contracts")
            operation = self.operations.create("rollback_previous", {"version": previous["release"]["version"]})
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
            return self._plan_update(params["version"])
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
