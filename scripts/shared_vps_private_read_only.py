"""Fail-closed shared-host private read-only acceptance boundary.

The repository intentionally does not provide an SSH implementation that would
materialize a private key.  Production must inject a separately reviewed,
digest-bound memory-only transport capability.  No production capability issuer
exists in this module yet.  Without one, verification is unavailable rather
than silently falling back to ambient OpenSSH state.

The mutable lease guarantees erasure of the buffer it owns; it cannot prevent
an untrusted same-process object from copying bytes.  That is why raw transport
objects are rejected and the future capability issuer is part of the security
boundary, not a convenience adapter.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from release.acceptance import AcceptanceError, validate_rc_live_acceptance
from release.candidate import canonical_json_bytes, sha256_bytes

SHARED_VPS_ACCESS = "PRIVATE_READ_ONLY"
SHARED_VPS_ACCEPTANCE_ROOT = "/opt/animemo-v1.1/acceptance"
SHARED_VPS_RELEASE_TAG = "v1.1.0-rc.19"
SHARED_VPS_MAX_RETRIES_PER_FAILURE_ROOT = 10
SHARED_VPS_ALLOWED_FILES = ("SHA256SUMS", "shared-vps-authority.json")
SHARED_VPS_REMOTE_COMMAND = "animemo-v1.1-acceptance-read-v1"
SHARED_VPS_SSH_USER = "animemo-acceptance-ro"
SHARED_VPS_PROHIBITED_OPERATIONS = (
    "V1_0_OR_OTHER_SITE_ACCESS",
    "DNS_OR_CLOUDFLARE_ACCESS",
    "FIREWALL_ACCESS",
    "OPENRESTY_ACCESS",
    "HOST_MUTATION",
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_REMOTE_AUTHORITY_FIELDS = {
    "api_digest",
    "deployment_contract_identity",
    "formal_acceptance_identity",
    "formal_aggregate_receipt_digest",
    "formal_execution_receipt_digest",
    "installer_materials_identity",
    "publication_identity",
    "rc_tag",
    "release_manifest_identity",
    "schema",
    "web_digest",
}
_MAX_REMOTE_FILE_BYTES = 64 * 1024
_CAPABILITY_ISSUANCE_TOKEN = object()
_CONTROLLER_LIFETIME_ISSUANCE_TOKEN = object()
_SAFE_ERROR_CODE = re.compile(r"^SHARED_VPS_[A-Z0-9_]+$")


class SharedVpsPrivateReadOnlyError(RuntimeError):
    """Secret-free fixed-code verifier failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SharedVpsPrivateKeyLease:
    """One-probe, non-serializable mutable private-key copy."""

    __slots__ = ("_borrow_count", "_buffer", "_cleared")

    def __init__(self, value: bytearray) -> None:
        if type(value) is not bytearray or not value or len(value) > 64 * 1024:
            raise SharedVpsPrivateReadOnlyError("SHARED_VPS_PRIVATE_KEY_LEASE_INVALID")
        self._buffer = bytearray(value)
        self._borrow_count = 0
        self._cleared = False

    def __repr__(self) -> str:
        return "SharedVpsPrivateKeyLease(<redacted>)"

    def __reduce__(self) -> object:
        raise TypeError("SharedVpsPrivateKeyLease cannot be serialized")

    def read_once(self) -> memoryview:
        if self._borrow_count != 0 or self._cleared:
            raise SharedVpsPrivateReadOnlyError(
                "SHARED_VPS_PRIVATE_KEY_LEASE_UNAVAILABLE"
            )
        self._borrow_count = 1
        return memoryview(self._buffer).toreadonly()

    def clear(self) -> None:
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._cleared = True

    @property
    def cleared(self) -> bool:
        return self._cleared

    @property
    def borrow_count(self) -> int:
        return self._borrow_count


@dataclass(frozen=True, eq=False, slots=True)
class SharedVpsImmutablePlanAuthority:
    """Externally supplied immutable controller-plan authority."""

    schema: str
    endpoint_host: str
    endpoint_port: int
    host_key_authority_identity: str
    transport_authority_identity: str
    helper_binary_identity: str
    forced_command_policy_identity: str
    identity: str


def _is_valid_endpoint_host(value: object) -> bool:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 253
        or value != value.strip()
    ):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        return all(_HOST_LABEL.fullmatch(label) is not None for label in labels)
    return True


