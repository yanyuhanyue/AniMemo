"""Explicit stopped retention for a single Runtime DEV plan only."""
from __future__ import annotations

import stat
from pathlib import Path

from installer.development_lifecycle import validate_instance_stop
from scripts import candidate_vm_harness as h


def _vmx_identity(path):
    path = Path(path)
    metadata = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(metadata.st_mode)
            or getattr(metadata, 'st_file_attributes', 0) & 0x400):
        raise h.CandidateHarnessError('DEVELOPMENT_RETAINED_CLONE_INVALID')
    return {'path': str(path.resolve(strict=True)), 'device': metadata.st_dev,
        'inode': metadata.st_ino, 'size': metadata.st_size,
        'sha256': h.sha256_bytes(path.read_bytes())}


def _retained_disk_graph(vmx):
    """Read descriptors and metadata, without rehashing multi-GB disk extents."""
    vmx = Path(vmx).resolve(strict=True)
    root = vmx.parent
    files = h.ClosedVmwareProvider._validate_clone_disk_graph(root, vmx)
    records = []
    for path in files:
        value = path.lstat()
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                or getattr(value, 'st_file_attributes', 0) & 0x400):
            raise h.CandidateHarnessError('DEVELOPMENT_RETAINED_DISK_INVALID')
        records.append({'path': path.relative_to(root).as_posix(), 'device': value.st_dev,
            'inode': value.st_ino, 'size': value.st_size, 'mtime_ns': value.st_mtime_ns,
            'ctime_ns': value.st_ctime_ns})
    return records


def retain_stopped_clone(provider, plan, profile, verified, receipt):
    from scripts.development_guest_session import development_binding
    from scripts.development_plan import DevelopmentHarnessPlan
    if (type(plan) is not DevelopmentHarnessPlan or plan.runtime_offline_only is not True
            or plan.runtime_retention_policy != 'STOP_AND_RETAIN'
            or tuple(item.profile for item in plan.profiles) != ('RUNTIME_BASE_OFFLINE',)
            or profile is not plan.profiles[0]
            or type(verified) is not h.VerifiedCloneConnection
            or verified.authority != provider._active_profile_authority(profile, plan)
            or provider._is_running(verified.authority.clone_vmx)):
        raise h.CandidateHarnessError('DEVELOPMENT_RETAINED_CLONE_INVALID')
    stop = receipt.get('installer_output', {}).get('developmentInstanceStop')
    validate_instance_stop(stop, binding=development_binding(plan))
    identity = _vmx_identity(verified.authority.clone_vmx)
    body = {'schema': 'animemo.runtime-development-retained-clone/v1',
        'parentPlanDigest': plan.plan_digest, 'sessionId': plan.session_id,
        'cloneIdentity': profile.clone_identity, 'retentionPolicy': 'STOP_AND_RETAIN',
        'powerState': 'STOPPED', 'vmxIdentity': identity,
        'diskGraph': _retained_disk_graph(verified.authority.clone_vmx),
        'instanceStopDigest': stop['receiptDigest']}
    return {'clone_disposition': 'RETAINED_STOPPED', 'instance_stop': stop,
        'retention_receipt': {**body, 'receiptDigest': h.sha256_bytes(h.canonical_json_bytes(body))}}


def validate_retained_operation(operation, plan):
    try:
        stop, receipt = operation['instance_stop'], operation['retention_receipt']
        profile = plan['profiles'][0]
        expected = {'schema': 'animemo.runtime-development-retained-clone/v1',
            'parentPlanDigest': plan['planDigest'], 'sessionId': plan['sessionId'],
            'cloneIdentity': profile['cloneIdentity'], 'retentionPolicy': 'STOP_AND_RETAIN',
            'powerState': 'STOPPED', 'vmxIdentity': _vmx_identity(operation['clone_vmx']),
            'diskGraph': _retained_disk_graph(operation['clone_vmx']),
            'instanceStopDigest': stop['receiptDigest']}
        if (plan.get('runtimeOfflineOnly') is not True
                or plan.get('runtimeRetentionPolicy') != 'STOP_AND_RETAIN'
                or [item['profile'] for item in plan['profiles']] != ['RUNTIME_BASE_OFFLINE']
                or operation['clone_identity'] != profile['cloneIdentity']
                or operation['clone_disposition'] != 'RETAINED_STOPPED'
                or operation['power_state'] != 'STOPPED'
                or receipt != {**expected, 'receiptDigest': h.sha256_bytes(h.canonical_json_bytes(expected))}):
            raise ValueError('retention mismatch')
        validate_instance_stop(stop, binding={'session_id': plan['sessionId'],
            'plan_digest': plan['planDigest']})
    except Exception:  # noqa: BLE001 - public result boundary cannot expose host details
        raise h.CandidateHarnessError('DEVELOPMENT_RETAINED_CLONE_INVALID') from None
    return True
