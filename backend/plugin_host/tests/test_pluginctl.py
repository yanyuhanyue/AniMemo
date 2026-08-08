import importlib.util
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("anime_journal_pluginctl", ROOT / "scripts" / "pluginctl.py")
pluginctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pluginctl)


class PluginCtlBoundaryTests(SimpleTestCase):
    def test_source_import_cannot_escape_plugin_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-plugin"
            frontend = root / "frontend"
            frontend.mkdir(parents=True)
            (frontend / "index.jsx").write_text('import api from "../../../src/lib/api.js";\n', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "Plugin source import escapes plugin package boundary"):
                pluginctl._validate_source_imports(root)

    def test_unsupported_shared_named_export_fails_at_build_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-plugin"
            frontend = root / "frontend"
            frontend.mkdir(parents=True)
            (frontend / "index.jsx").write_text('import { createPortal } from "react-dom";\n', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "unsupported react-dom export"):
                pluginctl._validate_source_imports(root)

    def test_own_modules_and_supported_shared_surface_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-plugin"
            frontend = root / "frontend"
            frontend.mkdir(parents=True)
            (frontend / "view.jsx").write_text("export const View = () => null;\n", encoding="utf-8")
            (frontend / "index.jsx").write_text(
                'import { lazy, useState } from "react";\n'
                'import { Link } from "react-router-dom";\n'
                'import { View } from "./view.jsx";\n',
                encoding="utf-8",
            )
            pluginctl._validate_source_imports(root)

    def test_namespace_shared_import_fails_at_build_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-plugin"
            frontend = root / "frontend"
            frontend.mkdir(parents=True)
            (frontend / "index.jsx").write_text('import * as ReactDOM from "react-dom";\n', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "Unsupported shared module import form"):
                pluginctl._validate_source_imports(root)

    def test_react_namespace_import_also_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-plugin"
            frontend = root / "frontend"
            frontend.mkdir(parents=True)
            (frontend / "index.jsx").write_text('import * as React from "react";\n', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "Unsupported shared module import form"):
                pluginctl._validate_source_imports(root)

    def test_default_shared_import_fails_at_build_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo-plugin"
            frontend = root / "frontend"
            frontend.mkdir(parents=True)
            (frontend / "index.jsx").write_text('import Router from "react-router-dom";\n', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "Unsupported shared module import form"):
                pluginctl._validate_source_imports(root)
