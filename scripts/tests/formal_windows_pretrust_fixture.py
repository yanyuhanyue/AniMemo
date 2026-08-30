from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from release.formal_windows_pretrust import (
    build_formal_windows_pretrust_kit,
    create_windows_private_directory,
)
from scripts.tests.trust_kit_fixture import create_test_initial_trust_kit


@contextmanager
def private_windows_test_directory() -> Iterator[str]:
    """Use a path that satisfies the production full-chain Windows contract."""

    if os.name != "nt":
        with tempfile.TemporaryDirectory() as directory:
            yield directory
        return
    volume_root = Path(Path(__file__).resolve().anchor)
    root = create_windows_private_directory(
        volume_root, prefix="animemo-formal-test"
    )
    created = root.lstat()
    identity = (int(created.st_dev), int(created.st_ino))
    try:
        yield str(root)
    finally:
        observed = root.lstat()
        if (
            not root.is_absolute()
            or root.parent != volume_root
            or root.is_symlink()
            or getattr(observed, "st_file_attributes", 0) & 0x400
            or (int(observed.st_dev), int(observed.st_ino)) != identity
        ):
            raise RuntimeError("Windows private test root发生rebound")
        shutil.rmtree(root)


def minimal_pe32_plus_amd64(*, marker: bytes = b"FORMAL-WINDOWS") -> bytes:
    value = bytearray(512)
    value[0:2] = b"MZ"
    value[0x3C:0x40] = (0x80).to_bytes(4, "little")
    value[0x80:0x84] = b"PE\0\0"
    value[0x84:0x86] = (0x8664).to_bytes(2, "little")
    value[0x86:0x88] = (1).to_bytes(2, "little")
    value[0x94:0x96] = (0xF0).to_bytes(2, "little")
    value[0x96:0x98] = (0x0022).to_bytes(2, "little")
    value[0x98:0x9A] = (0x20B).to_bytes(2, "little")
    value[-len(marker) :] = marker
    return bytes(value)


def create_test_formal_windows_pretrust_kit(
    root: Path,
    *,
    source_initial_trust_kit: Path | None = None,
) -> Path:
    """Create non-authoritative contract bytes; native ACL has separate tests."""

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    authority_root = create_windows_private_directory(
        root, prefix="formal-test-authority"
    )
    if source_initial_trust_kit is None:
        source = create_test_initial_trust_kit(authority_root)
    else:
        source = authority_root / "source-initial-pretrust"
        source.mkdir()
        for item in Path(source_initial_trust_kit).iterdir():
            shutil.copyfile(item, source / item.name)
    verifier = authority_root / "formal-release-verifier.exe"
    verifier.write_bytes(minimal_pe32_plus_amd64())
    output = authority_root / "test-only-formal-windows-amd64-pretrust-v1"
    build_formal_windows_pretrust_kit(
        verifier=verifier,
        source_initial_trust_kit=source,
        output=output,
    )
    return output


def create_test_installer_trust_kits(root: Path) -> tuple[Path, Path]:
    initial = create_test_initial_trust_kit(root)
    formal = create_test_formal_windows_pretrust_kit(
        root, source_initial_trust_kit=initial
    )
    return initial, formal
