"""Fixed bootstrap for the development Runner and the baseline's offline wheels."""
from pathlib import Path
import runpy
import sys

from scripts.candidate_diagnostics import inherited_writer


def main(material_root, *, workload_mode='CLEAN_PREACCEPTANCE'):
    diagnostic = inherited_writer()
    if diagnostic is None:
        return 2
    if workload_mode not in {'CLEAN_PREACCEPTANCE', 'PLATFORM_DIAGNOSTIC'}:
        diagnostic.error('RUNNER_CONTEXT_INVALID')
        return 2
    root = Path(__file__).resolve().parents[1]
    diagnostic.stage('RUNTIME_INITIALIZING')
    try:
        from installer.offline_python_runtime import install_wheel_runtime
        runtime = Path('/var/lib/animemo/local-development/python-runtime')
        install_wheel_runtime(Path(material_root) / 'installer-root/wheelhouse', runtime)
        sys.path.insert(1, str(runtime))
    except BaseException:
        diagnostic.error('RUNTIME_INITIALIZATION_FAILED')
        return 2
    diagnostic.stage('RUNTIME_READY')
    diagnostic.stage('RUNNER_STARTING')
    try:
        name = ('development_platform_diagnostic.py' if workload_mode == 'PLATFORM_DIAGNOSTIC'
                else 'development_profile_runner.py')
        runpy.run_path(str(root / 'scripts' / name), run_name='__main__')
    except SystemExit as error:
        return error.code if type(error.code) is int and 0 <= error.code <= 255 else (0 if error.code is None else 2)
    except BaseException:
        diagnostic.error('RUNNER_EXECUTION_FAILED')
        return 2
    return 0
