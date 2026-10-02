"""Single Runtime DEV consent and one-use full-plan gate.

The public JSON is an inspection document.  Execution additionally requires a
locally acquired root gate and the independently verified LocalBundle capability.
The stdlib-only preparation functions are embedded in the fixed root program.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath

BOUNDARY_SCHEMA = 'animemo.runtime-development-operation-boundary/v1'
PLAN_SCHEMA = 'animemo.runtime-development-full-operation-plan/v1'
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_STEPS = ('roots.prepare', 'configuration.publish', 'release.stage',
    'services.prepare', 'database.migrate', 'application.bootstrap',
    'runtime.start', 'runtime.validate', 'updater.adopt-and-publish-locator', 'doctor.accept')
_BINDING_EXTRAS = {'runtime_operation_boundary', 'runtime_lifetime'}


class DevelopmentBoundaryError(ValueError):
    pass


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        allow_nan=False) + '\n').encode('utf-8')


def _digest(value):
    return 'sha256:' + hashlib.sha256(_canonical(value)).hexdigest()


def _copy(value):
    return json.loads(_canonical(value))


def _base_binding(binding):
    return {key: value for key, value in binding.items() if key not in _BINDING_EXTRAS}


def _resource_scope():
    return {
        'instance': 'default', 'compose_project': 'animemo-default',
        'service': 'animemo-updater@default.service',
        'docker_socket': 'unix:///var/run/docker.sock',
        'docker_storage_root': '/var/lib/docker',
        'updater_socket': '/run/animemo-updater/default/updater.sock',
        'listen': {'host': '127.0.0.1', 'port': 8088},
        'preparation_loopback_probe': {'host': '127.0.0.1', 'port': 0,
            'purpose': 'CAPABILITY_READINESS_ONLY'},
        'public_origin': 'https://candidate.invalid',
        'roots': ['/opt/animemo-instances/default', '/data/animemo-instances/default',
            '/var/lib/animemo-updater/instances/default', '/run/animemo-updater/default'],
        'shared_base_creation': ['/data', '/opt/animemo-instances', '/data/animemo-instances',
            '/var/lib/animemo-updater', '/var/lib/animemo-updater/instances', '/run/animemo-updater'],
        'updater_program': '/opt/animemo-updater',
        'service_assets': ['/etc/systemd/system/animemo-updater@.service',
            '/etc/systemd/system/animemo-updater@default.service.d/10-candidate-network-isolation.conf',
            '/etc/systemd/system/multi-user.target.wants/animemo-updater@default.service',
            '/usr/lib/sysusers.d/animemo-updater.conf', '/usr/lib/tmpfiles.d/animemo-updater.conf',
            '/usr/local/bin/animemo', '/usr/local/bin/animemo-updater'],
        'system_identity_changes': {'user': 'animemo-updater', 'group': 'animemo-api',
            'group_id': 10001, 'memberships': ['animemo-api', 'docker']},
        'containers': ['api', 'web', 'postgres', 'redis'],
        'data_mounts': ['/data/animemo-instances/default/postgres',
            '/data/animemo-instances/default/redis', '/data/animemo-instances/default/media',
            '/data/animemo-instances/default/plugins'],
        'named_volumes': [], 'network': 'LOCAL_COMPOSE_INTERNAL_ONLY',
        'external_fetch': False, 'pull_policy': 'never', 'system_packages': [],
    }


def preparation_paths(binding):
    """Exact roots; callers may only request these roots or their descendants."""
    selection = binding.get('runtime_trust_selection_digest')
    candidate = binding.get('candidate_input_digest')
    inventory = binding.get('execution_inventory_digest')
    if any(type(item) is not str or _DIGEST.fullmatch(item) is None
           for item in (selection, candidate, inventory)):
        raise DevelopmentBoundaryError('DEVELOPMENT_PREPARATION_IDENTITY_INVALID')
    base = '/var/lib/animemo/local-development'
    session = binding.get('session_id')
    if type(session) is not str or re.fullmatch('[0-9a-f]{32}', session) is None:
        raise DevelopmentBoundaryError('DEVELOPMENT_PREPARATION_IDENTITY_INVALID')
    return sorted([
        '/var/lib/animemo/prepublication-candidates/v2/' + candidate[7:],
        base + '/' + inventory[7:], base + '/python-runtime', base + '/profile-report.json',
        base + '/runtime-inputs/' + selection[7:],
        base + '/runtime-cache/' + selection[7:] + '/' + session,
        '/run/lock/animemo-platform-bootstrap.lock',
        base + '/runtime-authority/' + session + '.json',
        base + '/runtime-authority/' + session + '.consumed',
    ])


def _baseline_observation(baseline):
    observed = baseline.get('observation')
    if type(observed) is not dict:
        raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_BASELINE_REQUIRED')
    tools, socket, daemon = (observed.get(name) for name in ('tools', 'docker_socket', 'docker_daemon'))
    paths = {'python': ('/usr/bin/python3',), 'docker': ('/usr/bin/docker',),
        'systemctl': ('/usr/bin/systemctl',), 'dpkg': ('/usr/bin/dpkg',),
        'dpkg-query': ('/usr/bin/dpkg-query',), 'pg_dump': ('/usr/bin/pg_dump',),
        'psql': ('/usr/bin/psql',), 'compose': ('/usr/libexec/docker/cli-plugins/docker-compose',
            '/usr/lib/docker/cli-plugins/docker-compose')}
    if type(tools) is not dict or set(tools) != set(paths):
        raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_TOOLS_REQUIRED')
    for name, record in tools.items():
        if (type(record) is not dict or set(record) != {'path', 'resolved_path', 'sha256'}
                or record['path'] not in paths[name] or type(record['resolved_path']) is not str
                or not record['resolved_path'].startswith(('/usr/bin/', '/usr/lib/', '/usr/libexec/', '/usr/share/postgresql-common/'))
                or type(record['sha256']) is not str or _DIGEST.fullmatch(record['sha256']) is None):
            raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_TOOL_INVALID')
    if type(socket) is not dict or socket.get('path') != '/var/run/docker.sock':
        raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_SOCKET_INVALID')
    if socket.get('state') == 'KNOWN_LOCAL':
        if (set(socket) != {'state', 'path', 'resolved_path', 'device', 'inode', 'mode', 'uid', 'gid', 'ctime_ns'}
                or socket['resolved_path'] not in ('/run/docker.sock', '/var/run/docker.sock')
                or any(type(socket[key]) is not int or socket[key] < 0
                    for key in ('device', 'inode', 'mode', 'uid', 'gid', 'ctime_ns'))
                or socket['uid'] != 0 or not stat.S_ISSOCK(socket['mode']) or socket['mode'] & 0o002):
            raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_SOCKET_INVALID')
    elif socket != {'state': 'PERMISSION_UNKNOWN', 'path': '/var/run/docker.sock'}:
        raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_SOCKET_INVALID')
    if type(daemon) is not dict:
        raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_DAEMON_INVALID')
    if daemon.get('state') == 'OBSERVED':
        if (set(daemon) != {'state', 'server_version', 'storage_root', 'identity'}
                or daemon['storage_root'] != '/var/lib/docker'
                or daemon['identity'] is not None and (type(daemon['identity']) is not str
                    or _DIGEST.fullmatch(daemon['identity']) is None)
                or type(daemon['server_version']) is not str or not 0 < len(daemon['server_version']) <= 512):
            raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_DAEMON_INVALID')
    elif (set(daemon) != {'state', 'identity'} or daemon['state'] != 'PERMISSION_UNKNOWN'
            or daemon['identity'] is not None and (type(daemon['identity']) is not str
                or _DIGEST.fullmatch(daemon['identity']) is None)):
        raise DevelopmentBoundaryError('DEVELOPMENT_CONFIRMED_DAEMON_INVALID')
    return _copy({'tools': tools, 'docker_socket': socket, 'docker_daemon': daemon})


def build_runtime_operation_boundary(*, binding, baseline, lifetime, material):
    binding = _base_binding(binding)
    if (binding.get('runtime_offline_only') is not True
            or binding.get('workload_mode') != 'CLEAN_PREACCEPTANCE'
            or type(baseline) is not dict or type(baseline.get('guest_identity')) is not dict
            or not _DIGEST.fullmatch(baseline.get('baseline_digest', ''))
            or type(material) is not dict
            or material.get('selection_digest') != binding.get('runtime_trust_selection_digest')):
        raise DevelopmentBoundaryError('DEVELOPMENT_OPERATION_BOUNDARY_INVALID')
    body = {'schema': BOUNDARY_SCHEMA, 'purpose': 'LOCAL_INSTALLER_DEVELOPMENT',
        'binding': binding, 'baseline_digest': baseline['baseline_digest'],
        'baseline_observation': _baseline_observation(baseline),
        'guest_identity': baseline['guest_identity'], 'lifetime': lifetime,
        'material': material, 'resources': _resource_scope(),
        'preparation_writes': preparation_paths(binding),
        'execution': {'profile': 'OFFLINE_VALIDATE_ONLY', 'transport': 'local-bundle',
            'action': 'INSTALL_FRESH', 'steps': list(_STEPS), 'maximum_executions': 1},
        'retention': {'instance': 'STOP_AND_READBACK', 'clone': 'SOFT_STOP_AND_RETAIN',
            'data': 'RETAIN', 'delete_volumes': False, 'global_prune': False}}
    return {**_copy(body), 'boundary_digest': _digest(body)}


def validate_runtime_operation_boundary(boundary, binding):
    if type(boundary) is not dict:
        raise DevelopmentBoundaryError('DEVELOPMENT_OPERATION_BOUNDARY_MISSING')
    try:
        expected = build_runtime_operation_boundary(binding=binding,
            baseline={'baseline_digest': boundary['baseline_digest'],
                'guest_identity': boundary['guest_identity'], 'observation': boundary['baseline_observation']},
            lifetime=boundary['lifetime'], material=boundary['material'])
    except (KeyError, TypeError, ValueError):
        raise DevelopmentBoundaryError('DEVELOPMENT_OPERATION_BOUNDARY_INVALID') from None
    if boundary != expected:
        raise DevelopmentBoundaryError('DEVELOPMENT_OPERATION_BOUNDARY_CHANGED')
    return expected


def require_preparation_binding(binding, paths):
    boundary = validate_runtime_operation_boundary(binding.get('runtime_operation_boundary'), binding)
    if not isinstance(paths, (list, tuple)) or not paths:
        raise DevelopmentBoundaryError('DEVELOPMENT_PREPARATION_PATH_INVALID')
    roots = tuple(PurePosixPath(item) for item in boundary['preparation_writes'])
    for item in paths:
        if type(item) is not str or not item.startswith('/') or '..' in PurePosixPath(item).parts:
            raise DevelopmentBoundaryError('DEVELOPMENT_PREPARATION_PATH_INVALID')
        path = PurePosixPath(item)
        if str(path) != item or not any(path == root or root in path.parents for root in roots):
            raise DevelopmentBoundaryError('DEVELOPMENT_PREPARATION_OUT_OF_SCOPE')
    return boundary


def require_root_preparation_binding(binding, paths):
    """Fixed Host transport may seal only the confirmed preparation roots.

    This check cannot issue business permission. The sealed runtime performs
    the complete boundary and actual-observation checks before trust consumption.
    """
    boundary = binding.get('runtime_operation_boundary')
    fields = {'schema', 'purpose', 'binding', 'baseline_digest', 'baseline_observation',
        'guest_identity', 'lifetime', 'material', 'resources', 'preparation_writes',
        'execution', 'retention', 'boundary_digest'}
    if (type(boundary) is not dict or set(boundary) != fields
            or boundary['schema'] != BOUNDARY_SCHEMA
            or boundary['purpose'] != 'LOCAL_INSTALLER_DEVELOPMENT'
            or binding.get('runtime_offline_only') is not True
            or binding.get('workload_mode') != 'CLEAN_PREACCEPTANCE'
            or boundary['binding'] != _base_binding(binding)
            or boundary['boundary_digest'] != _digest({key: value for key, value in boundary.items()
                if key != 'boundary_digest'})
            or boundary['preparation_writes'] != preparation_paths(binding)):
        raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_PREPARATION_BOUNDARY_INVALID')
    if not isinstance(paths, (tuple, list)) or not paths:
        raise DevelopmentBoundaryError('DEVELOPMENT_PREPARATION_PATH_INVALID')
    roots = tuple(PurePosixPath(item) for item in boundary['preparation_writes'])
    for item in paths:
        if (type(item) is not str or not item.startswith('/') or str(PurePosixPath(item)) != item
                or '..' in PurePosixPath(item).parts
                or not any(PurePosixPath(item) == root or root in PurePosixPath(item).parents for root in roots)):
            raise DevelopmentBoundaryError('DEVELOPMENT_PREPARATION_OUT_OF_SCOPE')
    return boundary


class DevelopmentExecutionGate:
    """Nonserializable root capability; a public boundary alone cannot execute."""
    __slots__ = (
        '_approved',
        '_approved_plan',
        '_attempted',
        '_authority_fd',
        '_binding',
        '_boundary',
        '_closed',
        '_lifetime',
        '_root_observation',
        '_root_platform_plan',
        '_trust',
    )

    def __init__(self, *_args, **_kwargs):
        raise TypeError('Use acquire_development_execution_gate')

    def __reduce__(self):
        raise TypeError('Development execution gate cannot be serialized')

    @property
    def boundary(self):
        return _copy(self._boundary)

    @property
    def binding(self):
        return _copy(self._binding)

    @property
    def lifetime(self):
        return self._lifetime

    def remaining_seconds(self):
        return self._lifetime.clip_timeout(14400)

    @property
    def boundary_digest(self):
        return self._boundary['boundary_digest']

    @property
    def session_id(self):
        return self._binding['session_id']

    @property
    def approved_plan(self):
        return self._approved_plan

    @property
    def execution_started(self):
        return self._attempted

    def check_live(self, stage='RUNTIME_OPERATION'):
        if self._closed:
            raise DevelopmentBoundaryError('DEVELOPMENT_EXECUTION_GATE_CLOSED')
        self._lifetime.check(stage)
        validate_runtime_operation_boundary(self._boundary, self._binding)
        if self._authority_fd is not None:
            os.lseek(self._authority_fd, 0, os.SEEK_SET)
            if os.read(self._authority_fd, 128 * 1024 + 1) != _canonical(self._binding):
                raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_CHANNEL_CHANGED')

    def close(self):
        self._closed = True
        if self._authority_fd is not None:
            os.close(self._authority_fd)
            self._authority_fd = None

    def require_preparation(self, paths, *, binding=None):
        self.check_live('RUNTIME_PREPARATION')
        if binding is not None and _base_binding(binding) != _base_binding(self._binding):
            raise DevelopmentBoundaryError('DEVELOPMENT_PREPARATION_BINDING_CHANGED')
        if paths:
            require_preparation_binding(self._binding, tuple(str(path) for path in paths))

    def bind_trust(self, authority):
        from installer.development_trust import DevelopmentLocalBundleAuthority
        if type(authority) is not DevelopmentLocalBundleAuthority or self._trust is not None:
            raise DevelopmentBoundaryError('DEVELOPMENT_TRUST_AUTHORITY_REQUIRED')
        self.check_live('RUNTIME_TRUST')
        authority.verify_current()
        if (authority.material_boundary != self._boundary['material']
                or _base_binding(authority.binding) != _base_binding(self._binding)):
            raise DevelopmentBoundaryError('DEVELOPMENT_TRUST_MATERIAL_CHANGED')
        self._trust = authority

    def require_bound_trust(self, authority):
        if authority is not self._trust or authority is None:
            raise DevelopmentBoundaryError('DEVELOPMENT_TRUST_AUTHORITY_REQUIRED')
        self.check_live('RUNTIME_TRUST')
        authority.verify_current()

    def approve(self, platform_plan, installer_plan):
        self.require_bound_trust(self._trust)
        if self._approved is not None or self._attempted:
            raise DevelopmentBoundaryError('DEVELOPMENT_OPERATION_ALREADY_APPROVED')
        expression = full_operation_plan(self._boundary, platform_plan, installer_plan)
        if platform_plan.initial_capabilities.as_dict() != self._root_platform_plan.initial_capabilities.as_dict():
            raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_PLATFORM_CHANGED')
        self._approved = expression
        self._approved_plan = installer_plan
        return _copy(expression)

    def consume(self, plan):
        self.require_bound_trust(self._trust)
        if (self._attempted or self._approved_plan is not plan or self._approved is None
                or plan.as_dict() != self._approved['installer_plan']):
            raise DevelopmentBoundaryError('DEVELOPMENT_EXECUTION_PERMISSION_REQUIRED')
        # Consumption precedes revalidation and remains consumed on any failure.
        self._attempted = True
        self.check_execute()
        from installer.production import LocalDockerCommandRunner
        from scripts.runtime_development_boundary import observe_runtime_baseline_facts
        actual = observe_runtime_baseline_facts(expected=self._boundary['baseline_observation'],
            runner=LocalDockerCommandRunner(), timeout=lambda: self._lifetime.clip_timeout(30))
        _check_root_facts_against_platform(actual, self._root_platform_plan.as_dict())
        if actual != self._root_observation['baseline_facts']:
            raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_FACTS_CHANGED')

    def check_execute(self):
        self.check_live('RUNTIME_EXECUTE')
        self.require_bound_trust(self._trust)

    def require_cleanup(self, plan):
        if plan is not self._approved_plan or self._approved is None and plan is not None:
            raise DevelopmentBoundaryError('DEVELOPMENT_CLEANUP_PLAN_CHANGED')
        return self._lifetime.instance_stop_timeout(180)

    def result_observation(self):
        if not self._attempted or self._approved is None:
            raise DevelopmentBoundaryError('DEVELOPMENT_EXECUTION_NOT_CONSUMED')
        return {'schema': 'animemo.runtime-development-plan-consumption/v1',
            'boundary_digest': self.boundary_digest, 'session_id': self.session_id,
            'plan': _copy(self._approved), 'executions': 1,
            'root_observation': _copy(self._root_observation)}


def acquire_development_execution_gate(binding):
    from scripts.runtime_development_boundary import RuntimeDeadline
    boundary = validate_runtime_operation_boundary(binding.get('runtime_operation_boundary'), binding)
    if os.name != 'posix' or sys.platform != 'linux' or os.getuid() != 0 or os.geteuid() != 0:
        raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_OBSERVATION_REQUIRED')
    lifetime = RuntimeDeadline.from_guest_envelope(binding['runtime_lifetime'], binding=binding)
    lifetime.check('RUNTIME_ROOT_CHANNEL')
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    machine_id = Path('/etc/machine-id').read_text(encoding='ascii').strip()
    if (boot_id != boundary['guest_identity'].get('boot_id')
            or machine_id != boundary['guest_identity'].get('machine_id')):
        raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_IDENTITY_CHANGED')
    authority_path = Path('/var/lib/animemo/local-development/runtime-authority') / (binding['session_id'] + '.json')
    parent = authority_path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o077:
        raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_CHANNEL_REQUIRED')
    descriptor = os.open(authority_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077
                or metadata.st_nlink != 1 or metadata.st_size > 128 * 1024
                or os.read(descriptor, 128 * 1024 + 1) != _canonical(binding)):
            raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_CHANNEL_REQUIRED')
        require_preparation_binding(binding, (str(authority_path.with_suffix('.consumed')),))
        consumed = os.open(authority_path.with_suffix('.consumed'),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(consumed)
    except BaseException:
        os.close(descriptor)
        raise
    value = object.__new__(DevelopmentExecutionGate)
    value._binding = _copy(binding)
    value._boundary = boundary
    value._lifetime = lifetime
    value._trust = value._approved = value._approved_plan = None
    value._attempted = False
    value._closed = False
    value._root_observation = {'uid': 0, 'euid': 0, 'platform': 'linux',
        'machine_id': machine_id, 'boot_id': boot_id}
    value._authority_fd = descriptor
    try:
        value.check_live('RUNTIME_ROOT')
        from installer.production import LocalDockerCommandRunner
        value._root_platform_plan = observe_root_platform_plan(value)
        from scripts.runtime_development_boundary import observe_runtime_baseline_facts
        actual = observe_runtime_baseline_facts(expected=boundary['baseline_observation'],
            runner=LocalDockerCommandRunner(), timeout=lambda: value._lifetime.clip_timeout(30))
        _check_root_facts_against_platform(actual, value._root_platform_plan.as_dict())
        value._root_observation['baseline_facts'] = actual
        value._root_observation['docker_storage_root'] = actual['docker_daemon']['storage_root']
        value._root_observation['platform_plan_digest'] = value._root_platform_plan.plan_digest
        value.check_live('RUNTIME_ROOT')
    except BaseException:
        value.close()
        raise
    return value


def observe_root_platform_plan(gate):
    from installer.platform_bootstrap import (
        ProductionPlatformBootstrap,
        SubprocessPlatformCommandRunner,
    )
    from installer.production import CandidatePlatformCommandObserver
    from installer.runtime import InstallTransportSource
    observer = CandidatePlatformCommandObserver(SubprocessPlatformCommandRunner())
    observer._development_gate = gate
    return ProductionPlatformBootstrap(runner=observer).plan(transport_source=InstallTransportSource.LOCAL_BUNDLE)


def _check_root_facts_against_platform(actual, platform_plan):
    socket = actual['docker_socket']
    identity = _digest({key: socket[source] for key, source in (
        ('device', 'device'), ('inode', 'inode'), ('mode', 'mode'), ('uid', 'uid'),
        ('gid', 'gid'), ('ctimeNs', 'ctime_ns'), ('resolvedPath', 'resolved_path'))})
    if (identity != platform_plan['initialCapabilities']['dockerSocketIdentity']
            or actual['docker_daemon']['identity'] != platform_plan['initialCapabilities']['dockerDaemonIdentity']):
        raise DevelopmentBoundaryError('DEVELOPMENT_ROOT_FACTS_CHANGED')


def full_operation_plan(boundary, platform_plan, installer_plan):
    """Serialize actual planners plus their complete, source-fixed effect scope."""
    from installer.platform_bootstrap import PlatformBootstrapPlan
    from installer.runtime import InstallPlan
    if type(platform_plan) is not PlatformBootstrapPlan or type(installer_plan) is not InstallPlan:
        raise DevelopmentBoundaryError('DEVELOPMENT_REAL_PLANNER_REQUIRED')
    platform_value, value = platform_plan.as_dict(), installer_plan.as_dict()
    if (_digest(platform_plan.identity_body()) != platform_plan.plan_digest
            or 'sha256:' + hashlib.sha256(_canonical(installer_plan.body()).rstrip(b'\n')).hexdigest() != installer_plan.plan_digest):
        raise DevelopmentBoundaryError('DEVELOPMENT_PLAN_DIGEST_INVALID')
    return _full_operation_values(boundary, platform_value, value)


def _full_operation_values(boundary, platform_value, value):
    expected_fields = {'planIdentity', 'operationId', 'instanceName', 'mode', 'action',
        'selector', 'transportSource', 'transportPolicyIdentity', 'release', 'target',
        'platform', 'compatibility', 'configuration', 'restore', 'executionSteps', 'warnings', 'planDigest'}
    if (type(value) is not dict or set(value) != expected_fields
            or value['planIdentity'] != 'animemo.install-plan/v1'
            or value['planDigest'] != 'sha256:' + hashlib.sha256(_canonical(
                {key: item for key, item in value.items() if key != 'planDigest'}).rstrip(b'\n')).hexdigest()):
        raise DevelopmentBoundaryError('DEVELOPMENT_INSTALLER_PLAN_INVALID')
    if (platform_value['mode'] != 'OFFLINE_VALIDATE_ONLY'
            or platform_value['transportSource'] != 'local-bundle'
            or platform_value['actions'] != [{'kind': 'VALIDATE_ONLY', 'packages': []}]
            or platform_value['networkPolicy'] != 'DENY_ALL'
            or platform_value['dockerDaemonPolicy'] != 'VALIDATE_ONLY'
            or platform_value['initialCapabilities']['effectiveUid'] != 0):
        raise DevelopmentBoundaryError('DEVELOPMENT_PLATFORM_PLAN_OUT_OF_SCOPE')
    scope = boundary['resources']
    if (value['instanceName'] != scope['instance'] or value['mode'] != 'fresh'
            or value['action'] != 'INSTALL_FRESH' or value['restore'] is not None
            or value['transportSource'] != 'local-bundle'
            or value['target']['classification'] not in {'ABSENT', 'VERIFIED_EMPTY'}
            or value['executionSteps'] != list(_STEPS)
            or value['configuration']['publicOrigin'] != scope['public_origin']
            or value['configuration']['listen'] != scope['listen']
            or value['configuration']['exposure'] != 'loopback'
            or value['compatibility']['overallStatus'] != 'COMPATIBLE'
            or value['compatibility']['actions']
            or value['platform']['compatible'] is not True
            or value['release'] != boundary['material']['release']):
        raise DevelopmentBoundaryError('DEVELOPMENT_INSTALLER_PLAN_OUT_OF_SCOPE')
    effects = [{'action': step, 'depends_on': list(_STEPS[max(0, index - 1):index]),
        'resource_scope': 'resources', 'source': 'execution_inventory_digest',
        'material_source': 'material'} for index, step in enumerate(_STEPS)]
    body = {'schema': PLAN_SCHEMA, 'boundary_digest': boundary['boundary_digest'],
        'binding': boundary['binding'], 'baseline_digest': boundary['baseline_digest'],
        'platform_plan': platform_value, 'installer_plan': value,
        'preparation_writes': boundary['preparation_writes'], 'effects': effects,
        'resources': scope, 'material': boundary['material'],
        'execution_inventory_digest': boundary['binding']['execution_inventory_digest'],
        'retention': boundary['retention']}
    return {**_copy(body), 'operation_plan_digest': _digest(body)}


def validate_operation_result(observation, binding, installer_output):
    boundary = validate_runtime_operation_boundary(binding.get('runtime_operation_boundary'), binding)
    if (type(observation) is not dict or set(observation) != {'schema', 'boundary_digest',
            'session_id', 'plan', 'executions', 'root_observation'}
            or observation['schema'] != 'animemo.runtime-development-plan-consumption/v1'
            or observation['boundary_digest'] != boundary['boundary_digest']
            or observation['session_id'] != binding['session_id'] or observation['executions'] != 1):
        raise DevelopmentBoundaryError('DEVELOPMENT_EXECUTION_RESULT_INVALID')
    plan = observation['plan']
    if (type(plan) is not dict or plan.get('boundary_digest') != boundary['boundary_digest']
            or plan.get('binding') != boundary['binding']
            or plan.get('operation_plan_digest') != _digest({k: v for k, v in plan.items()
                if k != 'operation_plan_digest'})
            or plan.get('installer_plan', {}).get('planDigest') != installer_output.get('installerPlanDigest')
            or plan.get('platform_plan') != installer_output.get('platformPlan')
            or type(observation['root_observation']) is not dict
            or observation['root_observation'] != {'uid': 0, 'euid': 0, 'platform': 'linux',
                'machine_id': boundary['guest_identity']['machine_id'],
                'boot_id': boundary['guest_identity']['boot_id'],
                'docker_storage_root': boundary['resources']['docker_storage_root'],
                'platform_plan_digest': plan.get('platform_plan', {}).get('planDigest'),
                'baseline_facts': observation['root_observation'].get('baseline_facts')}):
        raise DevelopmentBoundaryError('DEVELOPMENT_EXECUTION_RESULT_CHANGED')
    try:
        expected = _full_operation_values(boundary, plan['platform_plan'], plan['installer_plan'])
    except (KeyError, TypeError, ValueError):
        raise DevelopmentBoundaryError('DEVELOPMENT_EXECUTION_RESULT_CHANGED') from None
    if plan != expected:
        raise DevelopmentBoundaryError('DEVELOPMENT_EXECUTION_RESULT_CHANGED')
    from scripts.runtime_development_boundary import compare_runtime_baseline_facts
    actual = compare_runtime_baseline_facts(boundary['baseline_observation'], observation['root_observation']['baseline_facts'])
    _check_root_facts_against_platform(actual, plan['platform_plan'])
    installed = plan['installer_plan']
    expected_result = {'resultIdentity': 'animemo.install-result/v1', 'outcome': 'SUCCEEDED',
        'operationId': installed['operationId'], 'mode': installed['mode'],
        'instanceName': installed['instanceName'], 'instanceId': installed['configuration']['instanceId'],
        'release': installed['release'], 'state': 'succeeded', 'reasonCode': 'INSTALL_FRESH_SUCCEEDED',
        'warnings': installed['warnings'], 'recoveryRequired': False,
        'completedSteps': installed['executionSteps']}
    if installer_output.get('installerResult') != expected_result:
        raise DevelopmentBoundaryError('DEVELOPMENT_INSTALLER_RESULT_CHANGED')
    return observation
