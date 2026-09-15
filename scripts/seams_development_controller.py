"""Fresh explicitly confirmed development session; no historical scope reuse."""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from scripts import development_controller as c


def serve(args):
    from scripts.local_candidate_development import run
    from release.formal_windows_pretrust import create_windows_private_named_directory, hold_windows_private_path_chain
    checkout = Path(__file__).resolve().parents[1]
    sha = c._git(checkout, 'rev-parse', 'HEAD').decode().strip()
    tree = c._git(checkout, 'rev-parse', 'HEAD^{tree}').decode().strip()
    common = (checkout/c._git(checkout, 'rev-parse', '--git-common-dir').decode('utf-8').strip()).resolve(strict=True)
    stable = c.stable_identities(checkout)
    c.verify_checkout(checkout, source_sha=sha, source_tree=tree, common_directory=common, stable=stable)
    root = args.control_root.resolve(strict=False)
    c._require(not root.exists() and root.parent.is_dir())
    create_windows_private_named_directory(root.parent, name=root.name)
    owners = []
    stopped = threading.Event()
    previous = {'digest': None}
    status_thread = None
    with hold_windows_private_path_chain(root, allow_leaf_child_writes=True):
        try:
            def status():
                try:
                    while not stopped.wait(0.5):
                        if owners:
                            owner = owners[0]
                            c.publish_status(root, {'schema':'animemo.local-development-controller-status/v1',
                                'owner':owner.record,'previous_result_sha256':previous['digest'],
                                'controller_source_sha':sha,'controller_source_tree':tree,
                                'stable_source_identities':stable},stopped=stopped,cancelled=lambda:owner.closed)
                except BaseException:
                    if owners:
                        owners[0].close('DEVELOPMENT_CONTROLLER_STATUS_FAILED')
                    stopped.set()
            status_thread = threading.Thread(target=status,daemon=True)
            status_thread.start()
            first = SimpleNamespace(**{**vars(args), 'execute':True, 'confirm_batch':True,
                'execution_source_sha':sha, 'execution_source_tree':tree,
                'result':root/'round-0001-result.json'})
            report = run(first,confirmed_owner_sink=owners)
            previous['digest'] = c._write_new(first.result,report)
            if not owners:
                return report
            owner = owners[0]
            owner.finish_round(report)
            c._write_new(root/'round-0001-owner.json',owner.record)
            while not owner.closed and not stopped.is_set():
                index = owner.record['last_reserved_round']+1
                request_path = root/f'request-{index:04d}.json'
                finish = root/'finish.json'
                if finish.exists():
                    value = c._json(c._read(finish,4096))
                    c._require(value == {'owner_id':owner.record['owner_id'],'previous_result_sha256':previous['digest']})
                    owner.close('DEVELOPMENT_SESSION_STOPPED')
                    break
                if not request_path.exists():
                    stopped.wait(0.5)
                    continue
                request = c._json(c._read(request_path,8192))
                next_checkout = c.validate_request(request,owner_record=owner.record,
                    previous_digest=previous['digest'],checkout_parent=checkout.parent,stable=stable)
                inventory = c.verify_checkout(next_checkout,source_sha=request['source_sha'],
                    source_tree=request['source_tree'],common_directory=common,stable=stable)
                with c.round_modules(next_checkout,inventory,stable) as entry:
                    options = SimpleNamespace(**{**vars(first),'confirm_batch':False,
                        'execution_source_sha':request['source_sha'],'execution_source_tree':request['source_tree'],
                        'result':root/f'round-{index:04d}-result.json'})
                    report = entry.run(options,session_owner=owner)
                    previous['digest'] = c._write_new(options.result,report)
                    owner.finish_round(report)
                c._write_new(root/f'round-{index:04d}-owner.json',owner.record)
            return report
        finally:
            stopped.set()
            primary = sys.exc_info()[1]
            cleanup_errors = []
            if owners:
                try:
                    owners[0].dispose()
                except BaseException:
                    cleanup_errors.append('DEVELOPMENT_OWNER_DISPOSE_FAILED')
                try:
                    c._write_new(root/'owner-final.json',owners[0].record)
                except BaseException:
                    cleanup_errors.append('DEVELOPMENT_OWNER_FINAL_WRITE_FAILED')
            if status_thread is not None:
                try:
                    status_thread.join(timeout=2)
                    c._require(not status_thread.is_alive())
                except BaseException:
                    cleanup_errors.append('DEVELOPMENT_STATUS_THREAD_CLEANUP_FAILED')
            if cleanup_errors:
                try:
                    c._write_new(root/'cleanup-errors.json', {'cleanup_errors':cleanup_errors,
                        'primary_failure':getattr(primary,'code','DEVELOPMENT_CONTROLLER_FAILED') if primary else None})
                except BaseException:
                    cleanup_errors.append('DEVELOPMENT_CLEANUP_RECORD_WRITE_FAILED')
                if primary is None:
                    raise c.DevelopmentOwnerError('DEVELOPMENT_CONTROLLER_CLEANUP_FAILED')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-root',required=True,type=Path)
    parser.add_argument('--authorization-id',required=True)
    parser.add_argument('--platform-diagnostic',action='store_true')
    parser.add_argument('--verified-candidate-digest',required=True)
    parser.add_argument('--qualification-run-id',required=True,type=int)
    parser.add_argument('--material-source-sha',required=True)
    parser.add_argument('--material-source-tree',required=True)
    args = parser.parse_args(argv)
    return 0 if serve(args)['status']=='PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