def _validate_immutable_plan_authority(
    value: object,
) -> SharedVpsImmutablePlanAuthority:
    _require(
        type(value) is SharedVpsImmutablePlanAuthority,
        "SHARED_VPS_IMMUTABLE_PLAN_AUTHORITY_INVALID",
    )
    assert isinstance(value, SharedVpsImmutablePlanAuthority)
    unsigned = {
        "schema": value.schema,
        "endpoint_host": value.endpoint_host,
        "endpoint_port": value.endpoint_port,
        "host_key_authority_identity": value.host_key_authority_identity,
        "transport_authority_identity": value.transport_authority_identity,
        "helper_binary_identity": value.helper_binary_identity,
        "forced_command_policy_identity": value.forced_command_policy_identity,
    }
    _require(
        value.schema == "animemo.shared-vps-immutable-plan-authority/v1"
        and _is_valid_endpoint_host(value.endpoint_host)
        and type(value.endpoint_port) is int
        and 1 <= value.endpoint_port <= 65535
        and all(
            type(item) is str and _DIGEST.fullmatch(item) is not None
            for name, item in unsigned.items()
            if name
            not in {
                "schema",
                "endpoint_host",
                "endpoint_port",
            }
        )
        and type(value.schema) is str
        and type(value.identity) is str
        and value.identity == sha256_bytes(canonical_json_bytes(unsigned)),
        "SHARED_VPS_IMMUTABLE_PLAN_AUTHORITY_INVALID",
    )
    return value


class SharedVpsRetryLedger:
    """Parent-owned in-memory retry authority for one controller lifetime."""

    __slots__ = ("_lock", "_probe_states")

    def __init__(self) -> None:
        self._lock = Lock()
        self._probe_states: dict[str, dict[str, object]] = {}

    def __reduce__(self) -> object:
        raise TypeError("SharedVpsRetryLedger cannot be serialized")

    def begin_probe_attempt(self, probe_authority_identity: str) -> int:
        if (
            type(probe_authority_identity) is not str
            or _DIGEST.fullmatch(probe_authority_identity) is None
        ):
            raise SharedVpsPrivateReadOnlyError("SHARED_VPS_RETRY_AUTHORITY_INVALID")
        with self._lock:
            state = self._probe_states.setdefault(
                probe_authority_identity,
                {
                    "closed": False,
                    "in_flight": False,
                    "last_failure_root": None,
                    "retry_counts": {},
                },
            )
            if state["closed"] or state["in_flight"]:
                raise SharedVpsPrivateReadOnlyError("SHARED_VPS_RETRY_SEQUENCE_INVALID")
            last_root = state["last_failure_root"]
            retry_attempt = 0
            if last_root is not None:
                retry_counts = state["retry_counts"]
                assert isinstance(retry_counts, dict)
                retry_attempt = retry_counts.get(last_root, 0) + 1
                if retry_attempt > SHARED_VPS_MAX_RETRIES_PER_FAILURE_ROOT:
                    raise SharedVpsPrivateReadOnlyError(
                        "SHARED_VPS_RETRY_LIMIT_EXHAUSTED"
                    )
                retry_counts[last_root] = retry_attempt
            state["in_flight"] = True
            return retry_attempt

    def record_probe_failure(
        self, probe_authority_identity: str, error_code: str
    ) -> str:
        with self._lock:
            state = self._probe_states.get(probe_authority_identity)
            if (
                type(error_code) is not str
                or _SAFE_ERROR_CODE.fullmatch(error_code) is None
                or state is None
                or state["in_flight"] is not True
            ):
                raise SharedVpsPrivateReadOnlyError("SHARED_VPS_RETRY_SEQUENCE_INVALID")
            failure_root = sha256_bytes(
                canonical_json_bytes(
                    {
                        "error_code": error_code,
                        "probe_authority_identity": probe_authority_identity,
                        "schema": "animemo.shared-vps-failure-root/v1",
                    }
                )
            )
            state["last_failure_root"] = failure_root
            state["in_flight"] = False
            return failure_root

    def record_probe_success(self, probe_authority_identity: str) -> None:
        with self._lock:
            state = self._probe_states.get(probe_authority_identity)
            if state is None or state["in_flight"] is not True:
                raise SharedVpsPrivateReadOnlyError("SHARED_VPS_RETRY_SEQUENCE_INVALID")
            state["closed"] = True
            state["in_flight"] = False


class SharedVpsControllerLifetimeAuthority:
    """Opaque owner of one controller lifetime's non-replaceable retry state."""

    __slots__ = (
        "_immutable_plan_authority_identity",
        "_issuance_identity",
        "_retry_ledger",
        "_sealed",
    )

    def __init__(
        self,
        *,
        immutable_plan_authority: SharedVpsImmutablePlanAuthority,
        issuance_identity: str,
        issuance_token: object,
    ) -> None:
        if issuance_token is not _CONTROLLER_LIFETIME_ISSUANCE_TOKEN:
            raise SharedVpsPrivateReadOnlyError(
                "SHARED_VPS_CONTROLLER_LIFETIME_AUTHORITY_INVALID"
            )
        plan = _validate_immutable_plan_authority(immutable_plan_authority)
        _require(
            type(issuance_identity) is str
            and _DIGEST.fullmatch(issuance_identity) is not None,
            "SHARED_VPS_CONTROLLER_LIFETIME_AUTHORITY_INVALID",
        )
        self._immutable_plan_authority_identity = plan.identity
        self._issuance_identity = issuance_identity
        self._retry_ledger = SharedVpsRetryLedger()
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("SharedVpsControllerLifetimeAuthority is immutable")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return "SharedVpsControllerLifetimeAuthority(<opaque>)"

    def __reduce__(self) -> object:
        raise TypeError("SharedVpsControllerLifetimeAuthority cannot be serialized")

    @property
    def immutable_plan_authority_identity(self) -> str:
        return self._immutable_plan_authority_identity

    @property
    def issuance_identity(self) -> str:
        return self._issuance_identity

    def begin_probe_attempt(self, probe_authority_identity: str) -> int:
        return self._retry_ledger.begin_probe_attempt(probe_authority_identity)

    def record_probe_failure(
        self, probe_authority_identity: str, error_code: str
    ) -> str:
        return self._retry_ledger.record_probe_failure(
            probe_authority_identity, error_code
        )

    def record_probe_success(self, probe_authority_identity: str) -> None:
        self._retry_ledger.record_probe_success(probe_authority_identity)


