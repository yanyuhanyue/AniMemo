"""Explicit synthetic DEV manifests; no fixture grants production authority."""
from datetime import datetime, timezone
from pathlib import Path

from bootstrap_kit import build, manifest

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
TRUST_BYTES = {path: ("TEST_ONLY_NOT_CRYPTO_VERIFIED:" + path + "\n").encode()
               for path in manifest.REQUIRED_TRUST_ROLES}


def metadata():
    return {
        "schema": "animemo.bootstrap-trust-kit/v1", "kind": "ANIMEMO_BOOTSTRAP_TRUST_KIT",
        "kitVersion": "1.0.0", "sequence": 3, "platform": "windows-amd64-cp312", "pythonAbi": "cp312",
        "classification": "DEVELOPMENT_ONLY", "issuedAt": "2026-09-19T00:00:00Z",
        "expiresAt": "2026-10-01T00:00:00Z", "disabled": False, "minimumKitVersion": "1.0.0",
        "minimumSequence": 1, "updatePolicy": "OPERATOR_RESELECT_ONLY",
        "source": {"repository": "yanyuhanyue/AniMemo", "commit": "a"*40, "tree": "b"*40,
                   "locator": "https://github.com/yanyuhanyue/AniMemo/tree/" + "a"*40},
        "build": {"identity": "sha256:"+"c"*64, "toolInputs": [
            {"name": "SYNTHETIC_TEST_SOURCE", "sha256": manifest.digest(b"x"), "size": 1}]},
        "policies": {"release": "sha256:"+"e"*64, "actions": "sha256:"+"f"*64},
        "trustRoots": {domain: {"initialSha256": pin,
            "currentSha256": manifest.digest(TRUST_BYTES['trust/'+domain+'-tuf-root.json']),
            "expiresAt": "2026-11-01T00:00:00Z"} for domain, pin in manifest.INITIAL_ROOT_PINS.items()},
        "osPrerequisites": {"python": "3.12", "stdlib": "OS_TRUSTED",
            "tls": "OS_CA_HOSTNAME_VERIFIED", "clock": "OPERATOR_TRUSTED_UTC"},
        "distribution": {"status": "NOT_PUBLISHED", "releaseId": None, "manifestUrl": None, "archiveUrl": None},
    }


def selection(value):
    return manifest.OperatorSelection(manifest.digest(manifest.canonical(value)), value["platform"], 3,
        "1.0.0", value["entry"]["sha256"], value["entry"]["size"], "a"*40, "b"*40)


def minimal_manifest():
    return {**metadata(), "entry": {"name": "entry.pyz", "sha256": "sha256:"+"3"*64, "size": 100},
            "archive": {"sha256": "sha256:"+"4"*64, "size": 10240},
            "members": sorted([{"path": "library/bootstrap_kit/runtime.py", "size": 1,
                "sha256": "sha256:"+"5"*64, "type": "file", "role": "ENTRY"},
                *[{"path": path, "size": len(raw), "sha256": manifest.digest(raw), "type": "file",
                   "role": manifest.REQUIRED_TRUST_ROLES[path]} for path, raw in TRUST_BYTES.items()]],
                key=lambda item: item['path'])}


def tool_inputs(root):
    source = Path(root) / "synthetic-build-tool"
    if not source.exists():
        source.write_bytes(b"x")
    return [build.ToolInput("SYNTHETIC_TEST_SOURCE", source, manifest.digest(b"x"), 1)]


def synthetic_kit(root, *, runtime=None):
    """File verification fixture; this deliberately does not test crypto/HTTP."""
    root = Path(root)
    source, output = root / "source", root / "output"
    source.mkdir()
    output.mkdir()
    runtime = runtime or ("import json,sys\n"
        "assert all('site-packages' not in p for p in sys.path)\n"
        "print(json.dumps({'state':'TEST_ONLY','production_authority_granted':False}))\n")
    members = {**TRUST_BYTES, "library/bootstrap_kit/runtime.py": runtime.encode(),
               "library/bootstrap_kit/__init__.py": b"", "library/last.py": b"# final member\n"}
    inputs = []
    for name, raw in members.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        role = manifest.REQUIRED_TRUST_ROLES.get(name, "ENTRY" if name.endswith("runtime.py") else "LIBRARY")
        inputs.append(build.BuildInput(name, path, manifest.digest(raw), role))
    entry_sources = []
    for name in ("__init__", "seed", "manifest", "safe_files", "http_supervisor", "http_worker", "http_protocol", "owned_process", "egress"):
        path = source / (name + ".py")
        path.write_bytes(b"# SYNTHETIC_ENTRY_NOT_FOR_DISTRIBUTION\n")
        entry_sources.append(build.BuildInput("bootstrap_kit/" + name + ".py", path,
            manifest.digest(path.read_bytes()), "ENTRY"))
    result = build.build_kit(inputs=inputs, entry_sources=entry_sources, tool_inputs=tool_inputs(root), output_dir=output,
                             metadata=metadata(), now=NOW)
    return result, inputs, entry_sources
