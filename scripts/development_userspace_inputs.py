"""Closed inputs for a zero-sudo DEV userspace probe; no execution authority."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

PURPOSE = "USERSPACE_PLATFORM_VALIDATION"
TEST_IDS = (
    "installer.tests.test_tokenless_stage0.TokenlessStage0BoundaryTests.test_held_file_rejects_symlink_if_host_permits_creation",
    "installer.tests.test_production.ProductionInstallerCompositionTests.test_fresh_root_preparation_honors_modes_under_restrictive_umask",
    "scripts.tests.test_trust_bootstrap.InitialTrustBootstrapTests.test_initial_trust_kit_descriptor_reads_reject_empty_members",
    "scripts.tests.test_trust_bootstrap.InitialTrustBootstrapTests.test_initial_trust_kit_rejects_post_read_membership_change",
    "release.test_release_pipeline.FrozenPrepublicationMaterialTests.test_builder_rejects_temporary_path_replacement_without_touching_victim",
    "release.test_release_pipeline.FrozenPrepublicationMaterialTests.test_builder_removes_wrong_output_when_temp_is_swapped_at_publish",
    "updater.tests.test_archive_handoff.ArchiveHandoffBoundaryTests.test_posix_truncation_during_borrow_is_detected_on_exit",
)
EXTRA_TEST_IDS = (
    "scripts.tests.test_formal_failure_diagnostics.FaultProtocolTests.test_real_dedicated_inherited_fd_through_child",
)
ROLES = (
    "kit_manifest",
    "kit_archive",
    "kit_entry",
    "kit_selection",
    "original_archive",
    "release_metadata",
    "release_bundle",
    "release_manifest",
    "deployment_contract",
    "actions_0",
    "actions_1",
    "actions_2",
    "actions_3",
    "actions_4",
)
ORIGINAL = "sha256:45d6a6d8d4653dd7b435e093de73ed675fdf1174a86bc6f5372be66255158950"
COMMIT = "94f64e7fba10f4a013281d599e695c3d6da36208"
TAG_OBJECT = "a10f7fcaca6f2ca11e730ca7b0b4269616db2739"


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()


def identity(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def require(value, code="DEVELOPMENT_USERSPACE_INPUT_INVALID"):
    if not value:
        from scripts.candidate_vm_harness import CandidateHarnessError

        raise CandidateHarnessError(code)


def read_inputs(path, expected_sha256, *, source_sha, source_tree):
    from release.materials import read_bounded_release_file, reject_duplicate_json_keys

    raw = read_bounded_release_file(
        Path(path), subject="DEV userspace inputs", maximum=1024 * 1024
    )
    require(identity(raw) == expected_sha256)
    value = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
    require(type(value) is dict)
    separate_kit_source = (
        value.get("schema") == "animemo.development-userspace-inputs/v2"
    )
    expected_keys = {
        "schema",
        "purpose",
        "source_sha",
        "source_tree",
        "files",
        "test_ids",
    }
    if separate_kit_source:
        expected_keys.add("kit_source")
    require(
        set(value) == expected_keys
        and value["schema"]
        in {
            "animemo.development-userspace-inputs/v1",
            "animemo.development-userspace-inputs/v2",
        }
        and value["purpose"] == PURPOSE
        and value["source_sha"] == source_sha
        and value["source_tree"] == source_tree
        and value["test_ids"] == list(TEST_IDS)
        and type(value["files"]) is dict
        and set(value["files"]) == set(ROLES)
    )
    if separate_kit_source:
        require(
            type(value["kit_source"]) is dict
            and set(value["kit_source"]) == {"commit", "tree"}
            and all(
                type(item) is str and re.fullmatch(r"[0-9a-f]{40}", item)
                for item in value["kit_source"].values()
            )
        )
    for role, item in value["files"].items():
        require(
            type(item) is dict
            and set(item) == {"path", "size", "sha256"}
            and type(item["path"]) is str
            and Path(item["path"]).is_absolute()
            and type(item["size"]) is int
            and 0
            < item["size"]
            <= (256 if role == "original_archive" else 64) * 1024 * 1024
            and type(item["sha256"]) is str
            and len(item["sha256"]) == 71
            and item["sha256"].startswith("sha256:")
            and all(c in "0123456789abcdef" for c in item["sha256"][7:])
        )
    require(
        value["files"]["original_archive"]["size"] == 134010880
        and value["files"]["original_archive"]["sha256"] == ORIGINAL
    )
    return value


def verify_kit_library_compatibility(*, kit_root, kit_manifest, execution_root):
    """DEV-only compatibility: every independently verified library byte must match."""
    from release.materials import read_bounded_release_file

    members = {}
    for item in kit_manifest["members"]:
        name = PurePosixPath(item["path"])
        if name.parts[0] != "library":
            continue
        relative = PurePosixPath(*name.parts[1:])
        require(
            not relative.is_absolute()
            and len(name.parts) > 1
            and not {"..", ".", "tests", "__pycache__"}.intersection(relative.parts),
            "DEVELOPMENT_USERSPACE_KIT_COMPATIBILITY_INVALID",
        )
        old = read_bounded_release_file(
            Path(kit_root) / name.as_posix(),
            subject="Verified DEV Kit library",
            maximum=64 * 1024 * 1024,
        )
        current = read_bounded_release_file(
            Path(execution_root) / relative.as_posix(),
            subject="Held DEV execution library",
            maximum=64 * 1024 * 1024,
        )
        require(
            len(old) == item["size"]
            and identity(old) == item["sha256"]
            and current == old,
            "DEVELOPMENT_USERSPACE_KIT_COMPATIBILITY_INVALID",
        )
        members[name.as_posix()] = item["sha256"]
    require(bool(members), "DEVELOPMENT_USERSPACE_KIT_COMPATIBILITY_INVALID")
    return {
        "mode": "ALL_LIBRARY_BYTES_EQUAL_EXECUTION_SNAPSHOT",
        "library_members": len(members),
        "library_inventory_digest": identity(canonical(members)),
        "verified_archive_members": len(kit_manifest["members"]),
    }


def verify_prepared_kit_compatibility(*, target, execution_root, selection):
    """Keep Kit read holds outside the DEV tree's DELETE-access directory holds."""
    from datetime import datetime, timezone
    from bootstrap_kit.safe_files import (
        create_private_directory,
        directory_identity,
        remove_owned_directory,
    )
    from bootstrap_kit.seed import verify_local
    from release.materials import read_bounded_release_file

    temporary = create_private_directory(
        Path(execution_root).anchor, prefix="animemo-userspace-compat-"
    )
    owned_identity = directory_identity(temporary)
    primary = None
    try:
        for name, maximum in (
            ("kit_manifest", 1024 * 1024),
            ("kit_archive", 64 * 1024 * 1024),
        ):
            data = read_bounded_release_file(
                Path(target) / name,
                subject="Bound DEV Kit verification copy",
                maximum=maximum,
            )
            with (temporary / name).open("xb") as stream:
                stream.write(data)
        with verify_local(
            manifest_path=temporary / "kit_manifest",
            archive_path=temporary / "kit_archive",
            selection=selection,
            destination_parent=temporary,
            now=datetime.now(timezone.utc),
            production=False,
        ) as verified_kit:
            return verify_kit_library_compatibility(
                kit_root=verified_kit.root,
                kit_manifest=verified_kit.manifest,
                execution_root=execution_root,
            )
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            remove_owned_directory(temporary, owned_identity)
        except (OSError, ValueError):
            if primary is None:
                require(False, "DEVELOPMENT_USERSPACE_COMPATIBILITY_CLEANUP_FAILED")
            primary.secondary_errors = (
                *getattr(primary, "secondary_errors", ()),
                "DEVELOPMENT_USERSPACE_COMPATIBILITY_CLEANUP_FAILED",
            )