class _TestOnlySharedVpsControllerLifetimeAuthority(
    SharedVpsControllerLifetimeAuthority
):
    """Distinct controller-lifetime authority for the explicit test seam."""

    __slots__ = ()


def _issue_test_only_controller_lifetime_authority(
    immutable_plan_authority: SharedVpsImmutablePlanAuthority,
) -> _TestOnlySharedVpsControllerLifetimeAuthority:
    """Create one isolated unit-test controller lifetime."""

    plan = _validate_immutable_plan_authority(immutable_plan_authority)
    issuance_identity = sha256_bytes(
        canonical_json_bytes(
            {
                "immutable_plan_authority_identity": plan.identity,
                "nonce": secrets.token_hex(32),
                "schema": "animemo.shared-vps-controller-lifetime-issuance/v1",
            }
        )
    )
    return _TestOnlySharedVpsControllerLifetimeAuthority(
        immutable_plan_authority=plan,
        issuance_identity=issuance_identity,
        issuance_token=_CONTROLLER_LIFETIME_ISSUANCE_TOKEN,
    )


def acquire_shared_vps_private_key_lease(
    value: bytearray,
) -> SharedVpsPrivateKeyLease:
    return SharedVpsPrivateKeyLease(value)


@dataclass(frozen=True)
class SharedVpsReadOnlyTransportRequest:
    host: str
    port: int
    access: str
    allowed_read_only_path: str
    allowed_files: tuple[str, ...]
    ssh_user: str
    host_key_algorithm: str
    host_key_fingerprint: str
    host_key_authority_identity: str
    remote_command: str
    prohibited_operations: tuple[str, ...]
    formal_acceptance_identity: str
    formal_aggregate_receipt_digest: str
    formal_execution_receipt_digest: str
    transport_authority_identity: str
    helper_binary_identity: str
    forced_command_policy_identity: str


@dataclass(frozen=True)
class SharedVpsReadOnlyTransportObservation:
    host: str
    port: int
    ssh_user: str
    host_key_algorithm: str
    host_key_fingerprint: str
    host_key_authority_identity: str
    remote_command: str
    transport_authority_identity: str
    helper_binary_identity: str
    forced_command_policy_identity: str
    resolved_read_only_path: str
    closed_inventory: tuple[str, ...]
    files: dict[str, bytes]
    connection_count: int
    command_count: int
    read_only_observation_count: int
    mutation_count: int
    v1_0_access_count: int
    unrelated_site_access_count: int
    dns_or_cloudflare_access_count: int
    firewall_access_count: int
    openresty_access_count: int
    regular_file_count: int
    symlink_count: int
    path_escape_count: int


class SharedVpsReadOnlyTransport(Protocol):
    def observe(
        self,
        request: SharedVpsReadOnlyTransportRequest,
        credential: SharedVpsPrivateKeyLease,
    ) -> SharedVpsReadOnlyTransportObservation: ...


