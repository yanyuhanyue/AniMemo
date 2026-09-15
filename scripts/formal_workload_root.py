"""One fixed root operation after same-connection Formal grant consumption."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.closed_runtime_inventory import _file_state, _open_directory_chain, closed_runtime_inventory_digest
    from scripts.candidate_workload_root import _copy_stage, _root_directory


def _offline_guest_egress(session_id,ssh_flow,expected_guest_ip):
    """Block new external host/container traffic, retaining the verified SSH flow."""
    chain='AMF'+session_id[:12]
    if (type(ssh_flow) is not list or len(ssh_flow)!=4 or ssh_flow[2]!=expected_guest_ip
            or ssh_flow[3]!='22' or not ssh_flow[1].isdecimal() or not 0<int(ssh_flow[1])<65536):
        raise ValueError('FORMAL_SSH_FLOW_INVALID')
    client=ipaddress.ip_address(ssh_flow[0])
    guest=ipaddress.ip_address(ssh_flow[2])
    if client.version!=guest.version:
        raise ValueError('FORMAL_SSH_FLOW_INVALID')
    commands=[]
    for executable in ('/usr/sbin/iptables','/usr/sbin/ip6tables'):
        rules=[['-N',chain]]
        if (executable.endswith('/iptables') and guest.version==4
                or executable.endswith('/ip6tables') and guest.version==6):
            rules.append(['-A',chain,'-s',str(guest),'-d',str(client),'-p','tcp',
                '--sport','22','--dport',ssh_flow[1],'-m','conntrack','--ctstate','ESTABLISHED',
                '--ctdir','REPLY','-j','ACCEPT'])
        rules += [['-A',chain,'-o',interface,'-j','ACCEPT'] for interface in ('lo','docker0','br+')]
        rules += [['-A',chain,'-j','REJECT'],['-I','OUTPUT','1','-j',chain],['-I','FORWARD','1','-j',chain],
            ['-C','OUTPUT','-j',chain],['-C','FORWARD','-j',chain]]
        for arguments in rules:
            command=[executable,'-w','10',*arguments]
            subprocess.run(command,check=True,timeout=20,stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            commands.append(command)
    return {'schema':'animemo.formal-offline-guest-egress/v1','commands':commands,
        'policy':'REJECT_NEW_EXTERNAL_HOST_AND_CONTAINER_TRAFFIC'}


def run_fixed_formal(*,session_id,profile,authority_identity,inventory_digest,archive_digest,
                     ssh_flow,expected_guest_ip,diagnostic):
    if (os.geteuid()!=0 or re.fullmatch('[0-9a-f]{32}',session_id or '') is None
            or profile not in {'FORMAL_FRESH','FORMAL_DOCKER','FORMAL_OFFLINE'}
            or any(re.fullmatch('sha256:[0-9a-f]{64}',value or '') is None
                for value in (authority_identity,inventory_digest,archive_digest))):
        raise ValueError('FORMAL_ROOT_SCOPE_INVALID')
    os.umask(0o077)
    os.chdir('/')
    os.environ.clear()
    os.environ.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin',LANG='C.UTF-8',LC_ALL='C.UTF-8')
    diagnostic.stage('MATERIAL_FINALIZING')
    parent_path=Path('/var/lib/animemo/bootstrap-authority/v1')
    parent=_root_directory(parent_path)
    root=parent_path/'materials'
    try:
        os.mkdir('materials',0o700,dir_fd=parent)
        target=os.open('materials',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        try:
            source=_open_directory_chain(Path('/tmp')/('animemo-formal-'+session_id+'-'+profile))
            try:
                _copy_stage(source,target)
            finally:
                os.close(source)
            os.fchmod(target,0o500)
        finally:
            os.close(target)
        if closed_runtime_inventory_digest(root)!=inventory_digest:
            raise ValueError('FORMAL_STAGE_INVENTORY_MISMATCH')
        digest=hashlib.sha256()
        with os.fdopen(os.open(root/'installer-materials.tar',os.O_RDONLY|os.O_NOFOLLOW),'rb') as source:
            target_fd=os.open('installer-materials.tar',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=parent)
            with os.fdopen(target_fd,'wb') as output:
                while chunk:=source.read(1024*1024):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if 'sha256:'+digest.hexdigest()!=archive_digest:
            raise ValueError('FORMAL_PROTECTED_ARCHIVE_MISMATCH')
    finally:
        os.close(parent)
    diagnostic.stage('MATERIAL_VERIFIED')
    receipt=Path('/var/lib/animemo/formal-acceptance/profile-receipt-draft.json')
    parent=_root_directory(receipt.parent)
    try:
        if receipt.exists() or receipt.is_symlink():
            raise ValueError('FORMAL_RECEIPT_EXISTS')
        if profile=='FORMAL_OFFLINE':
            policy=_offline_guest_egress(session_id,ssh_flow,expected_guest_ip)
            with (receipt.parent/'offline-egress-policy.json').open('x',encoding='utf-8') as stream:
                json.dump(policy,stream,sort_keys=True)
        program=('import sys;sys.path.insert(0,'+repr(str(root))+');'
            'from scripts.formal_runtime_entry import main;raise SystemExit(main())')
        environment=dict(os.environ)
        environment['ANIMEMO_FORMAL_PROFILE_CONTEXT_B64URL']=base64.urlsafe_b64encode(
            (json.dumps({'profile':profile,'rc_authority_identity':authority_identity},
                sort_keys=True,separators=(',',':'))+'\n').encode()).decode().rstrip('=')
        fd=os.dup(diagnostic.fd)
        try:
            environment['ANIMEMO_CANDIDATE_DIAGNOSTIC_FD']=str(fd)
            environment['ANIMEMO_CANDIDATE_DIAGNOSTIC_OPERATION']=diagnostic.operation
            completed=subprocess.run(['/usr/bin/python3','-I','-B','-c',program,
                '--authority-root',str(root),'--profile',profile],env=environment,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                pass_fds=(fd,),timeout=4*60*60)
        finally:
            os.close(fd)
        diagnostic.exited('RUNTIME_RUNNER',completed.returncode)
        if completed.returncode:
            diagnostic.error('RUNNER_EXECUTION_FAILED')
            raise ValueError('FORMAL_PROFILE_EXECUTION_FAILED')
        fd=os.open(receipt.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            before=os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=0
                    or before.st_mode&0o077 or not 0<before.st_size<=8*1024*1024):
                raise ValueError('FORMAL_RECEIPT_INVALID')
            with os.fdopen(fd,'rb',closefd=False) as stream:
                body=stream.read(before.st_size+1)
            if len(body)!=before.st_size or _file_state(os.fstat(fd))!=_file_state(before):
                raise ValueError('FORMAL_RECEIPT_CHANGED')
            diagnostic.stage('DRAFT_RETURNED')
            diagnostic.frame(b'R',body)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
