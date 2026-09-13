"""Fixed local Installer composition with an independently sealed service tree."""
import argparse
import io
import json
from contextlib import redirect_stdout

from installer import cli
from installer.development import (
    DevelopmentServiceError,
    acquire_development_service_source,
)
from installer.production import build_candidate_composition
from installer.runtime import InstallerError
from release.materials import reject_duplicate_json_keys
from scripts.candidate_diagnostics import inherited_writer
from scripts.development_profile_runner import validate_binding


class _BoundedJsonOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.count = 0

    def write(self, value):
        self.count += len(value.encode('utf-8'))
        if self.count > 8 * 1024 * 1024:
            raise ValueError('DEVELOPMENT_INSTALLER_OUTPUT_LIMIT')
        return super().write(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True)
    parser.add_argument('--profile', required=True,
        choices=('ONLINE_FRESH', 'ONLINE_EXISTING_DOCKER', 'OFFLINE_VALIDATE_ONLY'))
    parser.add_argument('--public-origin', required=True)
    arguments = parser.parse_args(argv)
    diagnostic = inherited_writer()
    if diagnostic is None:
        return 2
    composition = None
    try:
        if len(arguments.binding) > 8192:
            raise DevelopmentServiceError()
        binding = validate_binding(json.loads(arguments.binding, object_pairs_hook=reject_duplicate_json_keys))
        service_source = acquire_development_service_source(binding)
        args = cli._parser().parse_args(['candidate', '--verified-candidate-digest', binding['verified_candidate_digest'],
            '--profile', arguments.profile, '--public-origin', arguments.public_origin, '--execute', '--accept', '--json'])
        request = cli._candidate_request(args)
        composition = build_candidate_composition(binding['verified_candidate_digest'],
            profile=arguments.profile, instance_name=request.instance_name,
            _development_service_source=service_source)
        output = _BoundedJsonOutput()
        with redirect_stdout(output):
            code = cli._run_candidate_composition(args, request, composition, diagnostic)
        value = json.loads(output.getvalue(), object_pairs_hook=reject_duplicate_json_keys)
        if code == 0:
            value['developmentServiceSourceObservation'] = service_source.observe_installed()
        print(json.dumps(value, sort_keys=True))
        return code
    except InstallerError as error:
        print(json.dumps({'outcome': error.outcome.value, 'reasonCode': error.code}))
        return cli._exit_code(error)
    except DevelopmentServiceError:
        diagnostic.error('DEVELOPMENT_SERVICE_SOURCE_MISMATCH')
        print(json.dumps({'outcome': 'ENVIRONMENT_FAILED', 'reasonCode': 'DEVELOPMENT_SERVICE_SOURCE_MISMATCH'}))
        return 5
    except BaseException:
        diagnostic.error('DEVELOPMENT_INSTALLER_ENTRY_FAILED')
        print(json.dumps({'outcome': 'ENVIRONMENT_FAILED', 'reasonCode': 'DEVELOPMENT_INSTALLER_ENTRY_FAILED'}))
        return 5
    finally:
        if composition is not None:
            composition.close_candidate_runtime()


if __name__ == '__main__':
    raise SystemExit(main())
