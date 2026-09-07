from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from scripts.smoke_installer_materials import _probe


class MaterialProbeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.materials = self.root / "materials"
        self.materials.mkdir()
        self.cwd = self.root / "outside-materials"
        self.cwd.mkdir()
        # Guard probes never parse this path; no dependency/artifact is mocked.
        self.archive = self.root / "unused-archive"
        self.archive.write_bytes(b"")

    def probe(self, source):
        return _probe(
            python=Path(sys.executable), materials_root=self.materials,
            archive=self.archive, cwd=self.cwd,
            request={"kind": "guard-test", "source": source},
        )

    def test_network_process_and_file_mutations_are_rejected(self):
        for source, code in (
            ("import socket; socket.socket()", "NETWORK_OR_PROCESS_FORBIDDEN"),
            ("import subprocess; subprocess.run([sys.executable, '-c', 'pass'])", "NETWORK_OR_PROCESS_FORBIDDEN"),
            ("Path('changed').write_text('data')", "WRITE_FORBIDDEN"),
            ("Path('directory').mkdir()", "MUTATION_FORBIDDEN"),
        ):
            with self.subTest(source=source):
                completed = self.probe(source)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("MATERIAL_SMOKE_" + code, completed.stderr)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_ambient_pythonpath_and_cwd_cannot_fill_a_missing_product_module(self):
        ambient = self.root / "checkout"
        (ambient / "scripts").mkdir(parents=True)
        (ambient / "scripts" / "missing.py").write_text("VALUE = True\n", encoding="utf-8")
        (self.cwd / "scripts").mkdir()
        (self.cwd / "scripts" / "missing.py").write_text("VALUE = True\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(ambient)}):
            completed = self.probe("import scripts.missing")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("ModuleNotFoundError", completed.stderr)

    def test_material_root_resolves_normally_and_product_environment_is_empty(self):
        (self.materials / "scripts").mkdir()
        (self.materials / "scripts" / "delivered.py").write_text("VALUE = 17\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DATABASE_URL": "synthetic-not-a-connection"}):
            completed = self.probe(
                "import scripts.delivered; print(json.dumps({"
                "'value': scripts.delivered.VALUE, 'database': os.environ.get('DATABASE_URL'), "
                "'cwd': str(Path.cwd()), 'unicode': '\u6750\u6599'}))"
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result, {"value": 17, "database": None, "cwd": str(self.cwd), "unicode": "材料"})


if __name__ == "__main__":
    unittest.main()