class SharedVpsReadOnlyTransportCapability:
    """Module-issued transport authority; raw Protocol objects are rejected."""

    __slots__ = (
        "_authority_identity",
        "_controller_lifetime_authority",
        "_forced_command_policy_identity",
        "_helper_binary_identity",
        "_immutable_plan_authority_identity",
        "_sealed",
        "_transport",
    )

    def __init__(
        self,
        *,
        transport: SharedVpsReadOnlyTransport,
        immutable_plan_authority: SharedVpsImmutablePlanAuthority,
        controller_lifetime_authority: SharedVpsControllerLifetimeAuthority,
        issuance_token: object,
    ) -> None:
        if issuance_token is not _CAPABILITY_ISSUANCE_TOKEN or not callable(
            getattr(transport, "observe", None)
        ):
            raise SharedVpsPrivateReadOnlyError(
                "SHARED_VPS_TRANSPORT_CAPABILITY_INVALID"
            )
        plan = _validate_immutable_plan_authority(immutable_plan_authority)
        expected_lifetime_type = (
            _TestOnlySharedVpsControllerLifetimeAuthority
            if type(self) is _TestOnlySharedVpsReadOnlyTransportCapability
            else SharedVpsControllerLifetimeAuthority
        )
        _require(
            type(controller_lifetime_authority) is expected_lifetime_type
            and controller_lifetime_authority.immutable_plan_authority_identity
            == plan.identity,
            "SHARED_VPS_CONTROLLER_LIFETIME_AUTHORITY_INVALID",
        )
        self._transport = transport
        self._controller_lifetime_authority = controller_lifetime_authority
        self._immutable_plan_authority_identity = plan.identity
        self._authority_identity = plan.transport_authority_identity
        self._helper_binary_identity = plan.helper_binary_identity
        self._forced_command_policy_identity = plan.forced_command_policy_identity
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("SharedVpsReadOnlyTransportCapability is immutable")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return "SharedVpsReadOnlyTransportCapability(<opaque>)"

    def __reduce__(self) -> object:
        raise TypeError("SharedVpsReadOnlyTransportCapability cannot be serialized")

    @property
    def authority_identity(self) -> str:
        return self._authority_identity

    @property
    def helper_binary_identity(self) -> str:
        return self._helper_binary_identity

    @property
    def forced_command_policy_identity(self) -> str:
        return self._forced_command_policy_identity

    @property
    def immutable_plan_authority_identity(self) -> str:
        return self._immutable_plan_authority_identity

    @property
    def controller_lifetime_issuance_identity(self) -> str:
        return self._controller_lifetime_authority.issuance_identity

    @property
    def controller_lifetime_immutable_plan_authority_identity(self) -> str:
        return self._controller_lifetime_authority.immutable_plan_authority_identity

    def observe(
        self,
        request: SharedVpsReadOnlyTransportRequest,
        credential: SharedVpsPrivateKeyLease,
    ) -> SharedVpsReadOnlyTransportObservation:
        return self._transport.observe(request, credential)

    def begin_probe_attempt(self, probe_authority_identity: str) -> int:
        return self._controller_lifetime_authority.begin_probe_attempt(
            probe_authority_identity
        )

    def record_probe_failure(
        self, probe_authority_identity: str, error_code: str
    ) -> str:
        return self._controller_lifetime_authority.record_probe_failure(
            probe_authority_identity, error_code
        )

    def record_probe_success(self, probe_authority_identity: str) -> None:
        self._controller_lifetime_authority.record_probe_success(
            probe_authority_identity
        )


class _TestOnlySharedVpsReadOnlyTransportCapability(
    SharedVpsReadOnlyTransportCapability
):
    """Distinct capability type which production rejects exactly."""

    __slots__ = ()


def _issue_test_only_transport_capability(
    transport: SharedVpsReadOnlyTransport,
    *,
    immutable_plan_authority: SharedVpsImmutablePlanAuthority,
    controller_lifetime_authority: _TestOnlySharedVpsControllerLifetimeAuthority,
) -> _TestOnlySharedVpsReadOnlyTransportCapability:
    """Issue only the explicit unit-test seam; production has no issuer yet."""

    return _TestOnlySharedVpsReadOnlyTransportCapability(
        transport=transport,
        immutable_plan_authority=immutable_plan_authority,
        controller_lifetime_authority=controller_lifetime_authority,
        issuance_token=_CAPABILITY_ISSUANCE_TOKEN,
    )


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SharedVpsPrivateReadOnlyError(code)


def _closed_formal_acceptance(value: object) -> dict[str, object]:
    try:
        closed = validate_rc_live_acceptance(value)
    except (AcceptanceError, KeyError, TypeError, ValueError) as error:
        raise SharedVpsPrivateReadOnlyError(
            "SHARED_VPS_FORMAL_ACCEPTANCE_INVALID"
        ) from error
    _require(
        type(closed) is dict
        and closed.get("doctor_result") == "PASS"
        and closed.get("upgrade_result") in {"PASS", "NOT_APPLICABLE"}
        and type(closed.get("formal_evidence")) is dict,
        "SHARED_VPS_FORMAL_ACCEPTANCE_INVALID",
    )
    return closed


def shared_vps_host_key_authority_identity(
    *,
    host: str,
    port: int,
    ssh_user: str,
    host_key_algorithm: str,
    host_key_fingerprint: str,
) -> str:
    """Canonical identity which must itself be bound by the immutable plan."""

    return sha256_bytes(
        canonical_json_bytes(
            {
                "host": host,
                "host_key_algorithm": host_key_algorithm,
                "host_key_fingerprint": host_key_fingerprint,
                "port": port,
                "schema": "animemo.shared-vps-host-key-authority/v1",
                "ssh_user": ssh_user,
            }
        )
    )


def _formal_binding(formal: dict[str, object]) -> dict[str, str]:
    execution_receipt = formal.get("execution_receipt")
    _require(
        type(execution_receipt) is dict,
        "SHARED_VPS_FORMAL_ACCEPTANCE_INVALID",
    )
    assert isinstance(execution_receipt, dict)
    binding = {
        "formal_acceptance_identity": formal.get("identity"),
        "formal_aggregate_receipt_digest": execution_receipt.get(
            "formal_aggregate_receipt_digest"
        ),
        "formal_execution_receipt_digest": execution_receipt.get(
            "formal_execution_receipt_digest"
        ),
    }
    _require(
        all(
            type(value) is str and _DIGEST.fullmatch(value) is not None
            for value in binding.values()
        ),
        "SHARED_VPS_FORMAL_ACCEPTANCE_INVALID",
    )
    return binding  # type: ignore[return-value]


