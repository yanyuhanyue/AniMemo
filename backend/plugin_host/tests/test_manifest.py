from django.test import SimpleTestCase

from plugin_host.manifest import ManifestError, parse_version, validate_manifest


class ManifestV2Tests(SimpleTestCase):
    @staticmethod
    def valid_manifest(**updates):
        manifest = {
            "schemaVersion": 2, "sdkApi": 2, "id": "com.example.demo", "slug": "demo",
            "name": "Demo", "version": "1.0.0", "description": "Demo", "license": "MIT",
            "author": {"name": "Example"}, "installationMode": "user",
            "runtimes": [], "extensions": [], "permissions": [], "settings": [],
            "hooks": [], "dataPolicy": {k: False for k in ("storesPersonalData", "usesExternalNetwork", "acceptsFileUploads", "retainsDataOnDisable")},
        }
        manifest.update(updates)
        return manifest

    def test_rejects_v1(self):
        with self.assertRaises(ManifestError):
            validate_manifest({"schemaVersion": 1, "sdkApi": 1})

    def test_rejects_windows_reserved_or_overlong_slug_before_acceptance(self):
        for slug in ("con", "prn", "aux", "nul", "com1", "lpt9", "a" * 81):
            with self.subTest(slug=slug), self.assertRaises(ManifestError):
                validate_manifest(self.valid_manifest(slug=slug))

    def test_rejects_unknown_role(self):
        manifest = {
            "schemaVersion": 2, "sdkApi": 2, "id": "com.example.demo", "slug": "demo",
            "name": "Demo", "version": "1.0.0", "description": "Demo",
            "runtimes": [], "extensions": [], "permissions": [{"code": "demo.run", "roles": ["admin"]}],
            "hooks": [], "dataPolicy": {k: False for k in ("storesPersonalData", "usesExternalNetwork", "acceptsFileUploads", "retainsDataOnDisable")},
        }
        with self.assertRaises(ManifestError):
            validate_manifest(manifest)

    def test_user_plugin_rejects_system_scoped_hook(self):
        with self.assertRaises(ManifestError):
            validate_manifest(self.valid_manifest(hooks=["registration.before_request"]))

    def test_system_plugin_can_declare_global_lifecycle_hook(self):
        manifest = self.valid_manifest(
            installationMode="system",
            hooks=["registration.before_request", "user.after_created"],
        )
        self.assertEqual(validate_manifest(manifest), manifest)

    def test_plugins_without_integrations_remain_valid(self):
        manifest = self.valid_manifest()
        self.assertNotIn("integrations", validate_manifest(manifest))

    def test_data_compatibility_floor_is_optional_and_bounded_by_version(self):
        manifest = self.valid_manifest(
            version="2.0.0",
            dataCompatibility={"rollbackFloor": "1.5.0"},
        )
        self.assertEqual(validate_manifest(manifest)["dataCompatibility"]["rollbackFloor"], "1.5.0")
        with self.assertRaisesRegex(ManifestError, "不能高于当前版本"):
            validate_manifest(self.valid_manifest(
                version="2.0.0",
                dataCompatibility={"rollbackFloor": "3.0.0"},
            ))

    def test_version_and_rollback_floor_share_the_database_width_boundary(self):
        version_40 = "1.0.0-rc." + ("1" * 31)
        version_41 = "1.0.0-rc." + ("1" * 32)

        self.assertEqual(
            validate_manifest(self.valid_manifest(version=version_40))["version"],
            version_40,
        )
        with self.assertRaises(ManifestError):
            validate_manifest(self.valid_manifest(version=version_41))
        self.assertEqual(
            validate_manifest(
                self.valid_manifest(
                    version="2.0.0",
                    dataCompatibility={"rollbackFloor": version_40},
                )
            )["dataCompatibility"]["rollbackFloor"],
            version_40,
        )
        with self.assertRaises(ManifestError):
            validate_manifest(
                self.valid_manifest(
                    version="2.0.0",
                    dataCompatibility={"rollbackFloor": version_41},
                )
            )

    def test_prerelease_identifiers_are_canonical_semver_segments(self):
        valid_versions = (
            "1.0.0-RC.1",
            "1.0.0-x.7.z.92",
            "1.0.0-a-b",
            "1.0.0-0A",
        )
        for version in valid_versions:
            with self.subTest(valid=version):
                self.assertEqual(
                    validate_manifest(self.valid_manifest(version=version))["version"],
                    version,
                )
                self.assertIsNotNone(parse_version(version))
        for version in ("1.0.0-rc.", "1.0.0-a..b", "1.0.0-01"):
            with self.subTest(version=version), self.assertRaises(ManifestError):
                validate_manifest(self.valid_manifest(version=version))
            with self.subTest(rollback_floor=version), self.assertRaises(ManifestError):
                validate_manifest(
                    self.valid_manifest(
                        version="2.0.0",
                        dataCompatibility={"rollbackFloor": version},
                    )
                )

    def test_semver_comparison_uses_semver_not_pep440_precedence(self):
        ordered = (
            "1.0.0-1",
            "1.0.0-0A",
            "1.0.0-a-b",
            "1.0.0-x.7.z.92",
            "1.0.0",
        )

        self.assertEqual(
            sorted(ordered, key=parse_version),
            list(ordered),
        )

    def test_integration_declarations_are_optional_and_provider_neutral(self):
        manifest = self.valid_manifest(
            extensions=["integration.actions", "integration.events"],
            integrations={
                "actions": [{"name": "import-text", "description": "Import text."}],
                "events": [{"name": "import-completed"}],
            },
        )
        self.assertEqual(validate_manifest(manifest), manifest)

    def test_integration_action_requires_capability_extension(self):
        with self.assertRaises(ManifestError):
            validate_manifest(
                self.valid_manifest(integrations={"actions": [{"name": "run"}]})
            )

    def test_integration_names_are_conservative_and_unique(self):
        with self.assertRaises(ManifestError):
            validate_manifest(
                self.valid_manifest(
                    extensions=["integration.actions"],
                    integrations={"actions": [{"name": "Bad.Name"}]},
                )
            )
        with self.assertRaises(ManifestError):
            validate_manifest(
                self.valid_manifest(
                    extensions=["integration.events"],
                    integrations={"events": [{"name": "done"}, {"name": "done"}]},
                )
            )

    def test_core_capabilities_require_backend_and_are_known_unique(self):
        manifest = self.valid_manifest(
            runtimes=["backend"],
            backend={"entry": "backend/plugin.py"},
            extensions=["backend.api"],
            coreCapabilities=["journal", "analytics"],
        )
        self.assertEqual(validate_manifest(manifest)["coreCapabilities"], ["journal", "analytics"])
        with self.assertRaisesRegex(ManifestError, "只能由 backend"):
            validate_manifest(self.valid_manifest(coreCapabilities=["journal"]))
        with self.assertRaises(ManifestError):
            validate_manifest(self.valid_manifest(
                runtimes=["backend"],
                backend={"entry": "backend/plugin.py"},
                extensions=["backend.api"],
                coreCapabilities=["journal", "journal"],
            ))
        with self.assertRaises(ManifestError):
            validate_manifest(self.valid_manifest(
                runtimes=["backend"],
                backend={"entry": "backend/plugin.py"},
                extensions=["backend.api"],
                coreCapabilities=["unknown"],
            ))
        with self.assertRaises(ManifestError):
            validate_manifest(self.valid_manifest(coreCapabilities=[{"name": "journal"}]))
        with self.assertRaisesRegex(ManifestError, "settings definition"):
            validate_manifest(self.valid_manifest(settings=[{"key": "enabled", "scope": "user"}]))

    def test_backend_runtime_rejects_every_noncanonical_entry_path(self):
        for entry in ("../outside.py", "/tmp/outside.py", "backend/../outside.py"):
            with self.subTest(entry=entry), self.assertRaisesRegex(ManifestError, "固定 backend/plugin.py"):
                validate_manifest(
                    self.valid_manifest(
                        runtimes=["backend"],
                        backend={"entry": entry},
                        extensions=["backend.api"],
                    )
                )
