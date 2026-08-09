#!/usr/bin/env python3
"""Static packaging checks for the standalone AstrBot Bridge export."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridges" / "astrbot_plugin_animemo_bridge"
REQUIRED = {"main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "README.md"}


def parse_metadata(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"\'')
    return values


def validate():
    missing = sorted(name for name in REQUIRED if not (BRIDGE / name).is_file())
    if missing:
        raise SystemExit(f"AstrBot Bridge missing required files: {', '.join(missing)}")
    metadata = parse_metadata(BRIDGE / "metadata.yaml")
    for field in ("name", "display_name", "version", "desc", "license"):
        if not metadata.get(field):
            raise SystemExit(f"metadata.yaml missing {field}")
    schema = json.loads((BRIDGE / "_conf_schema.json").read_text(encoding="utf-8"))
    required_config = {"enabled", "animemo_base_url", "key_id", "secret", "poll_events", "poll_wait_seconds", "request_timeout_seconds", "allow_group_commands", "developer_commands", "verify_tls"}
    if set(schema) != required_config:
        raise SystemExit("_conf_schema.json configuration keys do not match Bridge contract")
    requirements = (BRIDGE / "requirements.txt").read_text(encoding="utf-8").lower()
    if "requests" in requirements or "httpx" not in requirements:
        raise SystemExit("requirements.txt must use httpx and must not use requests")
    for path in BRIDGE.rglob("*"):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_file() and path.name == ".env":
            raise SystemExit(f"runtime package contains forbidden file: {path.relative_to(BRIDGE)}")
        if path.is_file() and any(token in path.read_text(encoding="utf-8", errors="ignore") for token in ("sk_live_", "prod-secret", "BEGIN PRIVATE KEY")):
            raise SystemExit(f"possible real secret in {path.relative_to(BRIDGE)}")
    size = sum(path.stat().st_size for path in BRIDGE.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    if size > 2 * 1024 * 1024:
        raise SystemExit(f"AstrBot Bridge package is too large: {size} bytes")
    print(f"validated AstrBot Bridge {metadata['version']} ({size} bytes)")


if __name__ == "__main__":
    validate()
