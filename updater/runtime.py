from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from release.contract import ReleaseContractError, validate_manifest

from . import __version__
from .agent import UpdateAgent
from .deployment import HostPaths, ImmutableComposeDeployment
from .errors import StateError
from .executor import UpdateExecutor
from .plans import PlanStore
from .runtime_state import RuntimeState
from .server import UnixRpcServer
from .slots import ReleaseSlots
from .source import GitHubReleaseSource
from .state import OperationStore


PRODUCTION_SOCKET_PATH = Path("/run/animemo-updater/updater.sock")
PRODUCTION_BOOTSTRAP_MANIFEST = Path("/var/lib/animemo-updater/bootstrap/release-manifest.json")


@dataclass
class HostAgentRuntime:
    """Compose the fixed host Agent behind a tiny lifecycle interface."""

    paths: HostPaths
    socket_path: Path
    bootstrap_manifest: Path
    slots: ReleaseSlots
    runtime_state: RuntimeState
    agent: UpdateAgent
    server: UnixRpcServer

    @classmethod
    def _build(
        cls,
        *,
        paths: HostPaths,
        socket_path: Path,
        bootstrap_manifest: Path,
        background: bool = True,
    ) -> "HostAgentRuntime":
        state_root = paths.state_root
        slots = ReleaseSlots(state_root / "releases")
        runtime_state = RuntimeState(state_root)
        operations = OperationStore(state_root)
        source = GitHubReleaseSource(state_root / "cache" / "github-releases")
        deployment = ImmutableComposeDeployment(paths)
        executor = UpdateExecutor(
            store=operations,
            slots=slots,
            release_source=source,
            deployment=deployment,
            runtime_state=runtime_state,
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
        )
        server = UnixRpcServer(socket_path, agent)
        return cls(
            paths=paths,
            socket_path=socket_path,
            bootstrap_manifest=bootstrap_manifest,
            slots=slots,
            runtime_state=runtime_state,
            agent=agent,
            server=server,
        )

    @classmethod
    def production(cls) -> "HostAgentRuntime":
        return cls._build(
            paths=HostPaths.production(),
            socket_path=PRODUCTION_SOCKET_PATH,
            bootstrap_manifest=PRODUCTION_BOOTSTRAP_MANIFEST,
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
    ) -> "HostAgentRuntime":
        return cls._build(
            paths=HostPaths.testing(app=app_root, data=data_root, state=state_root),
            socket_path=socket_path.resolve(),
            bootstrap_manifest=bootstrap_manifest.resolve(),
            background=background,
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

    def import_current(self) -> dict[str, object]:
        try:
            manifest = json.loads(self.bootstrap_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("Fixed bootstrap release manifest is unavailable or invalid") from error
        try:
            validate_manifest(manifest, updater_version=__version__)
        except ReleaseContractError as error:
            raise StateError(f"Fixed bootstrap release manifest failed validation: {error}") from error

        current = self.slots.read()["current"]
        runtime_exists = self.runtime_state.path.exists()
        if current is not None and runtime_exists:
            raise StateError("CURRENT is already initialized; bootstrap import is one-time")
        if current is None and runtime_exists:
            raise StateError("Bootstrap state is inconsistent: runtime exists without CURRENT")
        if current is not None:
            if current != manifest:
                raise StateError("Bootstrap manifest conflicts with partially imported CURRENT")
            self.runtime_state.initialize_from_manifest(manifest)
            return self._identity(manifest)

        self.slots.import_current(manifest)
        self.runtime_state.initialize_from_manifest(manifest)
        return self._identity(manifest)

    def status(self) -> dict[str, object]:
        return self.agent.dispatch({"operation": "get_status", "params": {}})

    def serve_forever(self) -> None:
        self.server.serve_forever()


def production_runtime() -> HostAgentRuntime:
    return HostAgentRuntime.production()
