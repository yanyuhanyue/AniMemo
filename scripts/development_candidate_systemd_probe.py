"""Temporary actual systemd parser/runtime regression on disposable CI only."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

assert os.name == 'posix' and os.geteuid() == 0 and os.environ.get('GITHUB_ACTIONS') == 'true'
source = Path.cwd()
name = 'animemo-candidate-isolation-probe-' + str(os.getpid()) + '.service'
unit = Path('/run/systemd/system') / name
dropin = unit.with_name(name + '.d')
override = dropin / '10-candidate-network-isolation.conf'
check = Path('/run') / (name + '.py')
result = {'scope': 'DEVELOPMENT_SYSTEMD_CANDIDATE_DROPIN_ONLY', 'status': 'ERROR',
          'vm_start_count': 0, 'capture_count': 0}
phase = 'prepare'
def run(*args, codes=(0,)):
    completed = subprocess.run(['/usr/bin/systemctl', *args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45)
    assert len(completed.stdout) < 4096 and completed.returncode in codes
    return completed.stdout.decode('ascii').strip()
try:
    parsed = ast.parse((source / 'installer/production.py').read_bytes())
    corrected = next(ast.literal_eval(n.value) for n in parsed.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == '_CANDIDATE_SYSTEMD_NETWORK_ISOLATION_BYTES' for t in n.targets))
    assert corrected == b'[Service]\nRestrictAddressFamilies=\nRestrictAddressFamilies=AF_UNIX AF_NETLINK\n'
    original = corrected.replace(b'RestrictAddressFamilies=\n', b'')
    base = [line for line in (source / 'deploy/updater/animemo-updater@.service').read_text().splitlines()
            if line.startswith('RestrictAddressFamilies=')]
    assert base == ['RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6']
    check.write_text('import socket\nfor family in (socket.AF_INET,socket.AF_INET6):\n'
        ' try: s=socket.socket(family); s.close()\n except OSError: pass\n'
        ' else: raise SystemExit(2)\nsocket.socket(socket.AF_UNIX).close()\n')
    check.chmod(0o444)
    with unit.open('x') as output:
        output.write('[Service]\nType=oneshot\nExecStart=/usr/bin/python3 ' + str(check)
            + '\nStandardOutput=null\nStandardError=null\n' + base[0] + '\n')
    dropin.mkdir()
    override.write_bytes(original)
    phase = 'red_original_dropin'
    run('daemon-reload')
    old = run('show', name, '--property', 'RestrictAddressFamilies', '--value').split()
    assert set(old) == {'AF_UNIX', 'AF_INET', 'AF_INET6', 'AF_NETLINK'}
    result['original_readback'] = old
    phase = 'green_corrected_dropin'
    override.write_bytes(corrected)
    run('daemon-reload')
    corrected_readback = run('show', name, '--property', 'RestrictAddressFamilies', '--value').split()
    result['corrected_readback'] = corrected_readback
    assert sorted(corrected_readback) == ['AF_NETLINK', 'AF_UNIX']
    run('start', name)
    assert run('show', name, '--property', 'ExecMainStatus', '--value') == '0'
    result['inet_socket_creation_rejected_unix_allowed'] = True
    result['status'] = 'PASS'
except BaseException as error:
    result.update(failure_stage=phase, failure_type=type(error).__name__)
finally:
    cleanup = []
    try:
        run('stop', name, codes=(0, 5))
        override.unlink(missing_ok=True)
        if dropin.exists(): dropin.rmdir()
        unit.unlink(missing_ok=True)
        check.unlink(missing_ok=True)
        run('daemon-reload')
    except BaseException:
        cleanup.append('OWNED_UNIT_CLEANUP_FAILED')
        result['status'] = 'ERROR'
    result['cleanup_errors'] = cleanup
    with Path(sys.argv[1]).open('x') as output: json.dump(result, output, indent=2)
    print(json.dumps(result))
raise SystemExit(0 if result['status'] == 'PASS' else 2)
