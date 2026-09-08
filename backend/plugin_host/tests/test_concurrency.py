import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from integrations.authentication import sign_hmac_request
from integrations.models import ExternalIdentityBinding, IntegrationConnection, IntegrationEvent
from journal.models import JournalEntry, WatchHistoryRecord
from plugin_host.installer import PluginInstallError, PluginPackageInstaller
from plugin_host.models import PluginData, PluginDeployment, PluginPackageBlob, PluginProject, PluginVersion, UserPluginInstallation
from plugin_host.package import inspect_package
from plugin_host.runtime import RuntimeUnavailable, runtime_registry
from plugin_host.services import garbage_collect_package_blobs, install_for_user, store_package_blob
from plugin_host.storage import PluginStorage
from plugin_host.tests.test_runtime_e2e import make_package


@skipUnless(connection.vendor == "postgresql", "PostgreSQL concurrency proof requires PostgreSQL")
class PluginPlatformPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.settings = override_settings(PLUGIN_ROOT=Path(self.root.name), PLUGIN_MIN_FREE_DISK_MB=0)
        self.settings.enable()
        self.admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "password-123")
        self.user = get_user_model().objects.create_user("user", "user@example.com", "password-123")

    def tearDown(self):
        runtime_registry.clear()
        self.settings.disable()
        self.root.cleanup()

    @staticmethod
    def _parallel(callables):
        def run(callback):
            try:
                return callback()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(callables)) as pool:
            futures = [pool.submit(run, callback) for callback in callables]
            return [future.result() for future in futures]

    def _version(self, project, version):
        payload, manifest = make_package(project.slug, version, runtimes=["frontend"])
        blob, _, _, _ = store_package_blob(payload)
        return PluginVersion.objects.create(
            plugin=project, version=version, package_blob=blob, manifest_snapshot=manifest,
            runtime_types=["frontend"], review_status=PluginVersion.ReviewStatus.APPROVED,
            created_by=self.admin,
        )

    def _official_importer(self):
        call_command("sync_official_plugins", verbosity=0)
        project = PluginProject.objects.get(slug="watch-history-importer")
        install_for_user(project, user=self.user)
        runtime_registry.ensure_current(project.slug)
        return project

    def _stale_dispatch_result_after(self, project, lifecycle_action):
        started = Event()
        proceed = Event()
        stale_version = PluginDeployment.objects.select_related("current_version").get(plugin=project).current_version.version

        def dispatch():
            close_old_connections()
            try:
                started.set()
                proceed.wait(timeout=10)
                try:
                    return stale_version, runtime_registry.ensure_current(project.slug).version
                except RuntimeUnavailable:
                    return stale_version, None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(dispatch)
            self.assertTrue(started.wait(timeout=10))
            lifecycle_action()
            proceed.set()
            return future.result(timeout=10)

    def test_same_package_concurrent_upload_creates_one_blob(self):
        payload, _ = make_package("cas-race", "1.0.0", runtimes=["frontend"])
        barrier = Barrier(2)

        def upload():
            close_old_connections()
            barrier.wait()
            result = store_package_blob(payload)[0].sha256
            close_old_connections()
            return result

        hashes = self._parallel([upload, upload])
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(len(list((Path(self.root.name) / "packages" / "sha256").rglob("*.ajplugin"))), 1)

    def test_same_user_concurrent_install_creates_one_row(self):
        project = PluginProject.objects.create(plugin_id="com.example.install-race", slug="install-race", name="race", description="test")
        version = self._version(project, "1.0.0")
        version.published_at = timezone.now()
        version.save(update_fields=["published_at"])
        PluginDeployment.objects.create(plugin=project, current_version=version, enabled=True, healthy=True)
        barrier = Barrier(2)

        def install():
            close_old_connections()
            barrier.wait()
            result = install_for_user(PluginProject.objects.get(pk=project.pk), user=get_user_model().objects.get(pk=self.user.pk))[0].pk
            close_old_connections()
            return result

        ids = self._parallel([install, install])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(UserPluginInstallation.objects.filter(user=self.user, plugin=project).count(), 1)

    def test_concurrent_import_previews_cannot_exceed_batch_row_limit(self):
        project = self._official_importer()
        barrier = Barrier(2)
        original = PluginStorage.set_bounded

        def synchronized_set(storage, *args, **kwargs):
            if storage.namespace == "batches":
                barrier.wait(timeout=10)
            return original(storage, *args, **kwargs)

        def preview(suffix):
            close_old_connections()
            client = APIClient()
            client.force_authenticate(get_user_model().objects.get(pk=self.user.pk))
            response = client.post(
                "/api/plugins/watch-history-importer/preview/",
                {
                    "files": [
                        SimpleUploadedFile(
                            f"2026-{suffix}.txt",
                            f"1月1日 首刷 并发动画{suffix} 第1集".encode(),
                        )
                    ]
                },
                format="multipart",
            )
            return response.status_code

        with override_settings(
            WATCH_HISTORY_IMPORT_BATCH_MAX_PER_USER=1,
            WATCH_HISTORY_IMPORT_BATCH_MAX_BYTES=1024 * 1024,
            WATCH_HISTORY_IMPORT_TOTAL_UPLOAD_MAX_BYTES=1024 * 1024,
        ), patch.object(PluginStorage, "set_bounded", new=synchronized_set):
            statuses = self._parallel([
                lambda: preview("one"),
                lambda: preview("two"),
            ])

        self.assertEqual(statuses, [201, 201])
        self.assertEqual(
            PluginData.objects.filter(
                plugin=project,
                user=self.user,
                namespace="batches",
            ).count(),
            1,
        )

    def test_concurrent_import_commit_executes_batch_once(self):
        project = self._official_importer()
        connection_row = IntegrationConnection(
            provider="generic",
            instance_id="import-commit-race",
            name="Import Commit Race",
            key_id="import-commit-race-key",
        )
        connection_row.set_secret("import-commit-race-secret")
        connection_row.save()
        ExternalIdentityBinding.objects.create(
            connection=connection_row,
            user=self.user,
            platform="qq",
            external_user_id="42",
            verified_at=timezone.now(),
        )
        batch_id = uuid4().hex
        PluginData.objects.create(
            plugin=project,
            namespace="batches",
            user=self.user,
            key=batch_id,
            value={
                "id": batch_id,
                "status": "ready",
                "summary": {},
                "target_user_id": self.user.pk,
                "payload": {
                    "groups": [{
                        "source_title": "并发提交动画",
                        "resolution": {
                            "status": "matched",
                            "bangumi_id": 991,
                            "title": "并发提交动画",
                            "japanese_title": "",
                            "tags": [],
                        },
                        "records": [{
                            "watch_date": "2026-08-11",
                            "watch_date_label": "",
                            "brush": 1,
                            "brush_label": "首刷",
                            "episode_range": {"start": 1, "end": 1},
                            "notes": [],
                        }],
                    }],
                },
            },
        )
        barrier = Barrier(2)
        original_collection = PluginStorage.collection

        def synchronized_collection(storage):
            if storage.namespace == "batches":
                barrier.wait(timeout=10)
            return original_collection(storage)

        def commit(request_id):
            close_old_connections()
            path = "/api/integrations/v1/actions/"
            body = {
                "request_id": request_id,
                "platform": "qq",
                "external_user_id": "42",
                "action": "watch-history-importer.import-commit",
                "payload": {"batch_id": batch_id},
            }
            raw = json.dumps(body, separators=(",", ":")).encode()
            timestamp = str(int(time.time()))
            nonce = uuid4().hex
            response = APIClient().generic(
                "POST",
                path,
                data=raw,
                content_type="application/json",
                HTTP_X_ANIMEMO_KEY_ID=connection_row.key_id,
                HTTP_X_ANIMEMO_TIMESTAMP=timestamp,
                HTTP_X_ANIMEMO_NONCE=nonce,
                HTTP_X_ANIMEMO_SIGNATURE=sign_hmac_request(
                    connection_row.get_secret(),
                    timestamp,
                    nonce,
                    "POST",
                    path,
                    raw,
                ),
            )
            return response.status_code, response.data

        with patch.object(PluginStorage, "collection", new=synchronized_collection):
            responses = self._parallel([
                lambda: commit("import-commit-race-one"),
                lambda: commit("import-commit-race-two"),
            ])

        self.assertEqual([status for status, _data in responses], [200, 200])
        self.assertEqual(responses[0][1], responses[1][1])
        entry = JournalEntry.objects.get(user=self.user, title="并发提交动画")
        self.assertEqual(WatchHistoryRecord.objects.filter(entry=entry).count(), 1)
        self.assertEqual(
            IntegrationEvent.objects.filter(
                plugin_slug="watch-history-importer",
                event_name="import-completed",
            ).count(),
            1,
        )

    def test_gc_vs_same_sha_upload_never_leaves_database_row_without_file(self):
        payload, _ = make_package("gc-upload-race", "1.0.0", runtimes=["frontend"])
        blob, _, _, _ = store_package_blob(payload)
        PluginPackageBlob.objects.filter(pk=blob.pk).update(created_at=timezone.now() - timedelta(days=2))
        barrier = Barrier(2)

        def upload():
            close_old_connections(); barrier.wait()
            try:
                return store_package_blob(payload)[0].sha256
            finally:
                close_old_connections()

        def collect():
            close_old_connections(); barrier.wait()
            try:
                return garbage_collect_package_blobs(root=self.root.name)
            finally:
                close_old_connections()

        self._parallel([upload, collect])
        row = PluginPackageBlob.objects.filter(sha256=blob.sha256).first()
        path = Path(self.root.name) / "packages" / "sha256" / blob.sha256[:2] / f"{blob.sha256}.ajplugin"
        if row is None:
            self.assertFalse(path.exists())
        else:
            self.assertTrue(path.is_file())
            self.assertEqual(inspect_package(path.read_bytes())["sha256"], row.sha256)

    def test_publish_vs_revoke_never_leaves_revoked_runtime_enabled(self):
        project = PluginProject.objects.create(plugin_id="com.example.publish-revoke", slug="publish-revoke", name="race", description="test")
        version = self._version(project, "1.0.0")
        barrier = Barrier(2)

        def publish():
            close_old_connections(); barrier.wait()
            try:
                PluginPackageInstaller().publish(PluginVersion.objects.get(pk=version.pk), actor=get_user_model().objects.get(pk=self.admin.pk))
            except PluginInstallError:
                pass
            finally:
                close_old_connections()

        def revoke():
            close_old_connections(); barrier.wait()
            PluginPackageInstaller().revoke(PluginVersion.objects.get(pk=version.pk), actor=get_user_model().objects.get(pk=self.admin.pk))
            close_old_connections()

        self._parallel([publish, revoke])
        version.refresh_from_db()
        deployment = PluginDeployment.objects.filter(plugin=project).first()
        self.assertIsNotNone(version.revoked_at)
        self.assertTrue(deployment is None or not deployment.enabled)
        self.assertNotEqual(runtime_registry.active_version(project.slug), version.version)

    def test_publish_vs_rollback_keeps_a_known_version(self):
        project = PluginProject.objects.create(plugin_id="com.example.publish-rollback", slug="publish-rollback", name="race", description="test")
        first = self._version(project, "1.0.0")
        second = self._version(project, "1.1.0")
        third = self._version(project, "1.2.0")
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        installer.publish(second, actor=self.admin)
        barrier = Barrier(2)

        def publish():
            close_old_connections(); barrier.wait()
            try:
                PluginPackageInstaller().publish(PluginVersion.objects.get(pk=third.pk), actor=get_user_model().objects.get(pk=self.admin.pk))
            except PluginInstallError:
                pass
            finally:
                close_old_connections()

        def rollback():
            close_old_connections(); barrier.wait()
            try:
                PluginPackageInstaller().rollback(project.slug, actor=get_user_model().objects.get(pk=self.admin.pk))
            except PluginInstallError:
                pass
            finally:
                close_old_connections()

        self._parallel([publish, rollback])
        deployment = PluginDeployment.objects.select_related("current_version").get(plugin=project)
        self.assertTrue((Path(self.root.name) / "runtime" / project.slug / deployment.current_version.version).is_dir())
        self.assertEqual(runtime_registry.ensure_current(project.slug).version, deployment.current_version.version)
        self.assertTrue(all(
            registration.plugin_version == deployment.current_version.version
            for registration in runtime_registry.hooks.registrations_for(project.slug)
        ))

    def test_dispatch_vs_upgrade_uses_database_current_version(self):
        project = PluginProject.objects.create(plugin_id="com.example.dispatch-upgrade", slug="dispatch-upgrade", name="race", description="test")
        first = self._version(project, "1.0.0")
        second = self._version(project, "1.1.0")
        PluginPackageInstaller().publish(first, actor=self.admin)

        stale, active = self._stale_dispatch_result_after(
            project,
            lambda: PluginPackageInstaller().publish(second, actor=self.admin),
        )

        self.assertEqual(stale, "1.0.0")
        self.assertEqual(active, "1.1.0")

    def test_dispatch_vs_rollback_uses_database_current_version(self):
        project = PluginProject.objects.create(plugin_id="com.example.dispatch-rollback", slug="dispatch-rollback", name="race", description="test")
        first = self._version(project, "1.0.0")
        second = self._version(project, "1.1.0")
        installer = PluginPackageInstaller()
        installer.publish(first, actor=self.admin)
        installer.publish(second, actor=self.admin)

        stale, active = self._stale_dispatch_result_after(
            project,
            lambda: PluginPackageInstaller().rollback(project.slug, actor=self.admin),
        )

        self.assertEqual(stale, "1.1.0")
        self.assertEqual(active, "1.0.0")

    def test_dispatch_vs_disable_cannot_resurrect_runtime(self):
        project = PluginProject.objects.create(plugin_id="com.example.dispatch-disable", slug="dispatch-disable", name="race", description="test")
        first = self._version(project, "1.0.0")
        PluginPackageInstaller().publish(first, actor=self.admin)

        stale, active = self._stale_dispatch_result_after(
            project,
            lambda: PluginPackageInstaller().set_enabled(project.slug, False, actor=self.admin),
        )

        self.assertEqual(stale, "1.0.0")
        self.assertIsNone(active)
        self.assertIsNone(runtime_registry.active_version(project.slug))

    @staticmethod
    def _cas_trace():
        from threading import local

        return {"events": [], "postgresql": {}, "worker": local()}

    def _cas_worker(self, trace, role, action):
        trace["worker"].role = role
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '15s'")
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SELECT pg_backend_pid(), current_database(), inet_server_port()")
                pid, database, port = cursor.fetchone()
            trace["postgresql"][role] = {"pid": pid, "database": database, "port": port}
            return action()
        finally:
            connections.close_all()

    def _cas_path(self, digest):
        return Path(self.root.name) / "packages" / "sha256" / digest[:2] / f"{digest}.ajplugin"

    def _cas_snapshot(self, digest):
        import hashlib

        path = self._cas_path(digest)
        raw = path.read_bytes() if path.is_file() else None
        return {
            "rows": list(PluginPackageBlob.objects.filter(sha256=digest).values(
                "pk", "sha256", "size_bytes", "storage_path", "created_at",
            )),
            "versions": list(PluginVersion.objects.filter(package_blob__sha256=digest).values(
                "pk", "plugin_id", "version", "package_blob_id",
            )),
            "file_exists": raw is not None,
            "file_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "file_size": len(raw) if raw is not None else None,
            "cas_lock_exists": (Path(self.root.name) / ".locks" / f"cas-{digest}.lock").exists(),
            "tombstones": sorted(
                str(path.relative_to(self.root.name))
                for path in Path(self.root.name).rglob(f"{digest}.*.tombstone")
            ),
        }

    def _cas_emit(self, case, trace, digest):
        print("CAS_COMMIT_REGRESSION " + json.dumps({
            "case": case,
            "role": "AUTHOR_REGRESSION_TEST",
            "boundary": trace.get("boundary", "REAL_POSTGRESQL_AND_CAS"),
            "postgresql": trace["postgresql"],
            "events": trace["events"],
            "final": self._cas_snapshot(digest),
        }, sort_keys=True, default=str))

    def _cas_seed(self, slug):
        payload, manifest = make_package(slug, "1.0.0", runtimes=["frontend"])
        blob, _, _, _ = store_package_blob(payload)
        PluginPackageBlob.objects.filter(pk=blob.pk).update(created_at=timezone.now() - timedelta(days=2))
        return payload, manifest, blob

    def _cas_project(self, manifest):
        return PluginProject.objects.create(
            plugin_id=manifest["id"], slug=manifest["slug"], name=manifest["name"],
            description=manifest["description"], installation_mode=manifest["installationMode"],
            owner=self.admin,
        )

    def _cas_assert_blob(self, digest, payload, *, blob_pk=None, version_count=0):
        state = self._cas_snapshot(digest)
        self.assertEqual(len(state["rows"]), 1, state)
        row = state["rows"][0]
        if blob_pk is not None:
            self.assertEqual(row["pk"], blob_pk, state)
        self.assertEqual(row["sha256"], digest, state)
        self.assertEqual(row["size_bytes"], len(payload), state)
        self.assertEqual(row["storage_path"], self._cas_path(digest).relative_to(self.root.name).as_posix(), state)
        self.assertEqual(state["file_sha256"], digest, state)
        self.assertEqual(state["file_size"], len(payload), state)
        self.assertEqual(self._cas_path(digest).read_bytes(), payload)
        self.assertEqual(len(state["versions"]), version_count, state)
        self.assertFalse(state["cas_lock_exists"], state)
        self.assertEqual(state["tombstones"], [], state)
        return state

    def _cas_hold_until_released(self, trace, role, stage, ready, release, digest):
        from django.db import transaction

        self.assertTrue(connection.in_atomic_block)
        self.assertFalse(connection.get_autocommit())
        self.assertTrue((Path(self.root.name) / ".locks" / f"cas-{digest}.lock").is_file())
        trace["events"].append(f"{role}_{stage}")
        transaction.on_commit(lambda: trace["events"].append(f"{role}_committed"))
        ready.set()
        self.assertTrue(release.wait(timeout=15), f"{role} was not released")

    def _cas_pause_version_save(self, trace, role, ready, release, digest):
        original_save = PluginVersion.save

        def save(version, *args, **kwargs):
            result = original_save(version, *args, **kwargs)
            if (
                getattr(trace["worker"], "role", None) == role
                and not ready.is_set()
                and version.package_blob.sha256 == digest
            ):
                trace["version_pk"] = version.pk
                self._cas_hold_until_released(trace, role, "version_written", ready, release, digest)
            return result

        return save

    def _cas_race(
        self, case, trace, digest, *, holder_role, holder, waiter_role, waiter,
        ready, release, acquired=None, before_release=None,
    ):
        import os
        from plugin_host import package

        busy = Event()
        acquired = acquired if acquired is not None else Event()
        original_open = package.os.open
        lock_path = Path(self.root.name) / ".locks" / f"cas-{digest}.lock"

        def observe_open(path, flags, *args, **kwargs):
            observed = (
                getattr(trace["worker"], "role", None) == waiter_role
                and Path(path) == lock_path and flags & os.O_EXCL
            )
            try:
                fd = original_open(path, flags, *args, **kwargs)
            except FileExistsError:
                if observed and not busy.is_set():
                    trace["events"].append(f"{waiter_role}_cas_wait_eexist")
                    busy.set()
                # The real PackageHashLock must handle and retry the real error.
                raise
            if observed and busy.is_set() and not acquired.is_set():
                trace["events"].append(f"{waiter_role}_cas_acquired")
                acquired.set()
            return fd

        failure = None
        results = {}
        try:
            with patch.object(package.os, "open", new=observe_open), ThreadPoolExecutor(max_workers=2) as pool:
                futures = {holder_role: pool.submit(self._cas_worker, trace, holder_role, holder)}
                try:
                    self.assertTrue(ready.wait(timeout=5), f"{holder_role} did not reach its pause")
                    futures[waiter_role] = pool.submit(self._cas_worker, trace, waiter_role, waiter)
                    self.assertTrue(busy.wait(timeout=5), f"{waiter_role} did not contend on the real CAS lock")
                    self.assertFalse(acquired.is_set())
                    self.assertFalse(futures[waiter_role].done())
                    if before_release is not None:
                        before_release()
                except BaseException as error:
                    failure = error
                finally:
                    release.set()
                # Drain every submitted future even if a controller assertion failed.
                for role, future in futures.items():
                    try:
                        results[role] = future.result(timeout=20)
                    except BaseException as error:
                        if failure is None:
                            failure = error
            if failure is not None:
                raise failure
            self.assertTrue(acquired.is_set())
            self.assertNotEqual(trace["postgresql"][holder_role]["pid"], trace["postgresql"][waiter_role]["pid"])
            self.assertLess(
                trace["events"].index(f"{holder_role}_committed"),
                trace["events"].index(f"{waiter_role}_cas_acquired"),
            )
            return results
        finally:
            release.set()
            self._cas_emit(case, trace, digest)

    def test_gc_waits_for_verified_standalone_upload_commit(self):
        from plugin_host import services

        payload, _, blob = self._cas_seed("cas-verified-upload")
        trace, ready, release = self._cas_trace(), Event(), Event()
        original_secure_file = services.secure_file

        def pause_verified_file(root, path, *args, **kwargs):
            result = original_secure_file(root, path, *args, **kwargs)
            if (
                getattr(trace["worker"], "role", None) == "upload"
                and Path(path) == self._cas_path(blob.sha256) and not ready.is_set()
            ):
                self._cas_hold_until_released(trace, "upload", "file_verified", ready, release, blob.sha256)
            return result

        with patch.object(services, "secure_file", new=pause_verified_file):
            results = self._cas_race(
                "verified_standalone_upload_then_gc", trace, blob.sha256,
                holder_role="upload", holder=lambda: store_package_blob(payload)[0].sha256,
                waiter_role="gc", waiter=lambda: garbage_collect_package_blobs(root=self.root.name),
                ready=ready, release=release,
            )
        self.assertEqual(results["upload"], blob.sha256)
        self.assertIn(blob.sha256, results["gc"]["package_blobs_removed"])
        # The old blob has no protecting version and remains eligible after upload commits.
        state = self._cas_snapshot(blob.sha256)
        self.assertEqual(state["rows"], [], state)
        self.assertFalse(state["file_exists"], state)
        self.assertFalse(state["cas_lock_exists"], state)
        self.assertEqual(state["tombstones"], [], state)

    def test_upload_waits_for_gc_quarantine_commit_then_rebuilds_blob(self):
        from plugin_host import services

        payload, _, blob = self._cas_seed("cas-quarantine-upload")
        trace, ready, release = self._cas_trace(), Event(), Event()
        original_replace = services.os.replace

        def pause_quarantine(source, destination, *args, **kwargs):
            result = original_replace(source, destination, *args, **kwargs)
            if (
                getattr(trace["worker"], "role", None) == "gc"
                and Path(source) == self._cas_path(blob.sha256)
                and Path(destination).suffix == ".tombstone" and not ready.is_set()
            ):
                self._cas_hold_until_released(trace, "gc", "quarantined", ready, release, blob.sha256)
            return result

        def while_quarantined():
            self.assertFalse(self._cas_path(blob.sha256).exists())
            self.assertTrue(PluginPackageBlob.objects.filter(pk=blob.pk).exists())
            trace["events"].append("controller_old_row_visible_while_quarantined")

        with patch.object(services.os, "replace", new=pause_quarantine):
            results = self._cas_race(
                "gc_quarantine_then_upload", trace, blob.sha256,
                holder_role="gc", holder=lambda: garbage_collect_package_blobs(root=self.root.name),
                waiter_role="upload", waiter=lambda: store_package_blob(payload)[0].pk,
                ready=ready, release=release, before_release=while_quarantined,
            )
        self.assertIn(blob.sha256, results["gc"]["package_blobs_removed"])
        self.assertNotEqual(results["upload"], blob.pk)
        self._cas_assert_blob(blob.sha256, payload, blob_pk=results["upload"])

    def test_gc_waits_for_uploaded_version_outer_commit(self):
        from plugin_host.services import upload_plugin_version

        payload, manifest, blob = self._cas_seed("cas-version-commit")
        project = self._cas_project(manifest)
        trace, ready, release = self._cas_trace(), Event(), Event()

        def upload():
            return upload_plugin_version(
                project, SimpleUploadedFile("version.ajplugin", payload), actor=self.admin,
            )[0].pk

        def before_commit():
            self.assertFalse(PluginVersion.objects.filter(pk=trace["version_pk"]).exists())
            trace["events"].append("controller_uncommitted_version_invisible")

        with patch.object(PluginVersion, "save", new=self._cas_pause_version_save(
            trace, "upload", ready, release, blob.sha256,
        )):
            results = self._cas_race(
                "uploaded_version_outer_commit", trace, blob.sha256,
                holder_role="upload", holder=upload,
                waiter_role="gc", waiter=lambda: garbage_collect_package_blobs(root=self.root.name),
                ready=ready, release=release, before_release=before_commit,
            )
        self.assertNotIn(blob.sha256, results["gc"]["package_blobs_removed"])
        state = self._cas_assert_blob(blob.sha256, payload, blob_pk=blob.pk, version_count=1)
        self.assertEqual(state["versions"][0]["pk"], results["upload"])
        self.assertEqual(state["versions"][0]["package_blob_id"], blob.pk)

    def test_uploaded_version_failure_rolls_back_and_preserves_existing_blob(self):
        from django.db import transaction
        from plugin_host.services import upload_plugin_version

        payload, manifest, blob = self._cas_seed("cas-version-rollback")
        project = self._cas_project(manifest)
        before = self._cas_snapshot(blob.sha256)
        trace = self._cas_trace()
        original_save = PluginVersion.save

        def fail_after_version_insert(version, *args, **kwargs):
            result = original_save(version, *args, **kwargs)
            if version.package_blob_id == blob.pk:
                self.assertTrue(connection.in_atomic_block)
                self.assertTrue(PluginVersion.objects.filter(pk=version.pk).exists())
                trace["events"].append("upload_version_inserted_before_failure")
                transaction.on_commit(lambda: trace["events"].append("upload_committed"))
                raise RuntimeError("injected_version_insert_failure")
            return result

        try:
            with patch.object(PluginVersion, "save", new=fail_after_version_insert):
                with self.assertRaisesRegex(RuntimeError, "injected_version_insert_failure"):
                    self._cas_worker(trace, "upload", lambda: upload_plugin_version(
                        project, SimpleUploadedFile("version.ajplugin", payload), actor=self.admin,
                    ))
            self.assertIn("upload_version_inserted_before_failure", trace["events"])
            self.assertNotIn("upload_committed", trace["events"])
            self.assertFalse(PluginVersion.objects.filter(plugin=project).exists())
            self.assertEqual(self._cas_snapshot(blob.sha256), before)
            self._cas_assert_blob(blob.sha256, payload, blob_pk=blob.pk)
        finally:
            self._cas_emit("uploaded_version_rollback", trace, blob.sha256)

    def test_gc_failure_after_row_delete_restores_row_and_canonical_file(self):
        from django.db import transaction

        payload, _, blob = self._cas_seed("cas-gc-rollback")
        before = self._cas_snapshot(blob.sha256)
        trace = self._cas_trace()
        original_delete = PluginPackageBlob.delete

        def fail_after_delete(row, *args, **kwargs):
            digest = row.sha256
            result = original_delete(row, *args, **kwargs)
            if digest == blob.sha256:
                self.assertTrue(connection.in_atomic_block)
                self.assertFalse(PluginPackageBlob.objects.filter(sha256=digest).exists())
                self.assertFalse(self._cas_path(digest).exists())
                trace["events"].append("gc_row_deleted_before_failure")
                transaction.on_commit(lambda: trace["events"].append("gc_committed"))
                raise RuntimeError("injected_gc_delete_failure")
            return result

        try:
            with patch.object(PluginPackageBlob, "delete", new=fail_after_delete):
                with self.assertRaisesRegex(RuntimeError, "injected_gc_delete_failure"):
                    self._cas_worker(trace, "gc", lambda: garbage_collect_package_blobs(root=self.root.name))
            self.assertIn("gc_row_deleted_before_failure", trace["events"])
            self.assertNotIn("gc_committed", trace["events"])
            self.assertEqual(self._cas_snapshot(blob.sha256), before)
            self._cas_assert_blob(blob.sha256, payload, blob_pk=blob.pk)
        finally:
            self._cas_emit("gc_delete_rollback", trace, blob.sha256)

    def _cas_all_bytes_and_rows(self):
        import hashlib

        return {
            "rows": list(PluginPackageBlob.objects.order_by("pk").values()),
            "files": {
                path.relative_to(self.root.name).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(Path(self.root.name).rglob("*")) if path.is_file()
            },
        }

    def test_nested_package_operations_reject_before_changing_rows_or_bytes(self):
        from django.db import transaction
        from plugin_host.services import PluginWorkflowError

        _, _, blob = self._cas_seed("cas-nested-existing")
        payload, _ = make_package("cas-nested-new", "1.0.0", runtimes=["frontend"])
        new_digest = inspect_package(payload)["sha256"]
        before = self._cas_all_bytes_and_rows()
        trace = self._cas_trace()

        def nested_operations():
            with transaction.atomic():
                with self.assertRaisesRegex(PluginWorkflowError, "提交边界"):
                    store_package_blob(payload)
                trace["events"].append("nested_upload_rejected")
                self.assertEqual(self._cas_all_bytes_and_rows(), before)
                with self.assertRaisesRegex(PluginWorkflowError, "提交边界"):
                    garbage_collect_package_blobs(root=self.root.name)
                trace["events"].append("nested_gc_rejected")
                self.assertEqual(self._cas_all_bytes_and_rows(), before)

        try:
            self._cas_worker(trace, "nested", nested_operations)
            self.assertEqual(self._cas_all_bytes_and_rows(), before)
            self.assertFalse(PluginPackageBlob.objects.filter(sha256=new_digest).exists())
            self.assertFalse(self._cas_path(new_digest).exists())
        finally:
            self._cas_emit("nested_boundary_rejection", trace, blob.sha256)

    def test_manual_autocommit_off_rejects_package_operations_without_changes(self):
        from plugin_host.services import PluginWorkflowError

        _, _, blob = self._cas_seed("cas-manual-existing")
        payload, _ = make_package("cas-manual-new", "1.0.0", runtimes=["frontend"])
        new_digest = inspect_package(payload)["sha256"]
        before = self._cas_all_bytes_and_rows()
        trace = self._cas_trace()

        def manual_operations():
            connection.set_autocommit(False)
            try:
                self.assertFalse(connection.in_atomic_block)
                with self.assertRaisesRegex(PluginWorkflowError, "提交边界"):
                    store_package_blob(payload)
                trace["events"].append("manual_upload_rejected")
                self.assertEqual(self._cas_all_bytes_and_rows(), before)
                with self.assertRaisesRegex(PluginWorkflowError, "提交边界"):
                    garbage_collect_package_blobs(root=self.root.name)
                trace["events"].append("manual_gc_rejected")
                self.assertEqual(self._cas_all_bytes_and_rows(), before)
            finally:
                connection.rollback()
                connection.set_autocommit(True)

        try:
            self._cas_worker(trace, "manual", manual_operations)
            self.assertEqual(self._cas_all_bytes_and_rows(), before)
            self.assertFalse(PluginPackageBlob.objects.filter(sha256=new_digest).exists())
            self.assertFalse(self._cas_path(new_digest).exists())
        finally:
            self._cas_emit("manual_boundary_rejection", trace, blob.sha256)

    def test_official_sync_holds_cas_through_version_commit_and_releases_before_publish(self):
        from io import StringIO
        from django.conf import settings
        from plugin_host.management.commands import sync_official_plugins
        from plugin_host.official_packages import build_official_package

        slug = "watch-history-importer"
        payload = build_official_package(Path(settings.BASE_DIR).parent / "plugins" / slug)
        blob, _, _, _ = store_package_blob(payload)
        PluginPackageBlob.objects.filter(pk=blob.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.assertFalse(PluginProject.objects.filter(slug=slug).exists())
        trace, ready, release, acquired = self._cas_trace(), Event(), Event(), Event()
        trace["boundary"] = "REAL_REGISTRATION_AND_GC_WITH_MOCKED_PUBLISH_BOUNDARY"
        output = StringIO()

        def before_commit():
            self.assertFalse(PluginVersion.objects.filter(pk=trace["version_pk"]).exists())
            self.assertFalse(PluginProject.objects.filter(slug=slug).exists())
            trace["events"].append("controller_uncommitted_official_registration_invisible")

        def publish_boundary(installer, version, *, actor):
            # This case proves registration/commit/lock order, not installer publication.
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(connection.get_autocommit())
            self.assertIn("official_sync_committed", trace["events"])
            self.assertTrue(acquired.wait(timeout=5), "GC could not acquire CAS before publication")
            self.assertEqual(version.package_blob_id, blob.pk)
            self.assertEqual(actor.pk, self.admin.pk)
            trace["events"].append("publish_boundary_outside_transaction_and_cas")

        with patch.object(sync_official_plugins, "OFFICIAL_PLUGIN_SLUGS", (slug,)), patch.object(
            PluginVersion, "save", new=self._cas_pause_version_save(
                trace, "official_sync", ready, release, blob.sha256,
            ),
        ), patch.object(PluginPackageInstaller, "publish", new=publish_boundary):
            results = self._cas_race(
                "official_registration_outer_commit", trace, blob.sha256,
                holder_role="official_sync",
                holder=lambda: call_command("sync_official_plugins", stdout=output, verbosity=0),
                waiter_role="gc", waiter=lambda: garbage_collect_package_blobs(root=self.root.name),
                ready=ready, release=release, acquired=acquired, before_release=before_commit,
            )
        self.assertNotIn(blob.sha256, results["gc"]["package_blobs_removed"])
        self.assertIn("publish_boundary_outside_transaction_and_cas", trace["events"])
        self._cas_assert_blob(blob.sha256, payload, blob_pk=blob.pk, version_count=1)
        version = PluginVersion.objects.get(plugin__slug=slug)
        self.assertEqual(version.pk, trace["version_pk"])
        self.assertEqual(version.review_status, PluginVersion.ReviewStatus.APPROVED)