def _expected_remote_authority(formal: dict[str, object]) -> dict[str, object]:
    evidence = formal["formal_evidence"]
    _require(
        type(evidence) is dict and type(evidence.get("rcLiveAcceptanceInput")) is dict,
        "SHARED_VPS_FORMAL_ACCEPTANCE_INVALID",
    )
    acceptance_input = evidence["rcLiveAcceptanceInput"]
    assert isinstance(acceptance_input, dict)
    formal_binding = _formal_binding(formal)
    expected = {
        "api_digest": formal["api_digest"],
        "deployment_contract_identity": formal["deployment_contract_identity"],
        **formal_binding,
        "installer_materials_identity": formal["installer_materials_identity"],
        "publication_identity": acceptance_input["publication_identity"],
        "rc_tag": formal["rc_tag"],
        "release_manifest_identity": formal["release_manifest_identity"],
        "schema": "animemo.shared-vps-release-authority/v2",
        "web_digest": formal["web_digest"],
    }
    _require(
        all(
            type(value) is str
            and (name in {"rc_tag", "schema"} or _DIGEST.fullmatch(value) is not None)
            for name, value in expected.items()
        ),
        "SHARED_VPS_FORMAL_ACCEPTANCE_INVALID",
    )
    return expected


def _probe_authority_identity(
    *,
    formal: dict[str, object],
    immutable_plan_authority: SharedVpsImmutablePlanAuthority,
    controller_lifetime_issuance_identity: str,
) -> str:
    plan = _validate_immutable_plan_authority(immutable_plan_authority)
    _require(
        type(controller_lifetime_issuance_identity) is str
        and _DIGEST.fullmatch(controller_lifetime_issuance_identity) is not None,
        "SHARED_VPS_CONTROLLER_LIFETIME_AUTHORITY_INVALID",
    )
    return sha256_bytes(
        canonical_json_bytes(
            {
                "allowed_files": list(SHARED_VPS_ALLOWED_FILES),
                "allowed_read_only_path": SHARED_VPS_ACCEPTANCE_ROOT,
                "controller_lifetime_issuance_identity": (
                    controller_lifetime_issuance_identity
                ),
                "formal_binding": _formal_binding(formal),
                "host": plan.endpoint_host,
                "host_key_authority_identity": plan.host_key_authority_identity,
                "helper_binary_identity": plan.helper_binary_identity,
                "forced_command_policy_identity": (plan.forced_command_policy_identity),
                "immutable_plan_authority_identity": plan.identity,
                "port": plan.endpoint_port,
                "release_tag": SHARED_VPS_RELEASE_TAG,
                "schema": "animemo.shared-vps-probe-authority/v1",
                "ssh_user": SHARED_VPS_SSH_USER,
                "transport_authority_identity": plan.transport_authority_identity,
            }
        )
    )


