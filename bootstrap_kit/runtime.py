"""Authenticated DEV kit runtime. No product code executes in this process.

The independently pinned seed holds every kit member throughout this child.
This child is intentionally not a production installation entry point.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path


def _activate(root, manifest_path, expected):
    # Only stdlib executes before the external manifest pin is checked.
    if (not sys.flags.isolated or not sys.flags.no_site or sys.version_info[:2] != (3, 12)
        or root != Path(__file__).absolute().parents[2]
        or manifest_path != root / 'manifest.json'
        or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected)):
        raise ValueError('BOOTSTRAP_KIT_RUNTIME_ENTRY_INVALID')
    with manifest_path.open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024 or 'sha256:' + hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('BOOTSTRAP_KIT_RUNTIME_MANIFEST_CHANGED')
    # -I -S removes CWD, PYTHONPATH, user site and .pth loading. Vendor modules
    # below are authenticated tar members, never the host's third-party site.
    sys.path[:0] = [str(root / 'library'), str(root / 'vendor')]
    from bootstrap_kit.manifest import OperatorSelection, validate_manifest
    value = json.loads(raw)
    selected = OperatorSelection(expected, value['platform'], value['sequence'], value['kitVersion'],
        value['entry']['sha256'], value['entry']['size'], value['source']['commit'], value['source']['tree'])
    # The seed already enforced the *external* floors. Rechecking the same
    # hash-bound value here cannot expand the caller's earlier authorization.
    return validate_manifest(raw, selected, now=datetime.now(timezone.utc), production=False)


class _ObservedClient:
    def __init__(self, client, deadline):
        self.client, self.deadline, self.records = client, deadline, []

    def fetch(self, selection, *, deadline, **kwargs):
        try:
            result = self.client.fetch(selection, deadline=min(deadline, self.deadline), **kwargs)
        except Exception as error:  # noqa: BLE001 - closed code only; never log a URL, query or exception body.
            secondary = getattr(error, 'secondary_errors', ())
            secondary = secondary if type(secondary) in (list, tuple) else ('INVALID',)
            self.records.append({'kind': selection.kind, 'status': 'FAILED',
                'code': getattr(error, 'code', 'BOOTSTRAP_KIT_NETWORK_FAILED'),
                'secondary_errors': [code if type(code) is str and re.fullmatch(r'BOOTSTRAP_HTTP_[A-Z0-9_]{1,80}', code)
                    else 'BOOTSTRAP_KIT_SECONDARY_INVALID' for code in secondary[:4]],
                'diagnostics': getattr(self.client, 'last_diagnostics', None),
                'process': self.client.last_process_receipt})
            raise
        self.records.append({'kind': selection.kind, 'status': 'COMPLETE',
            'size': result.size, 'sha256': result.sha256,
            'http': list(result.observations), 'process': result.process_receipt})
        return result


def _record_tuf_verification(work, refreshed):
    from bootstrap_kit.manifest import canonical, require
    from bootstrap_kit.safe_files import exclusive_file
    try:
        with exclusive_file(work / 'tuf-verification.json') as stream:
            stream.write(canonical(refreshed))
    except (OSError, ValueError):
        require(False, 'BOOTSTRAP_KIT_TUF_RECORD_FAILED')


def execute(args):
    root, manifest_path = Path(args.kit_root).absolute(), Path(args.manifest).absolute()
    value = _activate(root, manifest_path, args.expected_manifest_sha256)
    from bootstrap_kit.safe_files import (held_file, file_digest, create_private_directory,
        exclusive_file, safe_chain)
    from bootstrap_kit.manifest import require, canonical
    from bootstrap_kit.http_supervisor import SupervisedAnonymousHttp
    from bootstrap_kit.egress import bound_runtime_egress
    egress = bound_runtime_egress(getattr(args, 'egress_proxy', None),
        getattr(args, 'egress_sha256', None), offline=getattr(args, 'offline', False))
    expected_egress = os.environ.get('ANIMEMO_EXPECTED_EGRESS_SHA256')
    if expected_egress is not None or egress.endpoint is not None:
        require(egress.endpoint is not None and expected_egress == egress.identity,
                'BOOTSTRAP_KIT_EGRESS_BINDING_MISMATCH')
    require(type(args.version) is str and len(args.version) <= 128
            and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?', args.version),
            'BOOTSTRAP_KIT_DEV_SUBJECT_INVALID')
    output_parent = safe_chain(Path(args.output).absolute())
    work = create_private_directory(output_parent, prefix='kit-local-validation-')
    deadline = time.monotonic() + 1800
    client = _ObservedClient(SupervisedAnonymousHttp(private_root=work, egress=egress,
        offline=getattr(args, 'offline', False)), deadline)
    with ExitStack() as holds:
        # The seed remains the prior authority. Child readback keeps dependency
        # bytes held even when this local diagnostic is invoked directly.
        for member in value['members']:
            stream = holds.enter_context(held_file(root / member['path'], 64 * 1024 * 1024))
            require(file_digest(stream) == (member['sha256'], member['size']),
                    'BOOTSTRAP_KIT_RUNTIME_MEMBER_CHANGED')
        from installer.tokenless_stage0 import TokenlessActionsVerifier
        from installer.anonymous_release_transport import AnonymousReleaseReader
        from updater.source import AnonymousGitHubRest, GitHubReleaseSource
        from bootstrap_kit.trust import refresh_trust, load_local_trust
        from installer.bootstrap import authorize_online_stage0_test_only
        material = load_local_trust(root / 'trust')
        primary = None
        phase = 'TUF'
        completed = []
        def consume_phase(value):
            nonlocal phase
            if value == 'TEST_ONLY_CONSUMPTION':
                completed.append('PLATFORM_PROOF')
            phase = value
        try:
            refreshed = refresh_trust(verifier=material.verifier_path, output=work / 'fresh-tuf',
                                      client=client, deadline=deadline, retain_evidence=True,
                                      minimum_profile=material.profile)
            # Use the continuously verified current roots for the subsequent
            # proof checks, while leaving the operator-selected kit untouched.
            require(type(refreshed) is dict and refreshed.get('authority') == 'DEVELOPMENT_ONLY'
                    and refreshed.get('state') == 'TEST_ONLY' and refreshed.get('status') == 'VERIFIED',
                    'BOOTSTRAP_KIT_TUF_RESULT_INVALID')
            material = load_local_trust(work / 'fresh-tuf')
            require(material.profile.identity == refreshed['profileIdentity']
                    and material.profile.verifier_identity == refreshed['verifierIdentity'],
                    'BOOTSTRAP_KIT_TUF_RESULT_INVALID')
            _record_tuf_verification(work, refreshed)
            completed.append('CURRENT_TUF')
            phase = 'PRODUCT_BYTES_AND_ACTIONS'
            source = GitHubReleaseSource(work / 'release-cache', rest=AnonymousGitHubRest(
                http_client=client, private_root=work), actions_verifier=TokenlessActionsVerifier(
                    material, scratch_parent=work))
            with source.open_verified_release(args.version, evidence_parent=work) as transaction:
                verified = transaction.materials
                completed.extend(('ANONYMOUS_METADATA', 'COMPLETE_ASSET', 'ACTIONS_AND_PRODUCT_BYTES', 'EXTRACTED_MEMBERS'))
                phase = 'RAW_ARCHIVE_HANDOFF'
                with transaction.archive_for(verified, source=source) as archive:
                    completed.append('RAW_ARCHIVE_HANDOFF')
                    with exclusive_file(work / 'release-source-observation.json') as stream:
                        stream.write(canonical(transaction.observation))
                    phase = 'PLATFORM_PROOF'
                    inputs = AnonymousReleaseReader(http_client=client, private_root=work).fetch(args.version)
                    transaction.check_platform_source(inputs)
                    with exclusive_file(work / 'platform-release-bundle.json') as stream:
                        stream.write(inputs.release_bundle)
                    observed = authorize_online_stage0_test_only(tag=args.version,
                        release_commit=verified.manifest['release']['commit'], archive=archive,
                        verified_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        material=material, inputs=inputs, scratch_parent=work, on_phase=consume_phase)
                    require(observed['state'] == 'TEST_ONLY', 'BOOTSTRAP_KIT_RUNTIME_AUTHORITY_REJECTED')
                    completed.append('TEST_ONLY_CONSUMPTION')
                    with held_file(archive, 512 * 1024 * 1024) as stream:
                        digest, _ = file_digest(stream)
                    phase = 'ARCHIVE_OWNER_EXIT'
            completed.append('ARCHIVE_OWNER_EXIT')
            phase = 'FINAL_RESULT'
            report = dict(state='TEST_ONLY', production_authority_granted=False,
                subject=args.version, platform_release_signature='VERIFIED', actions_provenance='VERIFIED',
                tuf_chain='VERIFIED', large_asset_sha256=digest.removeprefix('sha256:'),
                product_execution='NOT_RUN', bootstrap_commit='TEST_ONLY')
            with exclusive_file(work / 'verification.json') as stream:
                stream.write(canonical({'result': report, 'source': value['source'],
                    'kit_manifest_sha256': args.expected_manifest_sha256, 'http': client.records,
                    'completed_stages': completed, 'test_only_consumption': observed}))
            return report
        except BaseException as error:
            primary = error
            code = getattr(error, 'code', '')
            if type(code) is not str or re.fullmatch(r'(?:BOOTSTRAP|TRANSPORT)_[A-Z0-9_]{1,90}', code) is None:
                code = 'BOOTSTRAP_KIT_COMPONENT_FAILED'
            try:
                with exclusive_file(work / 'failure.json') as stream:
                    stream.write(canonical({'state': 'FAILED', 'phase': phase, 'code': code,
                        'completed_stages': completed, 'production_authority_granted': False}))
            except Exception:  # noqa: BLE001 - preserve the original failure if its local observation cannot be written.
                error.secondary_errors = (*getattr(error, 'secondary_errors', ()),
                    'BOOTSTRAP_KIT_OBSERVATION_WRITE_FAILED')[:4]
            raise
        finally:
            try:
                with exclusive_file(work / 'http-observations.json') as stream:
                    stream.write(canonical({'authority': 'NON_AUTHORITATIVE', 'requests': client.records}))
            except Exception:  # noqa: BLE001 - fixed secondary only, preserve the original verifier/transport failure.
                if primary is None:
                    raise ValueError('BOOTSTRAP_KIT_OBSERVATION_WRITE_FAILED') from None
                primary.secondary_errors = (*getattr(primary, 'secondary_errors', ()),
                    'BOOTSTRAP_KIT_OBSERVATION_WRITE_FAILED')[:4]


def main():
    parser = argparse.ArgumentParser()
    for name in ('kit-root', 'manifest', 'expected-manifest-sha256', 'version', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--egress-proxy')
    parser.add_argument('--egress-sha256')
    args = parser.parse_args()
    try:
        result = execute(args)
        print(json.dumps(result, sort_keys=True, separators=(',', ':')))
        return 0
    except Exception:  # noqa: BLE001 - kit CLI intentionally emits no private path or raw remote exception.
        print('{"state":"FAILED","production_authority_granted":false}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
