import gc
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import weakref
from dataclasses import replace
from pathlib import Path
from unittest import mock

from bootstrap_kit import build, manifest, seed
from bootstrap_kit import safe_files as files
from bootstrap_kit.tests.fixtures import (
    NOW,
    metadata,
    selection,
    synthetic_kit,
    tool_inputs,
)


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.result, self.inputs, self.entry_sources = synthetic_kit(self.root)
        self.value = json.loads(self.result["paths"]["manifest"].read_bytes())

    def verify(self):
        return seed.verify_local(manifest_path=self.result["paths"]["manifest"],
            archive_path=self.result["paths"]["archive"], selection=selection(self.value),
            destination_parent=self.root, now=NOW)

    def repack(self, mutate):
        path = self.result["paths"]["archive"]
        with tarfile.open(path, "r:") as original:
            members = [(member, original.extractfile(member).read()) for member in original]
        members = mutate(members)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
            for member, data in members:
                archive.addfile(member, io.BytesIO(data))
        raw = stream.getvalue()
        path.write_bytes(raw)
        self.value["archive"] = {"sha256": manifest.digest(raw), "size": len(raw)}
        self.result["paths"]["manifest"].write_bytes(manifest.canonical(self.value))

    def test_complete_verification_precedes_fresh_isolated_dev_runtime(self):
        with self.verify() as kit:
            path = kit.root
            self.assertTrue((path / "library/last.py").exists())
            result = seed.run_local(kit, version="v2.0.0-rc.3", output=self.root/"result.json", timeout=10)
            self.assertEqual(result, {"state": "TEST_ONLY", "production_authority_granted": False})
            self.assertTrue(kit.last_runtime_cleanup["process_handle_closed"])
            self.assertTrue(kit.last_runtime_cleanup["pipes_closed"])
        self.assertFalse(path.exists())
        with self.assertRaises(ValueError):
            kit.require_open()

    def test_last_member_hash_failure_never_imports_or_leaves_verified_root(self):
        def corrupt(members):
            member, raw = members[-1]
            members[-1] = member, b'X' * len(raw)
            return members
        self.repack(corrupt)
        with mock.patch.object(seed.subprocess, "Popen") as run:
            with self.assertRaisesRegex(ValueError, "MEMBER_CHANGED"):
                self.verify()
            run.assert_not_called()
        self.assertFalse(list(self.root.glob("verified-kit-*")))

    @unittest.skipUnless(os.name == "nt", "Windows protective handles and GC retention")
    def test_unconfirmed_job_retains_holds_after_repeated_close_and_caller_gc(self):
        from bootstrap_kit.owned_process import ChildProcessError, OwnedProcess
        original_stop = OwnedProcess.stop_and_reap
        owners, calls = [], []
        def ambiguous_stop(owner, seconds=5):
            result = original_stop(owner, seconds=seconds)
            owners.append(owner)
            calls.append(result)
            # Actual child is reaped; inject only missing confirmation to test
            # retention without leaving a real uncontained test process.
            raise ChildProcessError()
        kit = self.verify()
        identity = kit._identity
        reference = weakref.ref(kit)
        try:
            with mock.patch.object(OwnedProcess, "stop_and_reap", new=ambiguous_stop), \
                    self.assertRaisesRegex(ValueError, "CLEANUP_UNCONFIRMED"):
                seed.run_local(kit, version="v2.0.0-rc.3", output=self.root/"unused", timeout=5)
            self.assertEqual(len(calls), 1)
            with mock.patch.object(kit._holds, "close", wraps=kit._holds.close) as close_holds, \
                    mock.patch.object(seed, "remove_owned_directory") as delete:
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "CLEANUP_UNCONFIRMED"):
                        kit.close()
                close_holds.assert_not_called()
                delete.assert_not_called()
            del kit
            gc.collect()
            retained = reference()
            self.assertIs(retained, seed._RETAINED_KITS[identity])
            self.assertIs(retained._retained_runtime[0], owners[0])
            self.assertEqual(len(retained._retained_runtime[1]), 2)
            self.assertIn(identity, [item["file_identity"] for item in seed.retained_kit_observations()])
            with self.assertRaises(PermissionError):
                (retained.root/"library/last.py").write_bytes(b"forbidden")
        finally:
            # Explicit fixture teardown only after the real process receipt
            # proves closure; production has no registry-release/retry API.
            retained = seed._RETAINED_KITS.pop(identity, reference())
            self.assertTrue(all(owner.closed and owner.receipt["tree_empty"] for owner in owners))
            retained._holds.close()
            files.remove_owned_directory(retained.root, retained._identity)

    @unittest.skipUnless(os.name == "nt", "Windows Popen process handle close")
    def test_native_process_handle_close_failure_is_recorded_and_retains_kit(self):
        from bootstrap_kit.owned_process import OwnedProcess
        original_init = OwnedProcess.__init__
        original_close = subprocess.Handle.Close
        owners, attempts = [], []
        def observe_init(owner, *args, **kwargs):
            original_init(owner, *args, **kwargs)
            owners.append(owner)
        def fail_process_handle(handle):
            if any(handle is owner.process._handle for owner in owners):
                attempts.append(handle)
                raise OSError("NONSECRET_CLOSE_FAILURE_DETAIL")
            return original_close(handle)
        kit = self.verify()
        try:
            with mock.patch.object(OwnedProcess, "__init__", new=observe_init), \
                    mock.patch.object(subprocess.Handle, "Close", new=fail_process_handle), \
                    self.assertRaisesRegex(ValueError, "PROCESS_HANDLE_CLOSE_FAILED") as caught:
                seed.run_local(kit, version="v2.0.0-rc.3", output=self.root/"unused", timeout=5)
            self.assertEqual(len(attempts), 1)
            self.assertNotIn("NONSECRET_CLOSE_FAILURE_DETAIL", str(caught.exception))
            self.assertTrue(kit.last_runtime_cleanup["pipes_closed"])
            self.assertFalse(kit.last_runtime_cleanup["process_handle_closed"])
            self.assertTrue(kit._cleanup_blocked)
            self.assertIs(kit._retained_runtime[0], owners[0])
            with self.assertRaisesRegex(ValueError, "CLEANUP_UNCONFIRMED"):
                kit.close()
            with self.assertRaises(PermissionError):
                (kit.root/"library/last.py").unlink()
        finally:
            # Undo only the injected close failure after real Job/pipe closure;
            # no failed filesystem delete is retried by this test.
            self.assertTrue(all(owner.closed and owner.receipt["tree_empty"] for owner in owners))
            for owner in owners:
                original_close(owner.process._handle)
            seed._RETAINED_KITS.pop(kit._identity, None)
            kit._holds.close()
            files.remove_owned_directory(kit.root, kit._identity)

    def test_reader_start_failure_still_reaps_process_and_closes_handle(self):
        with self.verify() as kit:
            with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("TEST_THREAD_START_FAILURE")), \
                    self.assertRaisesRegex(RuntimeError, "TEST_THREAD_START_FAILURE"):
                seed.run_local(kit, version="v2.0.0-rc.3", output=self.root/"unused", timeout=5)
            self.assertTrue(kit.last_runtime_cleanup["process_receipt"]["root_reaped"])
            self.assertTrue(kit.last_runtime_cleanup["process_receipt"]["tree_empty"])
            self.assertTrue(kit.last_runtime_cleanup["pipes_closed"])
            self.assertTrue(kit.last_runtime_cleanup["process_handle_closed"])
            self.assertIsNone(kit._retained_runtime)

    def test_unknown_missing_duplicate_link_and_traversal_tar_members_rejected(self):
        original = self.result["paths"]["archive"].read_bytes()
        base = json.loads(manifest.canonical(self.value))
        for mutation in ("missing", "extra", "duplicate", "link", "traversal", "case-collision"):
            self.result["paths"]["archive"].write_bytes(original)
            self.value = json.loads(manifest.canonical(base))
            def change(members, mutation=mutation):
                if mutation == "missing":
                    return members[:-1]
                if mutation == "duplicate":
                    return members + members[:1]
                member = tarfile.TarInfo("library/unknown.py" if mutation == "extra" else "../escape")
                if mutation == "link":
                    member.name, member.type, member.linkname = "library/link", tarfile.SYMTYPE, "../escape"
                if mutation == "case-collision":
                    member.name = members[0][0].name.upper()
                return members + [(member, b"")]
            self.repack(change)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.verify()
            self.assertFalse(list(self.root.glob("verified-kit-*")))

    def test_archive_hash_and_external_manifest_pin_are_not_self_authorizing(self):
        self.result["paths"]["archive"].write_bytes(b"untrusted")
        with self.assertRaisesRegex(ValueError, "ARCHIVE_PIN_MISMATCH"):
            self.verify()
        selected = replace(selection(self.value), expected_manifest_sha256="sha256:"+"0"*64)
        with self.assertRaisesRegex(ValueError, "PIN_MISMATCH"):
            seed.verify_local(manifest_path=self.result["paths"]["manifest"], archive_path=self.result["paths"]["archive"],
                selection=selected, destination_parent=self.root, now=NOW)

    def test_deterministic_build_has_stable_tar_entry_and_manifest_without_source_rewrite(self):
        other = self.root / "second"
        other.mkdir()
        result = build.build_kit(inputs=self.inputs, entry_sources=self.entry_sources,
            tool_inputs=tool_inputs(self.root), output_dir=other, metadata=metadata(), now=NOW)
        for key in ("manifest", "archive", "entry"):
            self.assertEqual(result["paths"][key].read_bytes(), self.result["paths"][key].read_bytes())

    def test_build_requires_exact_input_bytes_and_refuses_production(self):
        self.inputs[0].source.write_bytes(b"altered")
        target = self.root/"new"
        target.mkdir()
        with self.assertRaisesRegex(ValueError, "INPUT_CHANGED"):
            build.build_kit(inputs=self.inputs, entry_sources=self.entry_sources, output_dir=target,
                            tool_inputs=tool_inputs(self.root), metadata=metadata(), now=NOW)
        value = metadata()
        value["classification"] = "PRODUCTION"
        with self.assertRaisesRegex(ValueError, "DEVELOPMENT_ONLY"):
            build.build_kit(inputs=self.inputs, entry_sources=self.entry_sources, output_dir=target,
                            tool_inputs=tool_inputs(self.root), metadata=value, now=NOW)

    def test_build_verifies_actual_tool_bytes_and_computes_build_identity(self):
        tools = tool_inputs(self.root)
        tools[0].source.write_bytes(b"y")
        target = self.root / "tool-negative"
        target.mkdir()
        with self.assertRaisesRegex(ValueError, "TOOL_INPUT_CHANGED"):
            build.build_kit(inputs=self.inputs, entry_sources=self.entry_sources, tool_inputs=tools,
                            output_dir=target, metadata=metadata(), now=NOW)
        self.assertNotEqual(self.value["build"]["identity"], metadata()["build"]["identity"])

    def test_runtime_subject_is_bound_to_selection_without_product_rc_pinning(self):
        value = {"state": "TEST_ONLY", "production_authority_granted": False,
            "subject": "v1.2.3", "platform_release_signature": "VERIFIED", "actions_provenance": "VERIFIED",
            "tuf_chain": "VERIFIED", "large_asset_sha256": "a"*64,
            "product_execution": "NOT_RUN", "bootstrap_commit": "TEST_ONLY"}
        seed._runtime_report(value, expected_version="v1.2.3")
        with self.assertRaises(ValueError):
            seed._runtime_report(value, expected_version="v2.0.0-rc.3")

    def test_runtime_report_unknown_fields_and_production_claim_are_rejected(self):
        for value in ({"state": "TEST_ONLY", "production_authority_granted": False, "private_path": "private"},
                      {"state": "TEST_ONLY", "production_authority_granted": True}):
            with self.assertRaises(ValueError):
                seed._runtime_report(value)

    @unittest.skipUnless(os.name == "nt", "Windows deny-write/delete file holds")
    def test_verified_members_deny_write_and_delete_until_close(self):
        with self.verify() as kit:
            member = kit.root/"library/last.py"
            with self.assertRaises(PermissionError):
                member.write_bytes(b"changed")
            with self.assertRaises(PermissionError):
                member.unlink()

    def test_unissued_verified_object_and_prod_manifest_cannot_enter_dev_seed(self):
        with self.assertRaises(ValueError):
            seed.VerifiedKit(object(), root=self.root, selection=selection(self.value), value=self.value, holds=None)
        self.value["classification"] = "PRODUCTION"
        self.result["paths"]["manifest"].write_bytes(manifest.canonical(self.value))
        with self.assertRaises(ValueError):
            self.verify()

    def test_actual_authenticated_entry_pyz_runs_local_media_without_repo_or_cwd_import(self):
        entry_sources = []
        source_root = Path(__file__).resolve().parents[1]
        for name in ("__init__", "seed", "manifest", "safe_files", "http_supervisor",
                     "http_worker", "http_protocol", "owned_process", "egress"):
            path = source_root / (name+".py")
            entry_sources.append(build.BuildInput("bootstrap_kit/"+name+".py", path,
                manifest.digest(path.read_bytes()), "ENTRY"))
        output = self.root / "real-entry-build"
        output.mkdir()
        result = build.build_kit(inputs=self.inputs, entry_sources=entry_sources, output_dir=output,
                                 tool_inputs=tool_inputs(self.root), metadata=metadata(), now=NOW)
        value = json.loads(result["paths"]["manifest"].read_bytes())
        pin = self.root / "operator-selected-pin.json"
        pin.write_bytes(manifest.canonical(vars(selection(value))))
        # OS-side digest before execution is a separate read from the artifact;
        # this test supplies an explicit synthetic operator selection.
        self.assertEqual(manifest.digest(result["paths"]["entry"].read_bytes()), value["entry"]["sha256"])
        hostile = self.root / "hostile-cwd"
        hostile.mkdir()
        (hostile / "bootstrap_kit.py").write_text("raise RuntimeError('CWD MUST NOT IMPORT')\n")
        child = subprocess.run([sys.executable, "-I", "-S", "-B", str(result["paths"]["entry"]),
            "--manifest", str(result["paths"]["manifest"]), "--archive", str(result["paths"]["archive"]),
            "--selection", str(pin), "--destination-parent", str(self.root),
            "--version", "v2.0.0-rc.3", "--output", str(self.root/"local-result.json")],
            cwd=hostile, stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False)
        self.assertEqual(child.returncode, 0, child.stdout+child.stderr)
        self.assertEqual(json.loads(child.stdout)["state"], "TEST_ONLY")
        self.assertEqual(child.stderr, b"")

    def test_real_runtime_timeout_and_output_overflow_reap_job_before_kit_cleanup(self):
        for index, program in enumerate(("import time;time.sleep(30)\n",
                "import sys;sys.stdout.buffer.write(b'x'*1048576);sys.stdout.flush()\n")):
            root = self.root / ("negative-runtime-" + str(index))
            root.mkdir()
            result, _inputs, _entry = synthetic_kit(root, runtime=program)
            value = json.loads(result["paths"]["manifest"].read_bytes())
            with seed.verify_local(manifest_path=result["paths"]["manifest"], archive_path=result["paths"]["archive"],
                    selection=selection(value), destination_parent=root, now=NOW) as kit:
                started = time.monotonic()
                with self.assertRaises(ValueError):
                    seed.run_local(kit, version="v2.0.0-rc.3", output=root/"output.json", timeout=0.5)
                self.assertFalse(kit._cleanup_blocked)
                self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(kit.root.exists())

    def test_selected_public_branch_uses_real_workers_and_manifest_before_archive(self):
        from bootstrap_kit import http_supervisor as supervisor
        from bootstrap_kit.http_protocol import HttpFailure
        from bootstrap_kit.tests.test_http_supervisor import (
            loopback_program,
            test_command,
        )
        calls = []
        paths = self.result["paths"]
        bodies = {"/repos/yanyuhanyue/AniMemo/releases/assets/101": paths["manifest"].read_bytes(),
                  "/repos/yanyuhanyue/AniMemo/releases/assets/102": paths["archive"].read_bytes()}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                calls.append((self.path, dict(self.headers)))
                data = bodies[self.path]
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        listener = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=listener.serve_forever, name="animemo-seed-test-listener")
        server_thread.start()
        delivery = seed.DeliverySelection("yanyuhanyue/AniMemo", "1.0.0", "windows-amd64-cp312",
                                           100, 101, len(bodies[next(iter(bodies))]), 102)
        processes = []
        actual_fetch = supervisor.SupervisedAnonymousHttp.fetch
        def observed_fetch(client, *args, **kwargs):
            try:
                return actual_fetch(client, *args, **kwargs)
            finally:
                processes.append(client.last_process_receipt)
        try:
            # Only worker HTTPS socket construction is adapted to loopback;
            # seed, fixed asset selection, Job, framing, hash checks all run.
            with mock.patch.object(supervisor, "_command", test_command(loopback_program(listener.server_port))), \
                    mock.patch.object(supervisor.SupervisedAnonymousHttp, "fetch", new=observed_fetch):
                with seed.fetch_selected_kit(delivery=delivery, selection=selection(self.value),
                        destination_parent=self.root, now=NOW, deadline=time.monotonic()+10) as kit:
                    self.assertEqual(kit.manifest_bytes, paths["manifest"].read_bytes())
                self.assertEqual([path for path, _ in calls], list(bodies))
                self.assertTrue(all(receipt["root_reaped"] and receipt["tree_empty"] for receipt in processes))
                self.assertTrue(all(not {key.lower() for key in headers} & {"authorization", "cookie", "proxy-authorization"}
                                    for _, headers in calls))
                # Same-size changed manifest is rejected before any second GET.
                calls.clear()
                bodies[next(iter(bodies))] = b"X" * delivery.manifest_size
                with self.assertRaises((manifest.ManifestError, HttpFailure)):
                    seed.fetch_selected_kit(delivery=delivery, selection=selection(self.value),
                        destination_parent=self.root, now=NOW, deadline=time.monotonic()+10)
                self.assertEqual(len(calls), 1)
        finally:
            listener.shutdown()
            listener.server_close()
            server_thread.join(timeout=2)
            self.assertFalse(server_thread.is_alive())


class SafeFileTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows directory sharing semantics")
    def test_directory_hold_blocks_real_rename_and_keeps_same_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = files.create_private_directory(parent)
            identity = files.directory_identity(root)
            target = parent / "renamed-owned-directory"
            self.assertEqual(root.resolve().parent, parent)
            self.assertEqual(target.resolve(strict=False).parent, parent)
            with files.hold_path_chain(root), self.assertRaises(PermissionError):
                root.rename(target)
            self.assertEqual(files.directory_identity(root), identity)
            self.assertFalse(target.exists())
            files.remove_owned_directory(root, identity)

    def test_private_directory_exclusive_files_read_bounds_and_cleanup_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = files.create_private_directory(directory)
            identity = files.directory_identity(root)
            path = root/"data"
            with files.exclusive_file(path) as stream:
                stream.write(b"safe")
            with self.assertRaises(FileExistsError), files.exclusive_file(path):
                pass
            self.assertEqual(files.read_bounded(path, 4), b"safe")
            with self.assertRaises(ValueError):
                files.read_bounded(path, 3)
            with self.assertRaises(ValueError):
                files.remove_owned_directory(root, (0, 0))
            self.assertTrue(root.exists())
            self.assertTrue(files.remove_owned_directory(root, identity))
            self.assertFalse(root.exists())

    def test_hardlinked_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"one").write_bytes(b"content")
            os.link(root/"one", root/"two")
            with self.assertRaises(ValueError):
                files.read_bounded(root/"two", 100)


if __name__ == "__main__":
    unittest.main()