def prepare_userspace_inputs(
    *, execution_root, source_sha, source_tree, manifest, expected_manifest_sha256
):
    from bootstrap_kit.manifest import OperatorSelection, validate_manifest
    from datetime import datetime, timezone
    from release.materials import read_bounded_release_file, reject_duplicate_json_keys
    from scripts.development_source import ROOT

    value = read_inputs(
        manifest,
        expected_manifest_sha256,
        source_sha=source_sha,
        source_tree=source_tree,
    )
    target = Path(execution_root) / "userspace-probe"
    target.mkdir(mode=0o700)
    additions, payloads, total = {}, {}, 0
    for role in ROLES:
        item = value["files"][role]
        data = read_bounded_release_file(
            Path(item["path"]),
            subject="Bound DEV userspace material",
            maximum=(256 if role == "original_archive" else 64) * 1024 * 1024,
        )
        require(len(data) == item["size"] and identity(data) == item["sha256"])
        total += len(data)
        require(total <= 256 * 1024 * 1024)
        with (target / role).open("xb") as stream:
            stream.write(data)
        additions["userspace-probe/" + role] = item["sha256"]
        if role not in {"original_archive", "kit_archive"}:
            payloads[role] = data
    selection = OperatorSelection(
        **json.loads(
            payloads["kit_selection"], object_pairs_hook=reject_duplicate_json_keys
        )
    )
    kit = validate_manifest(
        payloads["kit_manifest"],
        selection,
        now=datetime.now(timezone.utc),
        production=False,
    )
    kit_source = value.get("kit_source", {"commit": source_sha, "tree": source_tree})
    require(
        selection.expected_platform == "linux-amd64-cp312"
        and selection.expected_source_commit == kit_source["commit"]
        and selection.expected_source_tree == kit_source["tree"]
        and selection.expected_entry_sha256 == identity(payloads["kit_entry"])
        and selection.expected_entry_size == len(payloads["kit_entry"])
        and kit["archive"]["sha256"] == value["files"]["kit_archive"]["sha256"]
        and kit["archive"]["size"] == value["files"]["kit_archive"]["size"]
    )
    compatibility = verify_prepared_kit_compatibility(
        target=target,
        execution_root=execution_root,
        selection=selection,
    )
    metadata = json.loads(
        payloads["release_metadata"], object_pairs_hook=reject_duplicate_json_keys
    )
    require(
        metadata["id"] == 392113678
        and metadata["tag_name"] == "v2.0.0-rc.3"
        and metadata["immutable"] is True
        and metadata["draft"] is False
    )
    release = json.loads(
        payloads["release_manifest"], object_pairs_hook=reject_duplicate_json_keys
    )
    require(
        release["release"]["commit"] == COMMIT
        and release["release"]["version"] == "v2.0.0-rc.3"
    )
    for name in ("release_bundle", *(f"actions_{i}" for i in range(5))):
        envelope = json.loads(
            payloads[name], object_pairs_hook=reject_duplicate_json_keys
        )
        statement = json.loads(
            base64.b64decode(envelope["dsseEnvelope"]["payload"], validate=True),
            object_pairs_hook=reject_duplicate_json_keys,
        )
        require(
            statement["predicateType"]
            == (
                "https://in-toto.io/attestation/release/v0.2"
                if name == "release_bundle"
                else "https://slsa.dev/provenance/v1"
            )
        )
    # Fixed package subtree only: needed tests, their Python fixture imports and
    # helper modules. No arbitrary path/argv supplied by the input record.
    raw = subprocess.check_output(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-tree",
            "-rz",
            "HEAD",
            "--",
            "scripts",
            "installer/tests",
            "updater/tests",
        ],
        timeout=30,
    )
    for row in filter(None, raw.decode().split("\0")):
        header, name = row.split("\t", 1)
        mode, kind, blob = header.split(" ")
        if PurePosixPath(name).suffix != ".py":
            continue
        require(mode in {"100644", "100755"} and kind == "blob")
        data = read_bounded_release_file(
            ROOT / name, subject="Fixed DEV Python test source", maximum=4 * 1024 * 1024
        )
        require(
            hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            == blob
        )
        destination = Path(execution_root) / name
        if destination.exists():
            require(destination.read_bytes() == data)
            continue
        total += len(data)
        require(total <= 256 * 1024 * 1024 and len(additions) < 2048)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(data)
        additions[name] = identity(data)
    context = {
        "schema": "animemo.development-userspace-context/v1",
        "purpose": PURPOSE,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "input_manifest_sha256": expected_manifest_sha256,
        "kit_source": kit_source,
        "kit_compatibility": compatibility,
        "test_ids": list(TEST_IDS),
        "extra_test_ids": list(EXTRA_TEST_IDS),
        "roles": {
            role: {
                "size": value["files"][role]["size"],
                "sha256": value["files"][role]["sha256"],
            }
            for role in ROLES
        },
        "sudo_capture_attempts": 0,
        "formal_authority_granted": False,
        "product_execution": "NOT_RUN",
        "network_policy": "REQUIRE_RUNTIME_SECCOMP_PROOF",
    }
    encoded = canonical(context)
    with (target / "context.json").open("xb") as stream:
        stream.write(encoded)
    additions["userspace-probe/context.json"] = identity(encoded)
    return additions, identity(encoded)
