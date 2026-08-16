from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from durability.managed_config import (
    DERIVED_ENV_FIELDS,
    MANAGED_CONFIG_SCHEMA,
    MANAGED_ENV_PATH,
    NON_SECRET_FIELDS,
    SECRET_ENV_FIELDS,
    SECRET_FIELDS,
    DirectAccessConfig,
    ListenConfig,
    LocalManagedConfigStore,
    ManagedConfigError,
    canonical_managed_config_bytes,
    canonical_managed_env_bytes,
    derive_runtime_environment,
    parse_managed_config,
    plan_config_change,
)

DJANGO_SECRET = "django-secret-marker-" + "x" * 64
DATABASE_SECRET = "database-secret-marker-" + "y" * 32
CREDENTIAL_KEY = "a0DtqkhZwqytmU2lcF-2oUKmjlyqPIrJsU5O_T6d3Io="
REVISION = "12345678-1234-4678-9234-567812345678"
NEXT_REVISION = "87654321-4321-4678-9234-567812345678"
INSTANCE_ID = "22345678-1234-4678-9234-567812345678"


def payload() -> dict[str, object]:
    return {
        "schema": "animemo.managed-config/v1",
        "instanceId": INSTANCE_ID,
        "configRevision": REVISION,
        "deploymentProfile": "v1.1-standard",
        "listen": {"host": "127.0.0.1", "port": 8088},
        "publicOrigin": "https://animemo.example",
        "directAccess": {
            "allowNonLoopback": False,
            "allowHttp": False,
            "warningAcknowledged": False,
        },
        "trustedOrigins": {
            "allowedHosts": ["assets.animemo.example"],
            "cors": ["https://console.animemo.example"],
            "csrf": ["https://console.animemo.example"],
        },
        "database": {
            "name": "animemo",
            "user": "animemo",
            "password": DATABASE_SECRET,
        },
        "redis": {"url": "redis://redis:6379/0"},
        "application": {
            "djangoSecretKey": DJANGO_SECRET,
            "credentialEncryptionKey": CREDENTIAL_KEY,
            "mediaPublicOrigin": None,
            "trustedProxyIps": ["127.0.0.1/32", "172.28.0.0/16"],
        },
        "integrations": {
            "bangumiOAuthClientId": "",
            "bangumiOAuthClientSecret": "",
            "resendApiKey": "",
        },
    }


def encoded(value: dict[str, object] | None = None) -> bytes:
    return json.dumps(value or payload(), ensure_ascii=False).encode("utf-8")


