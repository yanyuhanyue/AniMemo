import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from check_license_docs import (
    POLYFORM_BLOB,
    POLYFORM_LINES,
    POLYFORM_PATH,
    POLYFORM_SHA256,
    POLYFORM_SIZE,
    PRODUCT_IDENTITY,
    README_ASSET_WARNING,
    ROOT_LICENSE_PATH,
    ValidationError,
    _git_blob,
    _git_diff_names,
    _logical_lines,
    _node_lock,
    _normalized_text_sha256,
    _read_bytes,
    _read_text,
    main,
    validate_all,
    validate_documents,
    validate_polyform,
)


class LicenseDocumentationTests(unittest.TestCase):
    PRIVATE_SENTINEL = (
        r"PRIVATE_TOKEN_SENTINEL C:\private\operator\license-input.json "
        "Traceback SELECT secret FROM private_table"
    )

    def test_repository_license_documentation_is_current(self):
        validate_all()

    def test_polyform_official_fingerprints(self):
        payload = (ROOT / POLYFORM_PATH).read_bytes()
        self.assertEqual(_git_blob(payload), POLYFORM_BLOB)
        self.assertEqual(__import__("hashlib").sha256(payload).hexdigest(), POLYFORM_SHA256)
        self.assertEqual(len(payload), POLYFORM_SIZE)
        self.assertEqual(_logical_lines(payload), POLYFORM_LINES)
        validate_polyform()

    def test_readme_keeps_final_product_identity_and_asset_warning(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(PRODUCT_IDENTITY, readme)
        self.assertIn(README_ASSET_WARNING, readme)
        validate_documents()

    def test_root_license_is_byte_identical_to_named_polyform_copy(self):
        self.assertEqual(
            (ROOT / ROOT_LICENSE_PATH).read_bytes(),
            (ROOT / POLYFORM_PATH).read_bytes(),
        )

    def test_release_documents_have_no_legacy_provenance_markers(self):
        validate_documents()

    def test_polyform_validator_rejects_changed_bytes(self):
        payload = (ROOT / POLYFORM_PATH).read_bytes()
        self.assertNotEqual(_git_blob(payload + b"\n"), POLYFORM_BLOB)
        self.assertNotEqual(_logical_lines(payload + b"\n"), POLYFORM_LINES)

    def test_validation_error_is_a_runtime_error(self):
        self.assertTrue(issubclass(ValidationError, RuntimeError))

    def test_validation_error_discards_untrusted_reason_prose(self):
        error = ValidationError(self.PRIVATE_SENTINEL)

        self.assertEqual(error.reason_code, "license_validation_rule_failed")
        self.assertEqual(str(error), "license_validation_rule_failed")
        self.assertNotIn(self.PRIVATE_SENTINEL, repr(error))

    def test_read_failure_drops_os_error_and_absolute_path_without_chain(self):
        absolute_root = str(ROOT.resolve())
        failure = PermissionError(f"{absolute_root} {self.PRIVATE_SENTINEL}")
        with patch.object(Path, "read_bytes", side_effect=failure):
            with self.assertRaises(ValidationError) as raised:
                _read_bytes(self.PRIVATE_SENTINEL)

        self.assertEqual(raised.exception.reason_code, "license_input_read_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(absolute_root, repr(raised.exception))
        self.assertNotIn(self.PRIVATE_SENTINEL, repr(raised.exception))

    def test_invalid_utf8_drops_decoder_detail_without_chain(self):
        with patch("check_license_docs._read_bytes", return_value=b"\xffPRIVATE"):
            with self.assertRaises(ValidationError) as raised:
                _read_text(self.PRIVATE_SENTINEL)

        self.assertEqual(
            raised.exception.reason_code,
            "license_input_encoding_invalid",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(self.PRIVATE_SENTINEL, repr(raised.exception))

    def test_invalid_json_drops_parser_detail_and_input_path_without_chain(self):
        invalid = '{"private": "' + self.PRIVATE_SENTINEL
        with patch("check_license_docs._read_text", return_value=invalid):
            with self.assertRaises(ValidationError) as raised:
                _node_lock()

        self.assertEqual(raised.exception.reason_code, "license_input_json_invalid")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(self.PRIVATE_SENTINEL, repr(raised.exception))

    def test_git_failure_drops_stderr_token_and_base_path(self):
        absolute_root = str(ROOT.resolve())
        completed = SimpleNamespace(
            returncode=128,
            stdout="",
            stderr=f"{absolute_root} {self.PRIVATE_SENTINEL}",
        )
        with patch("check_license_docs.subprocess.run", return_value=completed):
            with self.assertRaises(ValidationError) as raised:
                _git_diff_names(self.PRIVATE_SENTINEL)

        self.assertEqual(
            raised.exception.reason_code,
            "license_repository_diff_failed",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(absolute_root, repr(raised.exception))
        self.assertNotIn(self.PRIVATE_SENTINEL, repr(raised.exception))

    def test_git_start_failure_drops_os_error_without_chain(self):
        with patch(
            "check_license_docs.subprocess.run",
            side_effect=OSError(self.PRIVATE_SENTINEL),
        ):
            with self.assertRaises(ValidationError) as raised:
                _git_diff_names(self.PRIVATE_SENTINEL)

        self.assertEqual(
            raised.exception.reason_code,
            "license_repository_diff_failed",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(self.PRIVATE_SENTINEL, repr(raised.exception))

    def test_main_projects_known_and_unknown_failures_to_exact_public_shape(self):
        for failure in (
            ValidationError("license_input_read_failed"),
            RuntimeError(self.PRIVATE_SENTINEL),
        ):
            with self.subTest(failure=type(failure).__name__):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch("check_license_docs.validate_all", side_effect=failure),
                    patch.object(sys, "argv", ["check_license_docs.py"]),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    self.assertEqual(main(), 1)

                self.assertEqual(stdout.getvalue(), "")
                payload = json.loads(stderr.getvalue())
                self.assertEqual(
                    set(payload),
                    {"code", "detail", "correlation_id"},
                )
                self.assertEqual(payload["code"], "license_validation_failed")
                self.assertEqual(
                    payload["detail"],
                    "License documentation validation failed",
                )
                self.assertRegex(payload["correlation_id"], r"^[0-9a-f]{32}$")
                self.assertEqual(
                    stderr.getvalue(),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                )
                self.assertNotIn(self.PRIVATE_SENTINEL, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_success_output_keeps_existing_license_authority(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("check_license_docs.validate_all") as validate,
            patch.object(sys, "argv", ["check_license_docs.py"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 0)

        validate.assert_called_once_with(base=None)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            stdout.getvalue(),
            "license documentation validation: PASS\n"
            f"PolyForm: blob={POLYFORM_BLOB} sha256={POLYFORM_SHA256} "
            f"bytes={POLYFORM_SIZE} lines={POLYFORM_LINES}\n",
        )

    def test_real_cli_stderr_is_one_exact_safe_public_failure(self):
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "check_license_docs.py"),
                self.PRIVATE_SENTINEL,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        payload = json.loads(completed.stderr)
        self.assertEqual(
            payload,
            {
                "code": "license_validation_failed",
                "detail": "License documentation validation failed",
                "correlation_id": payload["correlation_id"],
            },
        )
        self.assertRegex(payload["correlation_id"], re.compile(r"^[0-9a-f]{32}$"))
        self.assertEqual(
            completed.stderr,
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        )
        self.assertNotIn(self.PRIVATE_SENTINEL, completed.stderr)
        self.assertNotIn(str(ROOT.resolve()), completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_evidence_hashes_are_independent_of_checkout_line_endings(self):
        normalized = (ROOT / "package-lock.json").read_text(encoding="utf-8")
        expected = __import__("hashlib").sha256(normalized.encode("utf-8")).hexdigest()
        self.assertEqual(_normalized_text_sha256("package-lock.json"), expected)


if __name__ == "__main__":
    unittest.main()
