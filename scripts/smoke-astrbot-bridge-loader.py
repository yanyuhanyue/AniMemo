#!/usr/bin/env python3
"""Exercise AstrBot's real schema parser and PluginManager loader."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SOURCE = ROOT / "bridges" / "astrbot_plugin_animemo_bridge"
PLUGIN_NAME = "astrbot_plugin_animemo_bridge"


def _read_metadata(path: Path) -> dict:
    import yaml

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


class SmokeContext:
    def __init__(self, config):
        self._config = config
        self.web_routes = []
        self._star_manager = None

    def register_web_api(self, path, handler, methods, description):
        self.web_routes.append((path, handler, methods, description))

    def get_all_stars(self):
        return []

    def get_config(self):
        return self._config


def copy_plugin(destination: Path, plugin_zip: Path | None = None) -> None:
    if plugin_zip:
        with zipfile.ZipFile(plugin_zip) as archive:
            archive.extractall(destination.parent)
        if not destination.is_dir():
            raise RuntimeError(f"ZIP must contain the {PLUGIN_NAME} root directory")
        return
    shutil.copytree(
        BRIDGE_SOURCE,
        destination,
        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".env"),
    )


async def run(runtime_root: Path, label: str, plugin_zip: Path | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="animemo-astrbot-loader-", ignore_cleanup_errors=True) as temp:
        temp_root = Path(temp)
        os.environ["ASTRBOT_ROOT"] = str(temp_root)
        os.environ["ASTRBOT_RELOAD"] = "0"
        os.chdir(temp_root)
        sys.path.insert(0, str(temp_root))
        sys.path.insert(0, str(runtime_root))

        from astrbot.core import db_helper
        from astrbot.core.config.astrbot_config import AstrBotConfig
        from astrbot.core.star import star_manager as star_manager_module
        from astrbot.core.star.star_manager import PluginManager

        plugin_destination = temp_root / "data" / "plugins" / PLUGIN_NAME
        plugin_destination.parent.mkdir(parents=True, exist_ok=True)
        copy_plugin(plugin_destination, plugin_zip)

        schema = json.loads((plugin_destination / "_conf_schema.json").read_text(encoding="utf-8"))
        metadata = _read_metadata(plugin_destination / "metadata.yaml")
        expected_version = str(metadata.get("version", "")).strip()
        assert expected_version
        config_path = temp_root / "data" / "cmd_config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = AstrBotConfig(
            config_path=str(config_path),
            default_config={"dashboard": {"jwt_secret": "loader-smoke"}},
        )
        plugin_config_path = temp_root / "data" / "config" / "plugin-smoke.json"
        plugin_config_path.parent.mkdir(parents=True, exist_ok=True)
        parsed = AstrBotConfig(config_path=str(plugin_config_path), schema=schema)
        assert parsed["enabled"] is True
        assert parsed["poll_wait_seconds"] == 20
        assert parsed["request_timeout_seconds"] == 35
        assert parsed["allow_group_commands"] is False

        context = SmokeContext(config)
        manager = PluginManager(context, config)
        success, error = await manager.load(specified_dir_name=PLUGIN_NAME)
        assert success, error
        metadata = star_manager_module.star_map[f"data.plugins.{PLUGIN_NAME}.main"]
        assert metadata.star_cls is not None
        assert metadata.version == expected_version
        assert len(context.web_routes) == 4
        await metadata.star_cls.terminate()
        await db_helper.engine.dispose()
        print(
            json.dumps(
                {
                    "label": label,
                    "schema_parse": "PASS",
                    "config_create": "PASS",
                    "plugin_import": "PASS",
                    "plugin_instantiate": "PASS",
                    "plugin_initialize": "PASS",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--label", default="ASTRBOT REAL LOADER")
    parser.add_argument("--plugin-zip", type=Path)
    args = parser.parse_args()
    asyncio.run(
        run(
            args.runtime_root.resolve(),
            args.label,
            args.plugin_zip.resolve() if args.plugin_zip else None,
        )
    )


if __name__ == "__main__":
    main()
