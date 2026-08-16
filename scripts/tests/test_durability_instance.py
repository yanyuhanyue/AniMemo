from __future__ import annotations

import copy
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from durability.instance import (
    MANAGED_CONFIG_PATH,
    LocalLocatorStore,
    LocatorError,
    load_instance_locator,
    parse_instance_locator,
    publish_instance_locator,
    replace_instance_locator,
)

DIGEST = "sha256:" + "a" * 64


def locator_payload() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "instanceId": "abcdefab-1234-5678-9234-567812345678",
        "appRoot": "/opt/animemo",
        "dataRoot": "/data/animemo",
        "deploymentProfile": "v1.1-standard",
        "listen": {"host": "127.0.0.1", "port": 8088},
        "publicOrigin": "https://animemo.example",
        "managedConfigPath": str(MANAGED_CONFIG_PATH),
        "configRevision": "11111111-1111-4111-8111-111111111111",
        "releaseIdentity": {
            "version": "v1.1.0-rc.1",
            "channel": "rc",
            "commit": "a" * 40,
            "manifestDigest": DIGEST,
            "apiDigest": DIGEST,
            "webDigest": DIGEST,
        },
    }


class CanonicalLocatorParserTests(unittest.TestCase):
    def test_only_exact_canonical_scalar_identities_are_accepted(self):
        locator = parse_instance_locator(locator_payload())

        self.assertEqual(
            str(locator.managed_config_path), "/data/animemo/config/animemo.json"
        )
        self.assertEqual(locator.listen.host, "127.0.0.1")
        self.assertEqual(locator.public_origin, "https://animemo.example")

        cases = [
            ("schemaVersion", True, "LOCATOR_SCHEMA_UNSUPPORTED"),
            (
                "instanceId",
                "ABCDEFAB-1234-5678-9234-567812345678",
                "LOCATOR_INSTANCE_ID_INVALID",
            ),
            (
                "configRevision",
                "11111111-1111-4111-A111-111111111111",
                "LOCATOR_CONFIG_REVISION_INVALID",
            ),
            (
                "managedConfigPath",
                "/data/animemo/config/other.json",
                "LOCATOR_CONFIG_PATH_INVALID",
            ),
            (
                "publicOrigin",
                "https://ANIMEMO.example",
                "LOCATOR_PUBLIC_ORIGIN_INVALID",
            ),
        ]
        for field, value, code in cases:
            payload = copy.deepcopy(locator_payload())
            payload[field] = value
            with self.subTest(field=field):
                with self.assertRaises(LocatorError) as raised:
                    parse_instance_locator(payload)
                self.assertEqual(raised.exception.code, code)


class CanonicalLocatorStorageTests(unittest.TestCase):
    def test_initial_publication_is_private_no_clobber_and_replacement_is_cas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instance.json"
            store = LocalLocatorStore.testing(path)
            locator = parse_instance_locator(locator_payload())

            initial = publish_instance_locator(locator, store=store)

            self.assertEqual(load_instance_locator(store), locator)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(LocatorError) as duplicate:
                publish_instance_locator(locator, store=store)
            self.assertEqual(duplicate.exception.code, "LOCATOR_ALREADY_EXISTS")

            changed_payload = copy.deepcopy(locator_payload())
            changed_payload["publicOrigin"] = "https://changed.example"
            changed = parse_instance_locator(changed_payload)
            replaced = replace_instance_locator(
                changed,
                expected_digest=initial.digest,
                store=store,
            )

            self.assertNotEqual(replaced.digest, initial.digest)
            self.assertEqual(load_instance_locator(store), changed)
            with self.assertRaises(LocatorError) as stale:
                replace_instance_locator(
                    locator,
                    expected_digest=initial.digest,
                    store=store,
                )
            self.assertEqual(stale.exception.code, "LOCATOR_CONCURRENT_MODIFICATION")

    def test_concurrent_replacements_serialize_and_only_one_cas_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instance.json"
            initial_store = LocalLocatorStore.testing(path)
            initial_locator = parse_instance_locator(locator_payload())
            initial = publish_instance_locator(initial_locator, store=initial_store)
            candidates = []
            for origin in ("https://first.example", "https://second.example"):
                payload = copy.deepcopy(locator_payload())
                payload["publicOrigin"] = origin
                candidates.append(parse_instance_locator(payload))
            start = threading.Barrier(3)

            def replace(candidate):
                store = LocalLocatorStore.testing(path)
                start.wait()
                try:
                    snapshot = replace_instance_locator(
                        candidate,
                        expected_digest=initial.digest,
                        store=store,
                    )
                    return "REPLACED", snapshot.locator.public_origin
                except LocatorError as error:
                    return error.code, None

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(replace, candidate) for candidate in candidates]
                start.wait()
                outcomes = [future.result(timeout=5) for future in futures]

            self.assertEqual(
                sorted(code for code, _ in outcomes),
                ["LOCATOR_CONCURRENT_MODIFICATION", "REPLACED"],
            )
            winner = next(origin for code, origin in outcomes if code == "REPLACED")
            self.assertEqual(load_instance_locator(initial_store).public_origin, winner)


if __name__ == "__main__":
    unittest.main()
