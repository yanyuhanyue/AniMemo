"""Real small-file ownership boundaries; these tests do not claim crypto PASS."""
# ruff: noqa: SIM117 -- Nested scopes make borrow and unwind boundaries explicit.
import copy
import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from bootstrap_kit.safe_files import (
    create_private_directory,
    directory_identity,
    remove_owned_directory,
)
from installer.anonymous_release_transport import UntrustedReleaseMaterials
from release.materials import (
    MaterialContractError,
    MaterialFileIdentity,
    VerifiedMaterialSet,
)
from updater import archive_handoff as handoff
from updater.authority import VerifiedReleaseMaterials
from updater.errors import RequestRejected
from updater.source import ReleaseAssetInventory, ReleaseAssetInventoryEntry
from updater.transport import (
    RELEASE_BUNDLE_OBJECTS,
    AcquiredTransportSet,
    GitHubAssetPlan,
    TransportError,
    TransportObjectPlan,
    TransportObjectReceipt,
    TransportReceipt,
    TransportRequest,
    TransportSourceId,
)

ARCHIVE = 'installer-materials.tar'
VERSION = 'v2.0.0-rc.3'
COMMIT = 'a' * 40


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def small_tar(mtime=0):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        member = tarfile.TarInfo('member.txt')
        member.size, member.mtime = 6, mtime
        archive.addfile(member, io.BytesIO(b'member'))
    return output.getvalue()


class ArchiveHandoffBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.parent = create_private_directory(Path(tempfile.gettempdir()), prefix='archive-boundary-')
        self.parent_identity = directory_identity(self.parent)
        self.source = object()

    def tearDown(self):
        # Only unwind owners created by this fixture, after injected failures end.
        # Production must retain uncertain owners; no background process is used here.
        for key, (owner, _error) in list(handoff._RETAINED.items()):
            if owner.root.parent == self.parent:
                owner._holds.close()
                if owner.root.exists():
                    remove_owned_directory(owner.root, directory_identity(owner.root))
                del handoff._RETAINED[key]
        remove_owned_directory(self.parent, self.parent_identity)

    def prepare(self, owner, *, inner=False):
        raw = small_tar()
        path = owner.root / ARCHIVE
        path.write_bytes(raw)
        identity = digest(raw)
        request = TransportRequest.release_bundle(VERSION[1:],
            object_plans=tuple(TransportObjectPlan(name, len(raw)) for name in RELEASE_BUNDLE_OBJECTS),
            github_release_id=392113678,
            github_assets=tuple(GitHubAssetPlan(name, 100 + index, identity)
                for index, name in enumerate(RELEASE_BUNDLE_OBJECTS)))
        objects = (TransportObjectReceipt(ARCHIVE, ARCHIVE, identity[7:], len(raw)),)
        receipt = TransportReceipt(TransportSourceId.GITHUB, request.identity, objects, 'fixture-receipt')
        acquired = AcquiredTransportSet(owner.root, objects, receipt)
        asset = ReleaseAssetInventoryEntry(ARCHIVE, 'uploaded', len(raw), 102, identity)
        inventory = ReleaseAssetInventory(VERSION, True, (asset,), 392113678)
        observed_asset = {'id': 102, 'name': ARCHIVE, 'state': 'uploaded', 'size': len(raw), 'digest': identity}
        observation = {'release': {'id': 392113678, 'tag_name': VERSION, 'draft': False,
            'prerelease': True, 'immutable': True, 'assets': [observed_asset]}}
        members = owner.root / 'members'
        members.mkdir(mode=0o700)
        name = ARCHIVE if inner else 'member.txt'
        (members / name).write_bytes(b'member')
        material_set = VerifiedMaterialSet(members, identity,
            (MaterialFileIdentity(name, digest(b'member'), 6, 0o600),))
        materials = VerifiedReleaseMaterials(
            {'release': {'version': VERSION, 'commit': COMMIT},
             'deployment': {'installerMaterials': {'sha256': identity}}},
            {'archive': {'sha256': identity, 'size': len(raw)}}, material_set, 'fixture-materials')
        return acquired, request, inventory, observation, materials

    @contextmanager
    def bound(self, *, inner=False):
        with handoff.release_workspace(self.source, self.parent) as owner:
            acquired, request, inventory, observation, materials = self.prepare(owner, inner=inner)
            owner._hold(acquired, request, inventory, observation)
            owner._bind(materials)
            yield owner, materials

    def inputs(self, owner):
        return UntrustedReleaseMaterials(VERSION, copy.deepcopy(owner.observation['release']),
            'b' * 40, COMMIT, b'{}', ())

    def test_real_member_api_rejects_outer_archive_and_context_keeps_original_alive(self):
        with self.bound() as (owner, materials):
            with self.assertRaises(MaterialContractError):
                materials.material(ARCHIVE)
            with owner.archive_for(materials, source=self.source) as path:
                self.assertEqual(path.read_bytes(), small_tar())
                owner.check_platform_source(self.inputs(owner))
                root = owner.root
            self.assertTrue(path.exists())
        self.assertFalse(root.exists())

    def test_same_named_inner_member_is_distinct_from_original_tar(self):
        with self.bound(inner=True) as (owner, materials):
            inner = materials.material(ARCHIVE)
            with owner.archive_for(materials, source=self.source) as outer:
                self.assertNotEqual(inner, outer)
                self.assertEqual(inner.read_bytes(), b'member')
                self.assertEqual(outer.read_bytes(), small_tar())

    def test_same_members_with_different_tar_header_rejected(self):
        with handoff.release_workspace(self.source, self.parent) as owner:
            acquired, request, inventory, observation, _ = self.prepare(owner)
            (owner.root / ARCHIVE).write_bytes(small_tar(mtime=1))
            with self.assertRaises(TransportError):
                owner._hold(acquired, request, inventory, observation)

    def test_wrong_request_receipt_digest_size_and_observation_rejected(self):
        for mutation in ('request', 'receipt_objects', 'digest', 'size', 'release', 'observation_asset'):
            with self.subTest(mutation=mutation), handoff.release_workspace(self.source, self.parent) as owner:
                acquired, request, inventory, observation, _ = self.prepare(owner)
                if mutation == 'request':
                    request = replace(request, github_release_id=392113679)
                elif mutation == 'receipt_objects':
                    acquired = replace(acquired, receipt=replace(acquired.receipt, objects=()))
                elif mutation in ('digest', 'size'):
                    asset = replace(inventory.assets[0], **({'digest': 'sha256:' + '0'*64}
                        if mutation == 'digest' else {'size': inventory.assets[0].size + 1}))
                    inventory = replace(inventory, assets=(asset,))
                elif mutation == 'release':
                    observation['release']['id'] += 1
                else:
                    observation['release']['assets'][0]['id'] += 1
                with self.assertRaises(handoff.ArchiveHandoffError):
                    owner._hold(acquired, request, inventory, observation)

    def test_inventory_asset_must_match_exact_request_asset(self):
        with handoff.release_workspace(self.source, self.parent) as owner:
            acquired, request, inventory, observation, _ = self.prepare(owner)
            inventory = replace(inventory, assets=(replace(inventory.assets[0], asset_id=999),))
            observation['release']['assets'][0]['id'] = 999
            with self.assertRaises(handoff.ArchiveHandoffError):
                owner._hold(acquired, request, inventory, observation)

    def test_wrong_material_archive_digest_or_size_rejected(self):
        for field in ('sha256', 'size'):
            with self.subTest(field=field), handoff.release_workspace(self.source, self.parent) as owner:
                acquired, request, inventory, observation, materials = self.prepare(owner)
                owner._hold(acquired, request, inventory, observation)
                materials.deployment_contract['archive'][field] = 'sha256:' + '0'*64 if field == 'sha256' else 1
                with self.assertRaises((handoff.ArchiveHandoffError, RequestRejected)):
                    owner._bind(materials)

    def test_second_borrow_and_closed_owner_rejected(self):
        with self.bound() as (owner, materials):
            with owner.archive_for(materials, source=self.source):
                pass
            with self.assertRaisesRegex(handoff.ArchiveHandoffError, 'ALREADY_BORROWED'):
                with owner.archive_for(materials, source=self.source):
                    self.fail('second borrow entered')
        with self.assertRaisesRegex(handoff.ArchiveHandoffError, 'UNAVAILABLE'):
            _ = owner.materials
        with self.bound() as (unused, materials):
            pass
        with self.assertRaisesRegex(handoff.ArchiveHandoffError, 'TRANSACTION_MISMATCH'):
            with unused.archive_for(materials, source=self.source):
                self.fail('closed owner entered')

    def test_cross_source_and_same_value_foreign_materials_rejected(self):
        for mode in ('source', 'materials'):
            with self.subTest(mode=mode), self.bound() as (owner, materials):
                with self.assertRaisesRegex(handoff.ArchiveHandoffError, 'TRANSACTION_MISMATCH'):
                    with owner.archive_for(replace(materials) if mode == 'materials' else materials,
                        source=object() if mode == 'source' else self.source):
                        self.fail('foreign transaction entered')

    def test_mutable_materials_or_source_observation_cannot_change_after_bind(self):
        for mode in ('materials', 'observation'):
            with self.subTest(mode=mode), self.bound() as (owner, materials):
                if mode == 'materials':
                    materials.manifest['release']['commit'] = 'c' * 40
                else:
                    owner._observation['release']['id'] += 1
                with self.assertRaises(handoff.ArchiveHandoffError):
                    with owner.archive_for(materials, source=self.source):
                        self.fail('mutated binding entered')

    def test_public_observation_is_a_copy_and_cannot_rebind_live_transaction(self):
        with self.bound() as (owner, materials):
            with owner.archive_for(materials, source=self.source):
                observation = owner.observation
                observation['release']['id'] += 1
                self.assertNotEqual(observation, owner.observation)
                owner.check_platform_source(self.inputs(owner))

    def test_closed_stream_rejected(self):
        with self.bound() as (owner, materials):
            owner._stream.close()
            with self.assertRaisesRegex(handoff.ArchiveHandoffError, 'UNAVAILABLE'):
                with owner.archive_for(materials, source=self.source):
                    self.fail('closed stream entered')

    @unittest.skipUnless(os.name == 'nt', 'Windows kernel share-mode boundary')
    def test_windows_kernel_blocks_write_truncate_delete_replace_and_directory_rename(self):
        with self.bound() as (owner, materials):
            with owner.archive_for(materials, source=self.source) as path:
                alternate = owner.root / 'replacement'
                alternate.write_bytes(small_tar(mtime=1))
                for operation in ('write', 'truncate', 'delete', 'replace', 'rename_directory'):
                    with self.subTest(operation=operation), self.assertRaises(OSError):
                        if operation == 'write':
                            with path.open('r+b') as stream:
                                stream.write(b'x')
                        elif operation == 'truncate':
                            with path.open('wb'):
                                pass
                        elif operation == 'delete':
                            path.unlink()
                        elif operation == 'replace':
                            os.replace(alternate, path)
                        else:
                            owner.root.rename(self.parent / 'renamed')
                owner._check_file()

    def _assert_truncated_fixture_closed(self):
        import errno

        self.assertFalse(self._truncated_owner._active)
        self.assertTrue(self._truncated_owner._stream.closed)
        with self.assertRaises(OSError) as closed:
            os.fstat(self._truncated_descriptor)
        self.assertEqual(closed.exception.errno, errno.EBADF)
        self.assertTrue(any(owner is self._truncated_owner
            for owner, _error in handoff._RETAINED.values()))

    def _assert_posix_truncation_rejected(self):
        from bootstrap_kit.safe_files import KitFileError

        with self.assertRaises(handoff.ArchiveHandoffError) as outer:
            with self.bound() as (owner, materials):
                self._truncated_owner = owner
                self._truncated_descriptor = owner._stream.fileno()
                with self.assertRaises(handoff.ArchiveHandoffError) as inner:
                    with owner.archive_for(materials, source=self.source) as path:
                        path.write_bytes(b'x')
                self.assertIs(type(inner.exception), handoff.ArchiveHandoffError)
                self.assertEqual(inner.exception.code, 'BOOTSTRAP_ARCHIVE_IDENTITY_MISMATCH')
                self.assertEqual(getattr(inner.exception, 'secondary_errors', ()), ())
        self.assertIs(type(outer.exception), handoff.ArchiveHandoffError)
        self.assertEqual(outer.exception.code, 'BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED')
        self.assertEqual(getattr(outer.exception, 'secondary_errors', ()), ())
        cause = BaseException.__context__.__get__(outer.exception)
        self.assertIs(type(cause), KitFileError)
        self.assertEqual(cause.args, ('BOOTSTRAP_KIT_FILE_CHANGED',))
        self.assertEqual(getattr(cause, 'secondary_errors', ()), ())
        self._assert_truncated_fixture_closed()

    @unittest.skipIf(os.name == 'nt', 'POSIX mutation is detected rather than kernel denied')
    def test_posix_truncation_during_borrow_is_detected_on_exit(self):
        self._assert_posix_truncation_rejected()

    @unittest.skipIf(os.name == 'nt', 'POSIX real held-file boundary')
    def test_posix_truncation_inner_rejection_cannot_be_masked_by_outer(self):
        original = handoff._ArchiveTransaction._check_file
        calls = []

        def omit_postread(owner):
            calls.append(True)
            if len(calls) != 3:
                return original(owner)
            return None

        with mock.patch.object(handoff._ArchiveTransaction, '_check_file', omit_postread):
            with self.assertRaises(AssertionError):
                self._assert_posix_truncation_rejected()
        self.assertEqual(len(calls), 3)
        self._assert_truncated_fixture_closed()

    @unittest.skipIf(os.name == 'nt', 'POSIX real held-file boundary')
    def test_posix_truncation_wrong_rejection_type_or_code_cannot_pass(self):
        original = handoff._ArchiveTransaction._check_file
        for error, rejected in ((TypeError(), TypeError),
                                (handoff.ArchiveHandoffError('UNAVAILABLE'), AssertionError)):
            calls = []

            def wrong_postread(owner):
                calls.append(True)
                if len(calls) == 3:
                    raise error
                return original(owner)

            with mock.patch.object(handoff._ArchiveTransaction, '_check_file', wrong_postread):
                with self.assertRaises(rejected):
                    self._assert_posix_truncation_rejected()
            self.assertEqual(len(calls), 3)
            self._assert_truncated_fixture_closed()

    @unittest.skipIf(os.name == 'nt', 'POSIX real held-file boundary')
    def test_posix_truncation_unrelated_cleanup_failure_is_not_expected(self):
        from bootstrap_kit.safe_files import KitFileError

        original_hold = handoff._ArchiveTransaction._hold

        def hold_with_secondary(owner, *args):
            original_hold(owner, *args)
            original_close = owner._holds.close

            def close_with_secondary():
                try:
                    original_close()
                except KitFileError as error:
                    error.secondary_errors = (*getattr(error, 'secondary_errors', ()),
                        'BOOTSTRAP_KIT_HANDLE_CLOSE_FAILED')
                    raise

            patcher = mock.patch.object(owner._holds, 'close', close_with_secondary)
            patcher.start()
            self.addCleanup(patcher.stop)

        with mock.patch.object(handoff._ArchiveTransaction, '_hold', hold_with_secondary):
            with self.assertRaises(AssertionError):
                self._assert_posix_truncation_rejected()
        self._assert_truncated_fixture_closed()

    def test_platform_metadata_and_commit_must_match_download_transaction(self):
        for field in ('id', 'tag_name', 'draft', 'prerelease', 'immutable', 'asset', 'commit'):
            with self.subTest(field=field), self.bound() as (owner, materials):
                inputs = self.inputs(owner)
                if field == 'asset':
                    inputs.release_metadata['assets'][0]['id'] += 1
                elif field == 'commit':
                    inputs = replace(inputs, tag_commit='c'*40)
                else:
                    inputs.release_metadata[field] = None
                with owner.archive_for(materials, source=self.source):
                    with self.assertRaisesRegex(handoff.ArchiveHandoffError, 'SOURCE_MISMATCH'):
                        owner.check_platform_source(inputs)

    def test_downstream_cancel_and_timeout_preserve_primary_and_cleanup(self):
        for failure in (KeyboardInterrupt(), TimeoutError()):
            with self.subTest(kind=type(failure).__name__):
                with self.assertRaises(type(failure)) as seen:
                    with self.bound() as (owner, materials):
                        root = owner.root
                        with owner.archive_for(materials, source=self.source):
                            raise failure
                self.assertIs(seen.exception, failure)
                self.assertFalse(root.exists())

    def test_postread_invalid_material_snapshot_never_masks_downstream_primary(self):
        for mode in ('unsupported', 'cycle'):
            failure = TimeoutError()
            with self.subTest(mode=mode):
                with self.assertRaises(TimeoutError) as seen:
                    with self.bound() as (owner, materials):
                        with owner.archive_for(materials, source=self.source):
                            if mode == 'unsupported':
                                materials.manifest['invalid'] = object()
                            else:
                                materials.manifest['invalid'] = materials.manifest
                            raise failure
                self.assertIs(seen.exception, failure)
                self.assertIn('BOOTSTRAP_ARCHIVE_POSTREAD_FAILED', failure.secondary_errors)

    def test_postread_source_mutation_preserves_downstream_primary(self):
        failure = BrokenPipeError()
        with self.assertRaises(BrokenPipeError) as seen:
            with self.bound() as (owner, materials):
                with owner.archive_for(materials, source=self.source):
                    owner._observation['release']['id'] += 1
                    raise failure
        self.assertIs(seen.exception, failure)
        self.assertIn('BOOTSTRAP_ARCHIVE_POSTREAD_FAILED', failure.secondary_errors)

    def test_unknown_cleanup_retains_original_holds_without_delete_or_retry(self):
        failure = TimeoutError()
        failure.secondary_errors = ('BOOTSTRAP_HTTP_PROCESS_CLEANUP_FAILED',)
        with mock.patch.object(handoff, 'remove_owned_directory', wraps=remove_owned_directory) as removal:
            with self.assertRaises(TimeoutError) as seen:
                with self.bound() as (owner, materials):
                    with owner.archive_for(materials, source=self.source):
                        raise failure
            self.assertIs(seen.exception, failure)
            self.assertTrue(owner.root.exists())
            self.assertFalse(owner._stream.closed)
            self.assertIn(directory_identity(owner.root), handoff._RETAINED)
            removal.assert_not_called()
            with self.assertRaises(handoff.ArchiveHandoffError):
                _ = owner.materials
            removal.assert_not_called()

    def test_remove_failure_is_fixed_and_attempted_once(self):
        with mock.patch.object(handoff, 'remove_owned_directory', side_effect=OSError()) as removal:
            with self.assertRaisesRegex(handoff.ArchiveHandoffError, 'CLEANUP_FAILED'):
                with self.bound() as (owner, _):
                    pass
            self.assertTrue(owner.root.exists())
            self.assertTrue(owner._stream.closed)
            removal.assert_called_once()

    def test_handle_close_failure_retains_owner_and_never_deletes_or_retries(self):
        with mock.patch.object(handoff, 'remove_owned_directory', wraps=remove_owned_directory) as removal:
            with self.assertRaisesRegex(handoff.ArchiveHandoffError, 'HANDLE_CLOSE_FAILED'):
                with self.bound() as (owner, _):
                    patcher = mock.patch.object(owner._holds, 'close', side_effect=OSError())
                    close = patcher.start()
                    self.addCleanup(patcher.stop)
            close.assert_called_once()
            removal.assert_not_called()
            self.assertTrue(owner.root.exists())
            self.assertFalse(owner._stream.closed)
            patcher.stop()

    def test_explicit_uncertain_cleanup_flag_retains_owner(self):
        failure = TimeoutError()
        failure.cleanup_uncertain = True
        with mock.patch.object(handoff, 'remove_owned_directory', wraps=remove_owned_directory) as removal:
            with self.assertRaises(TimeoutError):
                with self.bound() as (owner, _):
                    raise failure
            removal.assert_not_called()
            self.assertTrue(owner.root.exists())

    def test_close_failure_is_secondary_to_downstream_cancellation(self):
        failure = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt) as seen:
            with self.bound() as (owner, materials):
                patcher = mock.patch.object(owner._holds, 'close', side_effect=OSError())
                patcher.start()
                self.addCleanup(patcher.stop)
                with owner.archive_for(materials, source=self.source):
                    raise failure
        patcher.stop()
        self.assertIs(seen.exception, failure)
        self.assertIn('BOOTSTRAP_ARCHIVE_HANDLE_CLOSE_FAILED', failure.secondary_errors)


if __name__ == '__main__':
    unittest.main()
