"""Run the real published Installer from its protected, archive-bound modules."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from scripts.candidate_diagnostics import inherited_writer


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authority-root',required=True,type=Path)
    parser.add_argument('--profile',required=True,choices=('FORMAL_FRESH','FORMAL_DOCKER','FORMAL_OFFLINE'))
    args=parser.parse_args(argv)
    diagnostic=inherited_writer()
    if diagnostic is None or os.geteuid()!=0:
        return 2
    root=Path(__file__).resolve().parents[1]
    if root!=Path('/var/lib/animemo/bootstrap-authority/v1/materials') or args.authority_root!=root:
        diagnostic.error('RUNTIME_INITIALIZATION_FAILED')
        return 2
    diagnostic.stage('RUNTIME_INITIALIZING')
    try:
        from installer.offline_python_runtime import install_wheel_runtime
        runtime=root.parent/'installer-runtime'
        install_wheel_runtime(root/'wheelhouse',runtime)
        sys.path.insert(1,str(runtime))
        from scripts.formal_profile_runner import _load_authority
        authority,_,_=_load_authority(root)
        from installer.formal_bootstrap import prepare_offline_stage0, prepare_online_gh
        if args.profile=='FORMAL_OFFLINE':
            prepare_offline_stage0(root,authority)
        else:
            prepare_online_gh(root)
    except BaseException:
        diagnostic.error('RUNTIME_INITIALIZATION_FAILED')
        return 2
    diagnostic.stage('RUNTIME_READY')
    diagnostic.stage('RUNNER_STARTING')
    diagnostic.stage('RUNNER_STARTED')
    from scripts.formal_profile_runner import main as run_profile
    return run_profile(['--authority-root',str(root),'--profile',args.profile,'--execute'])
