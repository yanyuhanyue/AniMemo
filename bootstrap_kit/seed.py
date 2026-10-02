"""OS-authenticated stdlib seed: pin, package, every member, then kit code."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bootstrap_kit import manifest
from bootstrap_kit.safe_files import (
    KitFileError,
    create_private_directory,
    directory_identity,
    exclusive_file,
    file_digest,
    held_file,
    read_bounded,
    remove_owned_directory,
    safe_relative,
)

_ISSUER = object()
_RETAINED_KITS = {}


class VerifiedKit:
    """Local import permission while every verified file remains held."""
    def __init__(self, issuer, *, root, selection, value, holds):
        manifest.require(issuer is _ISSUER, "BOOTSTRAP_KIT_VERIFIED_OBJECT_REQUIRED")
        self.root, self.selection = root, selection
        self.manifest_bytes = manifest.canonical(value)
        self._holds, self._closed = holds, False
        self._cleanup_blocked = False
        self._retained_runtime = None
        self._identity = directory_identity(root)

    @property
    def manifest(self):
        return json.loads(self.manifest_bytes)

    def require_open(self):
        manifest.require(not self._closed, "BOOTSTRAP_KIT_CLOSED")
        manifest.require(directory_identity(self.root) == self._identity)

    def _retain_unconfirmed(self):
        self._cleanup_blocked = True
        self._closed = True
        # A caller dropping its reference must not release generator-owned
        # holds while an unconfirmed child could still read these modules.
        _RETAINED_KITS[self._identity] = self

    def close(self):
        if self._cleanup_blocked:
            # Revoke further use while retaining every protective file hold.
            # No delete was attempted, and another close cannot retry it.
            self._retain_unconfirmed()
            raise KitFileError("BOOTSTRAP_KIT_RUNTIME_CLEANUP_UNCONFIRMED")
        if self._closed:
            return
        self._closed = True
        self._holds.close()
        # Do not retry cleanup if the single attempt is rejected.
        remove_owned_directory(self.root, self._identity)

    def __enter__(self):
        self.require_open()
        return self

    def __exit__(self, kind, error, trace):
        try:
            self.close()
        except (OSError, ValueError):
            if error is None:
                raise KitFileError("BOOTSTRAP_KIT_CLEANUP_FAILED") from None
            error.secondary_errors = (*getattr(error, "secondary_errors", ()), "BOOTSTRAP_KIT_CLEANUP_FAILED")

    def __reduce__(self):
        raise TypeError("Verified kit handles cannot be serialized")


def _archive_members(archive, value):
    expected = {item["path"]: item for item in value["members"]}
    manifest.require(len(expected) == len(value["members"]))
    seen, members, total = set(), [], 0
    for member in archive:
        name = safe_relative(member.name)
        # PAX/GNU extensions can override path/size or hide duplicate headers.
        manifest.require(member.type == tarfile.REGTYPE and not member.pax_headers
                         and member.offset_data == member.offset + 512
                         and member.linkname == "" and member.size <= manifest.MAX_FILE
                         and member.mode in {0o400, 0o500, 0o600, 0o644, 0o755})
        manifest.require(name.casefold() not in seen and name in expected
                         and 0 <= member.size == expected[name]["size"])
        seen.add(name.casefold())
        total += member.size
        manifest.require(len(seen) <= manifest.MAX_MEMBERS and total <= manifest.MAX_ARCHIVE)
        members.append(member)
    manifest.require(len(members) == len(expected), "BOOTSTRAP_KIT_MEMBER_SET_MISMATCH")
    return members


def verify_local(*, manifest_path, archive_path, selection, destination_parent, now, production=False):
    """Verify local media; no module from the tar is imported in this function."""
    raw = read_bounded(manifest_path, manifest.MAX_MANIFEST)
    value = manifest.validate_manifest(raw, selection, now=now, production=production)
    root, root_identity, holds = None, None, ExitStack()
    try:
        with held_file(archive_path, manifest.MAX_ARCHIVE) as stream:
            manifest.require(file_digest(stream) == (value["archive"]["sha256"], value["archive"]["size"]),
                             "BOOTSTRAP_KIT_ARCHIVE_PIN_MISMATCH")
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                members = _archive_members(archive, value)
                # First complete package/content validation, then materialize;
                # no partial verified object escapes if a last member is bad.
                expected = {item["path"]: item for item in value["members"]}
                for member in members:
                    with archive.extractfile(member) as source:
                        data = source.read(member.size + 1)
                    manifest.require(len(data) == member.size and manifest.digest(data) == expected[member.name]["sha256"],
                                     "BOOTSTRAP_KIT_MEMBER_CHANGED")
                root = create_private_directory(destination_parent, prefix="verified-kit-")
                root_identity = directory_identity(root)
                for member in members:
                    target = root / member.name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.extractfile(member) as source, exclusive_file(target) as output:
                        count = 0
                        while chunk := source.read(min(1048576, member.size + 1 - count)):
                            count += len(chunk)
                            manifest.require(count <= member.size)
                            output.write(chunk)
                    manifest.require(count == member.size)
                    source = holds.enter_context(held_file(target, manifest.MAX_FILE))
                    manifest.require(file_digest(source) == (expected[member.name]["sha256"], member.size))
        with exclusive_file(root / "manifest.json") as output:
            output.write(raw)
        control = holds.enter_context(held_file(root / "manifest.json", manifest.MAX_MANIFEST))
        manifest.require(control.read(len(raw) + 1) == raw)
        return VerifiedKit(_ISSUER, root=root, selection=selection, value=value, holds=holds)
    except BaseException as error:
        try:
            holds.close()
            if root is not None:
                remove_owned_directory(root, root_identity)
        except (OSError, ValueError):
            error.secondary_errors = ("BOOTSTRAP_KIT_CLEANUP_FAILED",)
        raise


def run_local(kit, *, version, output, timeout=1800, egress=None, offline=False):
    """Invoke only the fixed verified DEV runtime in a fresh isolated process."""
    import re

    from bootstrap_kit.owned_process import ChildProcessError, OwnedProcess
    from bootstrap_kit.egress import select_egress
    egress = select_egress(egress, offline=offline)
    # The product runtime is online; task-only replay is a separate test seam.
    manifest.require(not offline, 'BOOTSTRAP_KIT_OFFLINE_RUNTIME_UNSUPPORTED')
    manifest.require(type(kit) is VerifiedKit and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?", version))
    kit.require_open()
    manifest.require(kit.manifest["classification"] == "DEVELOPMENT_ONLY"
                     and sys.version_info[:2] == (3, 12)
                     and kit.manifest["platform"] == ("windows-amd64-cp312" if os.name == "nt"
                         else "linux-amd64-cp312" if sys.platform == "linux" else "UNSUPPORTED"),
                     "BOOTSTRAP_KIT_RUNTIME_PLATFORM_OR_CLASSIFICATION_REJECTED")
    manifest.require(type(timeout) in (int, float) and 0 < timeout <= 1800)
    command = [sys.executable, "-I", "-S", "-B", str(kit.root / "library/bootstrap_kit/runtime.py"),
        "--kit-root", str(kit.root), "--manifest", str(kit.root / "manifest.json"),
        "--expected-manifest-sha256", kit.selection.expected_manifest_sha256,
        "--version", version, "--output", str(output)]
    environment = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if egress.endpoint is not None:
        command += ['--egress-proxy', egress.endpoint, '--egress-sha256', egress.identity]
        environment['ANIMEMO_EXPECTED_EGRESS_SHA256'] = egress.identity
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    try:
        owned = OwnedProcess(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=kit.root, env=environment)
    except BaseException as error:
        if getattr(error, "cleanup_failed", False):
            kit._retained_runtime = (getattr(error, "_owned_process", None), ())
            kit._retain_unconfirmed()
        raise
    process = owned.process
    kit._retained_runtime = (owned, ())
    collected, oversized = [bytearray(), bytearray()], threading.Event()
    drain_failed = threading.Event()
    def drain(index, stream):
        try:
            while chunk := stream.read(4096):
                remaining = 16384 - len(collected[index])
                collected[index].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    oversized.set()
        except OSError:
            drain_failed.set()
    readers = [threading.Thread(target=drain, args=(i, stream), daemon=True)
               for i, stream in enumerate((process.stdout, process.stderr))]
    kit._retained_runtime = (owned, tuple(readers))
    started_readers = []
    primary, cleanup_attempted = None, False
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            manifest.require(not oversized.is_set(), "BOOTSTRAP_KIT_RUNTIME_OUTPUT_LIMIT")
            remaining = deadline - time.monotonic()
            manifest.require(remaining > 0, "BOOTSTRAP_KIT_RUNTIME_TIMEOUT")
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        # Terminate descendants and observe an empty Job/group before accepting
        # any output, including after the fixed root exits successfully.
        cleanup_attempted = True
        try:
            owned.stop_and_reap(seconds=5)
        except (OSError, subprocess.SubprocessError, ChildProcessError):
            kit._retain_unconfirmed()
            raise KitFileError("BOOTSTRAP_KIT_RUNTIME_CLEANUP_UNCONFIRMED") from None
        for reader in started_readers:
            reader.join(timeout=5)
        manifest.require(not any(reader.is_alive() for reader in readers)
                         and not oversized.is_set() and process.returncode == 0
                         and not drain_failed.is_set() and not collected[1], "BOOTSTRAP_KIT_RUNTIME_FAILED")
        value = json.loads(collected[0])
        _runtime_report(value, expected_version=version)
        return value
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if not cleanup_attempted:
                cleanup_attempted = True
                owned.stop_and_reap(seconds=5)
        except (OSError, subprocess.SubprocessError, ChildProcessError):
            kit._retain_unconfirmed()
            if primary is None:
                primary = KitFileError("BOOTSTRAP_KIT_RUNTIME_CLEANUP_UNCONFIRMED")
                raise primary from None
            primary.secondary_errors = ("BOOTSTRAP_KIT_RUNTIME_CLEANUP_UNCONFIRMED",)
        for reader in started_readers:
            reader.join(timeout=5)
        cleanup_errors = []
        for stream in (process.stdout, process.stderr):
            if not any(reader.is_alive() for reader in readers):
                try:
                    stream.close()
                except OSError:
                    cleanup_errors.append("BOOTSTRAP_KIT_RUNTIME_PIPE_CLOSE_FAILED")
        if any(reader.is_alive() for reader in readers):
            cleanup_errors.append("BOOTSTRAP_KIT_RUNTIME_PIPE_NOT_CLOSED")
        pipes_closed = all(stream.closed for stream in (process.stdout, process.stderr))
        handle_closed = os.name != "nt"
        if owned.closed and pipes_closed and os.name == "nt":
            try:
                process._handle.Close()
                handle_closed = True
            except OSError:
                cleanup_errors.append("BOOTSTRAP_KIT_RUNTIME_PROCESS_HANDLE_CLOSE_FAILED")
        kit.last_runtime_cleanup = {"pid": process.pid, "process_receipt": owned.receipt,
            "pipes_closed": pipes_closed, "process_handle_closed": handle_closed,
            "errors": tuple(cleanup_errors)}
        if cleanup_errors:
            kit._retain_unconfirmed()
            if primary is None:
                error = KitFileError(cleanup_errors[0])
                error.secondary_errors = tuple(cleanup_errors[1:])
                raise error from None
            primary.secondary_errors = (*getattr(primary, "secondary_errors", ()), *cleanup_errors)
        if not kit._cleanup_blocked:
            kit._retained_runtime = None


def retained_kit_observations():
    """Bounded public identities only; no retry or output-consumption handle."""
    return tuple({"file_identity": identity, "manifest_sha256": kit.selection.expected_manifest_sha256,
                  "reason": "BOOTSTRAP_KIT_RUNTIME_CLEANUP_UNCONFIRMED"}
                 for identity, kit in _RETAINED_KITS.items())


def _runtime_report(value, *, expected_version=None):
    import re
    minimal = {"state", "production_authority_granted"}
    fields = minimal | {"subject", "platform_release_signature", "actions_provenance", "tuf_chain",
                        "large_asset_sha256", "product_execution", "bootstrap_commit"}
    manifest.require(type(value) is dict and set(value) in (minimal, fields)
                     and value["state"] == "TEST_ONLY" and value["production_authority_granted"] is False,
                     "BOOTSTRAP_KIT_RUNTIME_AUTHORITY_REJECTED")
    if set(value) == fields:
        manifest.require(type(expected_version) is str and value["subject"] == expected_version
                         and type(value["large_asset_sha256"]) is str
                         and re.fullmatch(r"[0-9a-f]{64}", value["large_asset_sha256"])
                         and value["product_execution"] == "NOT_RUN"
                         and value["bootstrap_commit"] in {"TEST_ONLY", "NOT_RUN"}
                         and all(value[field] in {"VERIFIED", "FAILED", "NOT_RUN"}
                                 for field in ("platform_release_signature", "actions_provenance", "tuf_chain")),
                         "BOOTSTRAP_KIT_RUNTIME_REPORT_INVALID")


@dataclass(frozen=True)
class DeliverySelection:
    """Independent operator selection; never populated from product metadata."""
    repository: str
    kit_version: str
    platform: str
    release_id: int
    manifest_asset_id: int
    manifest_size: int
    kit_asset_id: int

    def __post_init__(self):
        manifest.version(self.kit_version)
        manifest.require(self.repository == "yanyuhanyue/AniMemo" and self.platform in manifest.PLATFORMS
            and all(type(value) is int and 0 < value < 2**53 for value in (
                self.release_id, self.manifest_asset_id, self.kit_asset_id))
            and self.manifest_asset_id != self.kit_asset_id and type(self.manifest_size) is int
            and 0 < self.manifest_size <= manifest.MAX_MANIFEST, "BOOTSTRAP_KIT_DELIVERY_SELECTION_INVALID")


def fetch_selected_kit(*, delivery, selection, destination_parent, now, deadline,
                       cancel_event=None, production=False, egress=None):
    """Public branch: anonymous exact IDs through the already authenticated seed.

    The two GETs are untrusted transport. External manifest pin is checked before
    the archive identity is read. No production/product namespace discovery.
    """
    from bootstrap_kit.http_protocol import HttpSelection
    from bootstrap_kit.http_supervisor import SupervisedAnonymousHttp
    manifest.require(type(delivery) is DeliverySelection and type(selection) is manifest.OperatorSelection
                     and delivery.platform == selection.expected_platform)
    name = "bootstrap-trust-kit-" + delivery.platform.removesuffix("-cp312")
    version = "bootstrap-trust-kit-v" + delivery.kit_version
    client = SupervisedAnonymousHttp(private_root=destination_parent, egress=egress)
    request = HttpSelection.github_asset(version=version, release_id=delivery.release_id,
        asset_id=delivery.manifest_asset_id, name=name+".manifest.json", size=delivery.manifest_size,
        sha256=selection.expected_manifest_sha256, kind="BOOTSTRAP_KIT")
    verified = None
    try:
        with client.fetch(request, deadline=deadline, cancel_event=cancel_event) as downloaded_manifest:
            value = manifest.validate_manifest(downloaded_manifest.read_bytes(manifest.MAX_MANIFEST),
                                               selection, now=now, production=production)
            manifest.require(value["kitVersion"] == delivery.kit_version)
            if production:
                manifest.require(value["distribution"]["releaseId"] == delivery.release_id)
            archive_request = HttpSelection.github_asset(version=version, release_id=delivery.release_id,
                asset_id=delivery.kit_asset_id, name=name+".tar", size=value["archive"]["size"],
                sha256=value["archive"]["sha256"], kind="BOOTSTRAP_KIT")
            with client.fetch(archive_request, deadline=deadline, cancel_event=cancel_event) as downloaded_archive:
                verified = verify_local(manifest_path=downloaded_manifest.body_path,
                    archive_path=downloaded_archive.body_path, selection=selection,
                    destination_parent=destination_parent, now=now, production=production)
        return verified
    except BaseException as error:
        if verified is not None:
            try:
                verified.close()
            except (OSError, ValueError):
                error.secondary_errors = (*getattr(error, "secondary_errors", ()),
                                          "BOOTSTRAP_KIT_CLEANUP_FAILED")
        raise


def main(argv=None):
    from bootstrap_kit.http_protocol import HttpFailure
    from bootstrap_kit.owned_process import ChildProcessError
    from bootstrap_kit.egress import EgressSelection
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--delivery-selection", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--destination-parent", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--egress-proxy', help='Explicit credential-free loopback HTTP CONNECT endpoint')
    args = parser.parse_args(argv)
    try:
        egress = EgressSelection(args.egress_proxy)
        selection = OperatorSelection_from_local_file(args.selection)
        # Python executed this archive only after the operator's OS hash check;
        # self-check detects accidental mismatches, not independent first trust.
        invoked = Path(sys.argv[0])
        with held_file(invoked, manifest.MAX_FILE) as stream:
            manifest.require(file_digest(stream) == (selection.expected_entry_sha256,
                                                     selection.expected_entry_size))
            if args.delivery_selection is not None:
                manifest.require(args.archive is None)
                delivery = DeliverySelection(**_selected_json(args.delivery_selection))
                kit = fetch_selected_kit(delivery=delivery, selection=selection,
                    destination_parent=args.destination_parent, now=datetime.now(timezone.utc),
                    deadline=time.monotonic()+900, egress=egress)
            else:
                manifest.require(args.archive is not None)
                kit = verify_local(manifest_path=args.manifest, archive_path=args.archive,
                    selection=selection, destination_parent=args.destination_parent, now=datetime.now(timezone.utc))
            with kit:
                result = run_local(kit, version=args.version, output=args.output, egress=egress)
                print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, TypeError, OSError, RecursionError, tarfile.TarError,
            subprocess.SubprocessError, HttpFailure, ChildProcessError):
        print('{"code":"BOOTSTRAP_KIT_REJECTED","state":"TEST_ONLY","production_authority_granted":false}')
        return 2
    except KeyboardInterrupt:
        print('{"code":"BOOTSTRAP_KIT_CANCELLED","state":"TEST_ONLY","production_authority_granted":false}')
        return 130


def OperatorSelection_from_local_file(path):
    """Explicit OS-selected local pin file; never discovered from the product."""
    return manifest.OperatorSelection(**_selected_json(path))


def _selected_json(path):
    raw = read_bounded(path, 8192)
    def pairs(items):
        value = {}
        for key, item in items:
            manifest.require(key not in value)
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs)
    manifest.require(type(value) is dict)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
