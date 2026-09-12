"""Fixed verified runtime/Runner bootstrap, with standard-library diagnostics."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys

from scripts.candidate_diagnostics import inherited_writer


def main(runtime_directory=None):
    diagnostic = inherited_writer()
    if diagnostic is None:
        return 2
    root = Path(__file__).resolve().parents[1]
    diagnostic.stage('RUNTIME_INITIALIZING')
    try:
        from installer.offline_python_runtime import install_wheel_runtime
        runtime = runtime_directory or Path('/var/lib/animemo/candidate-acceptance/python-runtime')
        install_wheel_runtime(root / 'wheelhouse', runtime)
        sys.path.insert(0, str(runtime))
    except BaseException:
        diagnostic.error('RUNTIME_INITIALIZATION_FAILED')
        return 2
    diagnostic.stage('RUNTIME_READY')
    diagnostic.stage('RUNNER_STARTING')
    try:
        runpy.run_path(str(root / 'scripts/candidate_profile_runner.py'), run_name='__main__')
    except SystemExit as error:
        return error.code if type(error.code) is int and 0 <= error.code <= 255 else (0 if error.code is None else 2)
    except BaseException:
        diagnostic.error('RUNNER_INITIALIZATION_FAILED')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
