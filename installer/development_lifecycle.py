"""Stop only the instance created by one conditional Runtime DEV operation."""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path

from release.candidate import canonical_json_bytes, sha256_bytes

SCHEMA = 'animemo.runtime-development-instance-stop/v1'
SERVICES = ('api', 'web', 'postgres', 'redis')


class DevelopmentStopError(RuntimeError):
    code = 'DEVELOPMENT_INSTANCE_STOP_UNVERIFIED'

    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(self.code)


def _data_identity(path):
    if not path.exists() and not path.is_symlink():
        return None
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError('unsafe data root')
    return {'path': str(path), 'device': metadata.st_dev, 'inode': metadata.st_ino}


def stop_runtime_instance(fresh, plan, gate):
    """Run before losing the workload role. Failure never authorizes retention PASS.

    Docker ownership is read before each exact-ID stop; a newly appeared or
    foreign container blocks closure. No network/volume/container removal occurs.
    The injected command runner is also the normal production observation seam.
    """
    from durability.instance import instance_namespace
    gate.require_cleanup(plan)
    if fresh.namespace != instance_namespace('default'):
        raise ValueError('DEVELOPMENT_STOP_INSTANCE_SCOPE_CHANGED')
    if hasattr(fresh.runner, '_development_gate'):
        if fresh.runner._development_gate is not gate:
            raise ValueError('DEVELOPMENT_STOP_RUNNER_CHANGED')
        fresh.runner._development_cleanup = True
    body = {'schema': SCHEMA, 'sessionId': gate.binding['session_id'],
        'parentPlanDigest': gate.binding['plan_digest'],
        'installerPlanDigest': None if plan is None else plan.plan_digest,
        'instanceName': str(fresh.namespace.name),
        'composeProject': fresh.namespace.compose_project,
        'updaterService': fresh.namespace.updater_service,
        'containers': [], 'serviceStopped': False, 'listenerClosed': False,
        'dataRetention': None, 'result': 'FAIL', 'failures': []}
    attempted = gate.execution_started
    if not attempted:
        fresh.close_candidate_listener()
        body.update(result='NOT_STARTED', listenerClosed=True)
        return {**body, 'receiptDigest': sha256_bytes(canonical_json_bytes(body))}
    if plan is None or fresh._development_service_source is None:
        raise DevelopmentStopError(body)
    namespace = fresh.namespace
    data_path = Path(str(namespace.data_root))
    data_before = None
    try:
        data_before = _data_identity(data_path)
    except Exception:  # noqa: BLE001 - keep the other bounded cleanup steps available
        body['failures'].append('DATA_IDENTITY_UNAVAILABLE')

    def run(argv, seconds=30):
        gate.require_cleanup(plan)
        result = fresh.runner.run(argv, timeout=gate.lifetime.instance_stop_timeout(seconds))
        if result.returncode != 0:
            raise ValueError('stop/readback command failed')
        return result.stdout.strip()

    # Starting was marked before the enable call, including an ambiguous failure.
    # A service never started by this operation is never stopped by name alone.
    try:
        if getattr(fresh, '_development_updater_start_attempted', False):
            fresh._development_service_source.observe_installed()
            value = run(['/usr/bin/systemctl', 'show', namespace.updater_service,
                '--property=Id', '--property=FragmentPath', '--value']).splitlines()
            if sorted(value) != sorted([namespace.updater_service,
                    '/etc/systemd/system/animemo-updater@.service']):
                raise ValueError('service owner mismatch')
            run(['/usr/bin/systemctl', 'stop', namespace.updater_service], 60)
            value = run(['/usr/bin/systemctl', 'show', namespace.updater_service,
                '--property=ActiveState', '--property=MainPID', '--value']).splitlines()
            if sorted(value) != ['0', 'inactive']:
                raise ValueError('service not stopped')
        body['serviceStopped'] = True
    except Exception:  # noqa: BLE001 - record closure failure without hiding the primary error
        body['failures'].append('UPDATER_STOP_UNVERIFIED')

    def inventory():
        raw = run(['/usr/bin/docker', 'ps', '--all', '--no-trunc', '--filter',
            'label=com.docker.compose.project=' + namespace.compose_project,
            '--format', '{{.ID}}'])
        ids = raw.splitlines() if raw else []
        if len(ids) > 16 or len(ids) != len(set(ids)) or any(
                not re.fullmatch('[0-9a-f]{64}', item) for item in ids):
            raise ValueError('container inventory invalid')
        return sorted(ids)

    def owned(identifier):
        template = ('[{{json .Id}},{{json (index .Config.Labels "io.animemo.instance-name")}},'
            '{{json (index .Config.Labels "io.animemo.instance-id")}},'
            '{{json (index .Config.Labels "io.animemo.compose-project")}},'
            '{{json (index .Config.Labels "com.docker.compose.service")}},'
            '{{json .State.Running}}]')
        value = json.loads(run(['/usr/bin/docker', 'inspect', '--format', template, identifier]))
        if (type(value) is not list or len(value) != 6
                or value[:4] != [identifier, str(namespace.name),
                    plan.configuration.instance_id, namespace.compose_project]
                or value[4] not in SERVICES or type(value[5]) is not bool):
            raise ValueError('container owner mismatch')
        return value

    try:
        ids = inventory()
        # Check the entire set before making the first stop call.
        for identifier in ids:
            owned(identifier)
        if hasattr(fresh.runner, '_development_gate'):
            fresh.runner._development_stop_ids = frozenset(ids)
        for identifier in ids:
            before = owned(identifier)
            if before[5]:
                run(['/usr/bin/docker', 'stop', '--time', '20', identifier], 30)
            after = owned(identifier)
            if after[:5] != before[:5] or after[5]:
                raise ValueError('container not stopped')
            body['containers'].append({'id': identifier, 'service': after[4], 'running': False})
        if inventory() != ids:
            raise ValueError('container set changed')
    except Exception:  # noqa: BLE001 - still close the listener after failed business stop
        body['failures'].append('CONTAINER_STOP_UNVERIFIED')
    try:
        fresh.close_candidate_listener()
        body['listenerClosed'] = fresh._candidate_listener is None
        if not body['listenerClosed']:
            raise ValueError('listener live')
    except Exception:  # noqa: BLE001 - fail closed with a bounded public cleanup code
        body['failures'].append('LISTENER_CLOSE_UNVERIFIED')
    try:
        data_after = _data_identity(data_path)
        if data_before != data_after:
            raise ValueError('data identity changed')
        body['dataRetention'] = {'state': 'RETAINED' if data_after else 'NOT_CREATED',
            'identity': data_after}
    except Exception:  # noqa: BLE001 - no successful retention claim on uncertain readback
        body['failures'].append('DATA_RETENTION_UNVERIFIED')
    body['result'] = 'PASS' if not body['failures'] else 'FAIL'
    receipt = {**body, 'receiptDigest': sha256_bytes(canonical_json_bytes(body))}
    if body['failures']:
        raise DevelopmentStopError(receipt)
    return receipt