def _validate_observation(
    observation: object,
    *,
    request: SharedVpsReadOnlyTransportRequest,
    expected_authority: dict[str, object],
) -> tuple[str, str, str]:
    _require(
        type(observation) is SharedVpsReadOnlyTransportObservation,
        "SHARED_VPS_TRANSPORT_OBSERVATION_INVALID",
    )
    assert isinstance(observation, SharedVpsReadOnlyTransportObservation)
    observed_strings = (
        observation.host,
        observation.ssh_user,
        observation.host_key_algorithm,
        observation.host_key_fingerprint,
        observation.host_key_authority_identity,
        observation.remote_command,
        observation.transport_authority_identity,
        observation.helper_binary_identity,
        observation.forced_command_policy_identity,
        observation.resolved_read_only_path,
    )
    _require(
        all(type(value) is str for value in observed_strings)
        and type(observation.port) is int
        and type(observation.closed_inventory) is tuple
        and all(type(value) is str for value in observation.closed_inventory),
        "SHARED_VPS_TRANSPORT_OBSERVATION_INVALID",
    )
    _require(
        observation.host == request.host
        and observation.port == request.port
        and observation.ssh_user == request.ssh_user
        and observation.host_key_algorithm == request.host_key_algorithm
        and observation.host_key_fingerprint == request.host_key_fingerprint
        and observation.host_key_authority_identity
        == request.host_key_authority_identity
        and observation.remote_command == request.remote_command,
        "SHARED_VPS_TRANSPORT_AUTHORITY_MISMATCH",
    )
    _require(
        observation.transport_authority_identity == request.transport_authority_identity
        and observation.helper_binary_identity == request.helper_binary_identity
        and observation.forced_command_policy_identity
        == request.forced_command_policy_identity,
        "SHARED_VPS_TRANSPORT_IMPLEMENTATION_IDENTITY_MISMATCH",
    )
    _require(
        observation.resolved_read_only_path == request.allowed_read_only_path
        and observation.closed_inventory == request.allowed_files,
        "SHARED_VPS_REMOTE_PATH_AUTHORITY_INVALID",
    )
    counts = (
        observation.connection_count,
        observation.command_count,
        observation.read_only_observation_count,
        observation.mutation_count,
        observation.v1_0_access_count,
        observation.unrelated_site_access_count,
        observation.dns_or_cloudflare_access_count,
        observation.firewall_access_count,
        observation.openresty_access_count,
        observation.regular_file_count,
        observation.symlink_count,
        observation.path_escape_count,
    )
    _require(
        all(type(value) is int for value in counts)
        and counts[:3] == (1, 1, len(SHARED_VPS_ALLOWED_FILES))
        and all(value == 0 for value in counts[3:9])
        and counts[9:] == (len(SHARED_VPS_ALLOWED_FILES), 0, 0),
        "SHARED_VPS_TRANSPORT_COUNTS_INVALID",
    )
    files = observation.files
    _require(
        type(files) is dict
        and all(type(key) is str for key in files)
        and set(files) == set(SHARED_VPS_ALLOWED_FILES)
        and all(
            type(value) is bytes and 0 < len(value) <= _MAX_REMOTE_FILE_BYTES
            for value in files.values()
        ),
        "SHARED_VPS_REMOTE_FILE_SET_INVALID",
    )
    authority_bytes = files["shared-vps-authority.json"]
    authority_sha256 = sha256_bytes(authority_bytes)
    expected_sums = (
        authority_sha256.removeprefix("sha256:") + "  shared-vps-authority.json\n"
    ).encode("ascii")
    _require(
        files["SHA256SUMS"] == expected_sums,
        "SHARED_VPS_REMOTE_SHA256SUMS_INVALID",
    )
    try:
        authority = json.loads(authority_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SharedVpsPrivateReadOnlyError(
            "SHARED_VPS_REMOTE_AUTHORITY_INVALID"
        ) from error
    _require(
        type(authority) is dict
        and set(authority) == _REMOTE_AUTHORITY_FIELDS
        and authority == expected_authority
        and canonical_json_bytes(authority) == authority_bytes,
        "SHARED_VPS_REMOTE_AUTHORITY_INVALID",
    )
    sums_sha256 = sha256_bytes(files["SHA256SUMS"])
    inventory_identity = sha256_bytes(
        canonical_json_bytes(
            [
                {
                    "path": name,
                    "sha256": sha256_bytes(files[name]),
                    "size": len(files[name]),
                }
                for name in SHARED_VPS_ALLOWED_FILES
            ]
        )
    )
    return authority_sha256, sums_sha256, inventory_identity


def _verify_shared_vps_private_read_only(
    *,
    formal_acceptance_record: object,
    host: str,
    port: int,
    access: str,
    allowed_read_only_path: str,
    ssh_user: str,
    host_key_algorithm: str,
    host_key_fingerprint: str,
    host_key_authority_identity: str,
    immutable_plan_authority: SharedVpsImmutablePlanAuthority,
    credential: SharedVpsPrivateKeyLease,
    prohibited_operations: tuple[str, ...],
    transport_capability: SharedVpsReadOnlyTransportCapability | None = None,
    required_capability_type: type[SharedVpsReadOnlyTransportCapability],
) -> dict[str, object]:
    """Verify one closed shared-host observation after Formal PASS.

    ``transport_capability`` is the only external boundary.  Omitting it fails
    closed; there is deliberately no ambient OpenSSH/private-key-file fallback.
    """

    plan = _validate_immutable_plan_authority(immutable_plan_authority)
    _require(
        _is_valid_endpoint_host(host)
        and host == plan.endpoint_host
        and type(port) is int
        and 1 <= port <= 65535
        and port == plan.endpoint_port
        and type(access) is str
        and access == SHARED_VPS_ACCESS
        and type(allowed_read_only_path) is str
        and allowed_read_only_path == SHARED_VPS_ACCEPTANCE_ROOT,
        "SHARED_VPS_ENDPOINT_INVALID",
    )
    _require(
        type(ssh_user) is str and ssh_user == SHARED_VPS_SSH_USER,
        "SHARED_VPS_SSH_USER_INVALID",
    )
    _require(
        type(host_key_algorithm) is str
        and host_key_algorithm == "ssh-ed25519"
        and type(host_key_fingerprint) is str
        and _FINGERPRINT.fullmatch(host_key_fingerprint) is not None,
        "SHARED_VPS_HOST_KEY_AUTHORITY_INVALID",
    )
    _require(
        type(host_key_authority_identity) is str
        and _DIGEST.fullmatch(host_key_authority_identity) is not None
        and host_key_authority_identity
        == shared_vps_host_key_authority_identity(
            host=host,
            port=port,
            ssh_user=ssh_user,
            host_key_algorithm=host_key_algorithm,
            host_key_fingerprint=host_key_fingerprint,
        ),
        "SHARED_VPS_HOST_KEY_AUTHORITY_INVALID",
    )
    _require(
        type(prohibited_operations) is tuple
        and all(type(value) is str for value in prohibited_operations)
        and prohibited_operations == SHARED_VPS_PROHIBITED_OPERATIONS,
        "SHARED_VPS_PROHIBITED_OPERATIONS_INVALID",
    )
    _require(
        type(credential) is SharedVpsPrivateKeyLease and not credential.cleared,
        "SHARED_VPS_PRIVATE_KEY_LEASE_INVALID",
    )
    formal = _closed_formal_acceptance(formal_acceptance_record)
    _require(
        formal.get("rc_tag") == SHARED_VPS_RELEASE_TAG,
        "SHARED_VPS_RELEASE_IDENTITY_INVALID",
    )
    expected_authority = _expected_remote_authority(formal)
    if transport_capability is None:
        raise SharedVpsPrivateReadOnlyError(
            "SHARED_VPS_MEMORY_ONLY_SSH_TRANSPORT_UNAVAILABLE"
        )
    if (
        required_capability_type is SharedVpsReadOnlyTransportCapability
        and type(transport_capability) is _TestOnlySharedVpsReadOnlyTransportCapability
    ):
        raise SharedVpsPrivateReadOnlyError("SHARED_VPS_TEST_AUTHORITY_FORBIDDEN")
    _require(
        type(transport_capability) is required_capability_type,
        "SHARED_VPS_TRANSPORT_CAPABILITY_INVALID",
    )
    assert isinstance(transport_capability, SharedVpsReadOnlyTransportCapability)
    _require(
        host_key_authority_identity == plan.host_key_authority_identity
        and transport_capability.immutable_plan_authority_identity == plan.identity
        and transport_capability.controller_lifetime_immutable_plan_authority_identity
        == plan.identity
        and transport_capability.authority_identity == plan.transport_authority_identity
        and transport_capability.helper_binary_identity == plan.helper_binary_identity
        and transport_capability.forced_command_policy_identity
        == plan.forced_command_policy_identity,
        "SHARED_VPS_IMMUTABLE_PLAN_AUTHORITY_MISMATCH",
    )
    formal_binding = _formal_binding(formal)
    request = SharedVpsReadOnlyTransportRequest(
        host=host,
        port=port,
        access=access,
        allowed_read_only_path=allowed_read_only_path,
        allowed_files=SHARED_VPS_ALLOWED_FILES,
        ssh_user=ssh_user,
        host_key_algorithm=host_key_algorithm,
        host_key_fingerprint=host_key_fingerprint,
        host_key_authority_identity=host_key_authority_identity,
        remote_command=SHARED_VPS_REMOTE_COMMAND,
        prohibited_operations=SHARED_VPS_PROHIBITED_OPERATIONS,
        formal_acceptance_identity=formal_binding["formal_acceptance_identity"],
        formal_aggregate_receipt_digest=formal_binding[
            "formal_aggregate_receipt_digest"
        ],
        formal_execution_receipt_digest=formal_binding[
            "formal_execution_receipt_digest"
        ],
        transport_authority_identity=plan.transport_authority_identity,
        helper_binary_identity=plan.helper_binary_identity,
        forced_command_policy_identity=plan.forced_command_policy_identity,
    )
    probe_authority_identity = _probe_authority_identity(
        formal=formal,
        immutable_plan_authority=plan,
        controller_lifetime_issuance_identity=(
            transport_capability.controller_lifetime_issuance_identity
        ),
    )
    retry_attempt = transport_capability.begin_probe_attempt(probe_authority_identity)
    try:
        try:
            observation = transport_capability.observe(request, credential)
        except BaseException:  # noqa: BLE001 - fixed-code secret boundary
            raise SharedVpsPrivateReadOnlyError("SHARED_VPS_TRANSPORT_FAILED") from None
        _require(
            credential.borrow_count == 1,
            "SHARED_VPS_PRIVATE_KEY_NOT_CONSUMED",
        )
        authority_sha256, sums_sha256, inventory_identity = _validate_observation(
            observation,
            request=request,
            expected_authority=expected_authority,
        )
    except SharedVpsPrivateReadOnlyError as error:
        transport_capability.record_probe_failure(probe_authority_identity, error.code)
        raise
    finally:
        credential.clear()
    transport_capability.record_probe_success(probe_authority_identity)
    assert isinstance(observation, SharedVpsReadOnlyTransportObservation)
    receipt_unsigned = {
        "schema": "animemo.shared-vps-production-private-read-only-observation/v2",
        "host": host,
        "port": port,
        "access": access,
        "allowedReadOnlyPath": allowed_read_only_path,
        "sshUser": ssh_user,
        "hostKeyAlgorithm": host_key_algorithm,
        "hostKeyFingerprint": host_key_fingerprint,
        "hostKeyAuthorityIdentity": host_key_authority_identity,
        "remoteCommand": SHARED_VPS_REMOTE_COMMAND,
        "releaseTag": formal["rc_tag"],
        "publicationIdentity": expected_authority["publication_identity"],
        "apiDigest": formal["api_digest"],
        "webDigest": formal["web_digest"],
        "formalAcceptanceIdentity": formal["identity"],
        "formalAggregateReceiptDigest": formal_binding[
            "formal_aggregate_receipt_digest"
        ],
        "formalExecutionReceiptDigest": formal_binding[
            "formal_execution_receipt_digest"
        ],
        "probeAuthorityIdentity": probe_authority_identity,
        "controllerLifetimeIssuanceIdentity": (
            transport_capability.controller_lifetime_issuance_identity
        ),
        "transportAuthorityIdentity": (transport_capability.authority_identity),
        "helperBinaryIdentity": transport_capability.helper_binary_identity,
        "forcedCommandPolicyIdentity": (
            transport_capability.forced_command_policy_identity
        ),
        "remoteAuthoritySha256": authority_sha256,
        "remoteSha256sumsSha256": sums_sha256,
        "remoteInventoryIdentity": inventory_identity,
        "connectionCount": observation.connection_count,
        "commandCount": observation.command_count,
        "readOnlyObservationCount": observation.read_only_observation_count,
        "mutationCount": observation.mutation_count,
        "v1_0AccessCount": observation.v1_0_access_count,
        "unrelatedSiteAccessCount": observation.unrelated_site_access_count,
        "dnsOrCloudflareAccessCount": observation.dns_or_cloudflare_access_count,
        "firewallAccessCount": observation.firewall_access_count,
        "openrestyAccessCount": observation.openresty_access_count,
        "regularFileCount": observation.regular_file_count,
        "symlinkCount": observation.symlink_count,
        "pathEscapeCount": observation.path_escape_count,
        "retryAttempt": retry_attempt,
        "result": "PASS",
    }
    return {
        **receipt_unsigned,
        "identity": sha256_bytes(canonical_json_bytes(receipt_unsigned)),
    }


def verify_shared_vps_private_read_only(
    *,
    formal_acceptance_record: object,
    host: str,
    port: int,
    access: str,
    allowed_read_only_path: str,
    ssh_user: str,
    host_key_algorithm: str,
    host_key_fingerprint: str,
    host_key_authority_identity: str,
    immutable_plan_authority: SharedVpsImmutablePlanAuthority,
    credential: SharedVpsPrivateKeyLease,
    prohibited_operations: tuple[str, ...],
    transport_capability: SharedVpsReadOnlyTransportCapability | None = None,
) -> dict[str, object]:
    """Public secret-clearing boundary for one post-Formal probe."""

    try:
        return _verify_shared_vps_private_read_only(
            formal_acceptance_record=formal_acceptance_record,
            host=host,
            port=port,
            access=access,
            allowed_read_only_path=allowed_read_only_path,
            ssh_user=ssh_user,
            host_key_algorithm=host_key_algorithm,
            host_key_fingerprint=host_key_fingerprint,
            host_key_authority_identity=host_key_authority_identity,
            immutable_plan_authority=immutable_plan_authority,
            credential=credential,
            prohibited_operations=prohibited_operations,
            transport_capability=transport_capability,
            required_capability_type=SharedVpsReadOnlyTransportCapability,
        )
    finally:
        if type(credential) is SharedVpsPrivateKeyLease:
            credential.clear()


def _verify_shared_vps_private_read_only_test_only(
    *,
    formal_acceptance_record: object,
    host: str,
    port: int,
    access: str,
    allowed_read_only_path: str,
    ssh_user: str,
    host_key_algorithm: str,
    host_key_fingerprint: str,
    host_key_authority_identity: str,
    immutable_plan_authority: SharedVpsImmutablePlanAuthority,
    credential: SharedVpsPrivateKeyLease,
    prohibited_operations: tuple[str, ...],
    transport_capability: SharedVpsReadOnlyTransportCapability | None = None,
) -> dict[str, object]:
    """Explicit unit-test seam which production does not export."""

    try:
        return _verify_shared_vps_private_read_only(
            formal_acceptance_record=formal_acceptance_record,
            host=host,
            port=port,
            access=access,
            allowed_read_only_path=allowed_read_only_path,
            ssh_user=ssh_user,
            host_key_algorithm=host_key_algorithm,
            host_key_fingerprint=host_key_fingerprint,
            host_key_authority_identity=host_key_authority_identity,
            immutable_plan_authority=immutable_plan_authority,
            credential=credential,
            prohibited_operations=prohibited_operations,
            transport_capability=transport_capability,
            required_capability_type=(_TestOnlySharedVpsReadOnlyTransportCapability),
        )
    finally:
        if type(credential) is SharedVpsPrivateKeyLease:
            credential.clear()


__all__ = (
    "SHARED_VPS_PROHIBITED_OPERATIONS",
    "SharedVpsImmutablePlanAuthority",
    "SharedVpsPrivateKeyLease",
    "SharedVpsPrivateReadOnlyError",
    "SharedVpsReadOnlyTransportCapability",
    "SharedVpsReadOnlyTransportObservation",
    "SharedVpsReadOnlyTransportRequest",
    "acquire_shared_vps_private_key_lease",
    "shared_vps_host_key_authority_identity",
    "verify_shared_vps_private_read_only",
)
