#!/usr/bin/env python3
"""Static packaging checks for the standalone AstrBot Bridge export."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridges" / "astrbot_plugin_animemo_bridge"
REQUIRED = {"main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "README.md"}
PAGE_REQUIRED = {"index.html", "app.js", "style.css"}
ALLOWED_TYPES = {"int", "float", "bool", "string", "text", "list", "file", "object", "template_list", "dict"}


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
    if metadata.get("astrbot_version") != ">=4.27.2":
        raise SystemExit("metadata.yaml must require the audited AstrBot >=4.27.2 runtime")
    if metadata.get("version") != "0.1.2":
        raise SystemExit("AstrBot Bridge pairing-log/timezone hotfix must use version 0.1.2")
    schema = json.loads((BRIDGE / "_conf_schema.json").read_text(encoding="utf-8"))
    required_config = {"enabled", "animemo_base_url", "key_id", "secret", "poll_events", "poll_wait_seconds", "request_timeout_seconds", "allow_group_commands", "developer_commands", "verify_tls"}
    if set(schema) != required_config:
        raise SystemExit("_conf_schema.json configuration keys do not match Bridge contract")
    for key, item in schema.items():
        if item.get("type") not in ALLOWED_TYPES:
            raise SystemExit(f"configuration {key} uses unsupported AstrBot type {item.get('type')}")
    expected_types = {
        "enabled": "bool",
        "animemo_base_url": "string",
        "key_id": "string",
        "secret": "string",
        "poll_events": "bool",
        "poll_wait_seconds": "int",
        "request_timeout_seconds": "int",
        "allow_group_commands": "bool",
        "developer_commands": "bool",
        "verify_tls": "bool",
    }
    for key, expected in expected_types.items():
        if schema[key].get("type") != expected:
            raise SystemExit(f"configuration {key} must use AstrBot type {expected}")
    if schema["secret"].get("invisible") is not True:
        raise SystemExit("secret must use invisible=true; production should prefer the environment override")
    if schema["allow_group_commands"].get("default") is not False:
        raise SystemExit("allow_group_commands must remain disabled by default")
    if schema["verify_tls"].get("default") is not True:
        raise SystemExit("verify_tls must remain enabled by default")
    page_root = BRIDGE / "pages" / "status"
    missing_page = sorted(name for name in PAGE_REQUIRED if not (page_root / name).is_file())
    if missing_page:
        raise SystemExit(f"AstrBot diagnostics page missing files: {', '.join(missing_page)}")
    page_script = (page_root / "app.js").read_text(encoding="utf-8")
    page_html = (page_root / "index.html").read_text(encoding="utf-8")
    for api_name in ("AstrBotPluginPage", "ready", "apiGet", "apiPost"):
        if api_name not in page_script:
            raise SystemExit(f"AstrBot diagnostics page missing Plugin Pages API: {api_name}")
    if 'type="module"' not in page_html:
        raise SystemExit("AstrBot diagnostics page script must be an external module")
    if "localStorage" in page_script or "document.cookie" in page_script:
        raise SystemExit("AstrBot diagnostics page must not read dashboard credentials")
    requirements = (BRIDGE / "requirements.txt").read_text(encoding="utf-8").lower()
    if "requests" in requirements or "httpx" not in requirements:
        raise SystemExit("requirements.txt must use httpx and must not use requests")
    production_main = (BRIDGE / "main.py").read_text(encoding="utf-8")
    for forbidden in ("_FallbackLogger", "class Star:", "class Context:", "astrbot.api.message"):
        if forbidden in production_main:
            raise SystemExit(f"production Bridge contains a fake or invalid AstrBot runtime path: {forbidden}")
    if "_config_bool" not in production_main or "_validated_timing" not in production_main:
        raise SystemExit("production Bridge must validate runtime boolean and timing values")
    event_source = (BRIDGE / "animemo_bridge" / "events.py").read_text(encoding="utf-8")
    if "astrbot.api.message" in event_source:
        raise SystemExit("event delivery must import MessageChain from astrbot.api.event")
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
