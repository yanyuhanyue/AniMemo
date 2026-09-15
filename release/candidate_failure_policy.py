"""Prospective graded failure contract; diagnostic classification grants no reuse."""
from __future__ import annotations

FAILURE_POLICY = 'animemo.graded-profile-failure/v1'
BUSINESS = 'TRUSTED_BUSINESS_FAILURE'
SECURITY = 'SECURITY_OR_EXECUTION_UNCERTAIN'
APT_FAILURES = frozenset({
    'PLATFORM_BOOTSTRAP_APT_UPDATE_FAILED', 'PLATFORM_BOOTSTRAP_DOCKER_INSTALL_FAILED',
    'PLATFORM_BOOTSTRAP_COMPOSE_INSTALL_FAILED', 'PLATFORM_BOOTSTRAP_POSTGRES_CLIENT_INSTALL_FAILED',
})
PLATFORM_BUSINESS = APT_FAILURES | {
    'PLATFORM_BOOTSTRAP_PACKAGE_UNAVAILABLE', 'PLATFORM_BOOTSTRAP_DOCKER_DAEMON_FAILED',
    'PLATFORM_BOOTSTRAP_POST_QUALIFICATION_FAILED',
}
INSTALLER_BUSINESS = frozenset({
    'INSTALL_SERVICE_PREPARATION_FAILED', 'INSTALL_DATABASE_MIGRATION_FAILED',
    'INSTALL_BOOTSTRAP_FAILED', 'INSTALL_RUNTIME_START_FAILED', 'INSTALL_DOCTOR_FAILED',
    'INSTALL_DOCTOR_INCOMPLETE', 'INSTALL_CANONICAL_ACCEPTANCE_FAILED',
})
WRAPPER_ERRORS = frozenset({
    'ROOT_EXECUTION_FAILED', 'RUNNER_EXECUTION_FAILED', 'INSTALLER_EXECUTION_FAILED',
})
_REQUIRED_STAGES = frozenset({
    'SSH_OBSERVED', 'SUDO_STARTED', 'ROOT_STARTED', 'MATERIAL_FINALIZING',
    'MATERIAL_VERIFIED', 'RUNTIME_INITIALIZING', 'RUNTIME_READY', 'RUNNER_STARTING',
    'RUNNER_STARTED', 'INSTALLER_STARTING', 'PLATFORM_PREPARING', 'PLATFORM_PLANNED',
})
_APT_UNSAFE = frozenset({'SIGNATURE', 'CERTIFICATE', 'SOURCE_IDENTITY', 'DISK', 'LOCK'})


def business_failure_diagnostic(observed):
    """Tentative only: complete delivery/SSH exit/cleanup are checked by the owner."""
    if (type(observed) is not dict or observed.get('root_started') is not True
            or observed.get('transport_error') is not None
            or observed.get('host_receipt_parse') != 'NOT_REACHED'
            or observed.get('profile_draft_received') is not False):
        return False
    exits = observed.get('exit_codes', {})
    if (any(type(exits.get(name)) is not int or exits[name] != 2
            for name in ('SUDO', 'ROOT', 'RUNTIME_RUNNER'))
            or type(exits.get('INSTALLER')) is not int or exits['INSTALLER'] not in {3, 5, 6}):
        return False
    events = observed.get('events', [])
    stages = {event.get('stage') for event in events if event.get('kind') == 'STAGE'}
    errors = set(observed.get('errors', []))
    platform = errors & PLATFORM_BUSINESS
    installer = errors & INSTALLER_BUSINESS
    if not _REQUIRED_STAGES <= stages or not WRAPPER_ERRORS <= errors:
        return False
    if platform:
        if (len(platform) != 1 or exits['INSTALLER'] != 6
                or 'PLATFORM_READY' in stages or 'PLATFORM_PREPARATION_FAILED' not in errors
                or errors - (WRAPPER_ERRORS | PLATFORM_BUSINESS | {'PLATFORM_PREPARATION_FAILED'})):
            return False
    elif installer:
        if (len(installer) != 1 or not {'PLATFORM_READY', 'INSTALLER_RUNNING'} <= stages
                or errors - (WRAPPER_ERRORS | INSTALLER_BUSINESS)):
            return False
    else:
        return False
    apt = [event['observation'] for event in events if event.get('kind') == 'APT']
    if platform & APT_FAILURES and not apt:
        return False
    for item in apt:
        if (item.get('outcome') != 'EXITED' or item.get('secondary_errors') != []
                or type(item.get('returncode')) is not int
                or any(item.get(name, {}).get('missing') is not False for name in ('stdout', 'stderr'))
                or set(item.get('categories', [])) & _APT_UNSAFE):
            return False
    if platform & APT_FAILURES and apt[-1]['returncode'] == 0:
        return False
    return True


def cleanup_allows_continuation(operation):
    """Require positive evidence, never infer success from missing diagnostics."""
    return (type(operation) is dict
        and operation.get('failure_policy') == FAILURE_POLICY
        and operation.get('failure_classification') == BUSINESS
        and operation.get('workload_process_completed') is True
        and operation.get('workload_delivery_completed') is True
        and operation.get('workload_identity_rechecked') is True
        and operation.get('workload_supervisor_closed') is True
        and operation.get('continuation_source_verified') is True
        and operation.get('power_state') == 'STOPPED'
        and operation.get('session_keys_removed') is True
        and operation.get('known_hosts_removed') is True
        and operation.get('lease_released') is True
        and operation.get('cleanup_errors') == [])