class ManagedConfigSchemaTests(unittest.TestCase):
    def test_exact_json_round_trip_and_field_classes_are_frozen(self) -> None:
        config = parse_managed_config(encoded())

        self.assertEqual(config.schema, MANAGED_CONFIG_SCHEMA)
        self.assertEqual(config.instance_id, INSTANCE_ID)
        self.assertEqual(config.public_origin, "https://animemo.example")
        self.assertEqual(config.listen, ListenConfig("127.0.0.1", 8088))
        self.assertEqual(
            parse_managed_config(canonical_managed_config_bytes(config)), config
        )
        self.assertIn("database.password", SECRET_FIELDS)
        self.assertIn("publicOrigin", NON_SECRET_FIELDS)
        self.assertIn("instanceId", NON_SECRET_FIELDS)
        self.assertIn("DATABASE_URL", DERIVED_ENV_FIELDS)
        self.assertIn("DATABASE_URL", SECRET_ENV_FIELDS)

    def test_unknown_duplicate_malformed_and_release_identity_fail_closed(self) -> None:
        unknown = payload()
        unknown["legacyEnvPath"] = "/opt/animemo/.env.production"
        release_identity = payload()
        release_identity["images"] = {"api": "mutable", "web": "mutable"}
        cases = (
            encoded(unknown),
            encoded(release_identity),
            b'{"schema":"animemo.managed-config/v1","schema":"other"}',
            b'{"schema":NaN}',
        )
        for raw in cases:
            with self.subTest(raw=raw[:40]), self.assertRaises(ManagedConfigError):
                parse_managed_config(raw)

    def test_origin_listen_and_direct_access_require_explicit_opt_in(self) -> None:
        wildcard = payload()
        wildcard["publicOrigin"] = "https://*.example.com"
        noncanonical = payload()
        noncanonical["publicOrigin"] = "https://AniMemo.Example/"
        exposed = payload()
        exposed["listen"] = {"host": "0.0.0.0", "port": 8088}
        insecure = payload()
        insecure["publicOrigin"] = "http://direct.example"
        for candidate in (wildcard, noncanonical, exposed, insecure):
            with (
                self.subTest(candidate=candidate),
                self.assertRaises(ManagedConfigError),
            ):
                parse_managed_config(encoded(candidate))

        explicit = payload()
        explicit["listen"] = {"host": "0.0.0.0", "port": 18088}
        explicit["publicOrigin"] = "http://direct.example:18088"
        explicit["directAccess"] = {
            "allowNonLoopback": True,
            "allowHttp": True,
            "warningAcknowledged": True,
        }
        config = parse_managed_config(encoded(explicit))
        self.assertFalse(config.listen.is_loopback)

    def test_runtime_env_is_derived_and_contains_no_second_identity_authority(
        self,
    ) -> None:
        config = parse_managed_config(encoded())
        environment = derive_runtime_environment(config)
        rendered = canonical_managed_env_bytes(config).decode("utf-8")

        self.assertEqual(environment["ANIMEMO_PUBLIC_ORIGIN"], config.public_origin)
        self.assertEqual(environment["ANIMEMO_LISTEN_HOST"], "127.0.0.1")
        self.assertEqual(environment["ANIMEMO_LISTEN_PORT"], "8088")
        self.assertIn("animemo.example", environment["ALLOWED_HOSTS"])
        self.assertNotIn("FRONTEND_URL", environment)
        self.assertNotIn("BANGUMI_OAUTH_REDIRECT_URI", environment)
        self.assertIn(DATABASE_SECRET, rendered)
        self.assertEqual(str(MANAGED_ENV_PATH), "/run/animemo-updater/managed.env")

    def test_change_plan_and_repr_do_not_disclose_secrets(self) -> None:
        current = parse_managed_config(encoded())
        plan, proposed = plan_config_change(
            current,
            next_revision=NEXT_REVISION,
            public_origin="https://new.example",
            listen=ListenConfig("0.0.0.0", 18088),
            direct_access=DirectAccessConfig(True, False, True),
        )
        rendered = json.dumps(plan.as_dict(), ensure_ascii=False) + repr(plan)

        self.assertNotIn(DJANGO_SECRET, rendered)
        self.assertNotIn(DATABASE_SECRET, rendered)
        self.assertEqual(proposed.database.password, DATABASE_SECRET)
        self.assertEqual(plan.instance_id, INSTANCE_ID)
        self.assertEqual(plan.warnings, ("DIRECT_NETWORK_EXPOSURE",))
        self.assertEqual(
            plan.changed_fields, ("publicOrigin", "listen", "directAccess")
        )
        self.assertRegex(plan.plan_digest, r"^sha256:[0-9a-f]{64}$")


class ManagedConfigAtomicStoreTests(unittest.TestCase):
    def test_authority_and_runtime_env_are_private_atomic_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            runtime_root = root / "runtime"
            config_root.mkdir(mode=0o700)
            runtime_root.mkdir(mode=0o750)
            store = LocalManagedConfigStore(
                config_root=config_root, runtime_root=runtime_root
            )
            config = parse_managed_config(encoded())

            store.write(config, expected_revision=None, must_not_exist=True)
            runtime_path = store.rebuild_runtime_env(expected_revision=REVISION)

            self.assertEqual(store.read(), config)
            self.assertEqual(runtime_path, runtime_root / "managed.env")
            self.assertEqual(
                (runtime_root / "managed.env").read_bytes(),
                canonical_managed_env_bytes(config),
            )
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE((config_root / "animemo.json").stat().st_mode),
                    0o600,
                )
                self.assertEqual(
                    stat.S_IMODE((runtime_root / "managed.env").stat().st_mode),
                    0o600,
                )

    def test_stale_revision_and_link_target_fail_without_replacing_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config"
            runtime_root = root / "runtime"
            config_root.mkdir(mode=0o700)
            runtime_root.mkdir(mode=0o750)
            store = LocalManagedConfigStore(
                config_root=config_root, runtime_root=runtime_root
            )
            current = parse_managed_config(encoded())
            store.write(current, expected_revision=None, must_not_exist=True)
            _, proposed = plan_config_change(
                current,
                next_revision=NEXT_REVISION,
                public_origin="https://new.example",
            )

            with self.assertRaisesRegex(ManagedConfigError, "CONFIG_STALE"):
                store.write(proposed, expected_revision=NEXT_REVISION)
            self.assertEqual(store.read(), current)

            if hasattr(os, "link"):
                authority = config_root / "animemo.json"
                outside = root / "outside.json"
                authority.replace(outside)
                try:
                    os.link(outside, authority)
                except OSError:
                    return
                with self.assertRaises(ManagedConfigError):
                    store.read()


if __name__ == "__main__":
    unittest.main()