def validate_instance_stop(receipt, *, binding, installer_plan_digest=None, require_data=True):
    """Shared host/result consumer. A stopped VM is not a business-stop receipt."""
    fields = {'schema', 'sessionId', 'parentPlanDigest', 'installerPlanDigest',
        'instanceName', 'composeProject', 'updaterService', 'containers',
        'serviceStopped', 'listenerClosed', 'dataRetention', 'result', 'failures', 'receiptDigest'}
    if (type(receipt) is not dict or set(receipt) != fields or receipt['schema'] != SCHEMA
            or receipt['sessionId'] != binding['session_id']
            or receipt['parentPlanDigest'] != binding['plan_digest']
            or receipt['instanceName'] != 'default' or receipt['composeProject'] != 'animemo-default'
            or receipt['updaterService'] != 'animemo-updater@default.service'
            or receipt['result'] != 'PASS' or receipt['failures'] != []
            or receipt['serviceStopped'] is not True or receipt['listenerClosed'] is not True
            or installer_plan_digest is not None and receipt['installerPlanDigest'] != installer_plan_digest
            or type(receipt['containers']) is not list
            or any(type(item) is not dict or set(item) != {'id', 'service', 'running'}
                or not re.fullmatch('[0-9a-f]{64}', str(item['id']))
                or item['service'] not in SERVICES or item['running'] is not False
                for item in receipt['containers'])
            or require_data and (type(receipt['dataRetention']) is not dict
                or receipt['dataRetention'].get('state') != 'RETAINED'
                or type(receipt['dataRetention'].get('identity')) is not dict
                or receipt['dataRetention']['identity'].get('path') != '/data/animemo-instances/default')
            or receipt['receiptDigest'] != sha256_bytes(canonical_json_bytes(
                {key: value for key, value in receipt.items() if key != 'receiptDigest'}))):
        raise DevelopmentStopError(receipt)
    return receipt
