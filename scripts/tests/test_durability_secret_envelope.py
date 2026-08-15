from __future__ import annotations

import io
import json
import logging
import traceback
import unittest
from unittest.mock import patch

from durability.canonical import canonical_json_bytes
from durability.secret_envelope import (
    ENVELOPE_IDENTITY,
    OneTimeKey,
    Passphrase,
    SecretEntry,
    SecretEnvelope,
    SecretEnvelopeCorruptError,
    SecretEnvelopeInputError,
    SecretEnvelopeOperationalError,
    SecretEnvelopeUnsupportedError,
    create_secret_envelope,
    open_secret_envelope,
)

ARTIFACT_ID = "11111111-1111-4111-8111-111111111111"
INSTANCE_ID = "22222222-2222-4222-8222-222222222222"


def binding_record(
    artifact_type: str,
    *,
    artifact_id: str = ARTIFACT_ID,
    instance_id: str = INSTANCE_ID,
    database_digest: str = "sha256:" + "ab" * 32,
):
    common = {
        "databasePayloadDigestRoot": database_digest,
        "filesystemPayloadDigestRoot": "sha256:" + "cd" * 32,
        "releaseIdentity": {
            "commit": "0" * 40,
            "version": "v1.1.0",
        },
        "secretProfileIdentity": ENVELOPE_IDENTITY,
    }
    if artifact_type == "backup":
        return {
            **common,
            "backupId": artifact_id,
            "format": "animemo-instance-backup",
            "schemaVersion": 1,
            "source": {"instanceId": instance_id},
        }
    return {
        **common,
        "bundleId": artifact_id,
        "format": "animemo-migration-bundle",
        "formatVersion": 1,
        "instanceId": instance_id,
    }


class SecretEnvelopeTests(unittest.TestCase):
    def _one_time_envelope(self):
        external_key = OneTimeKey.from_bytes(bytes(range(32)))
        envelope = create_secret_envelope(
            external_secret=external_key,
            artifact_type="migration-bundle",
            artifact_id=ARTIFACT_ID,
            artifact_binding_record=binding_record("migration-bundle"),
            source_instance_id=INSTANCE_ID,
            secret_entries=(
                SecretEntry.preserve(
                    "CREDENTIAL_ENCRYPTION_KEY",
                    b"credential-key-with-exact-bytes\x00\xff",
                ),
            ),
        )
        return external_key, envelope

    def test_one_time_key_round_trip_preserves_exact_secret_bytes(self):
        external_key = OneTimeKey.from_bytes(bytes(range(32)))
        credential_key = b"credential-key-with-exact-bytes\x00\xff"

        envelope = create_secret_envelope(
            external_secret=external_key,
            artifact_type="migration-bundle",
            artifact_id=ARTIFACT_ID,
            artifact_binding_record=binding_record("migration-bundle"),
            source_instance_id=INSTANCE_ID,
            secret_entries=(
                SecretEntry.preserve("CREDENTIAL_ENCRYPTION_KEY", credential_key),
            ),
        )
        payload = open_secret_envelope(
            envelope,
            external_secret=external_key,
            expected_artifact_type="migration-bundle",
            expected_artifact_id=ARTIFACT_ID,
            expected_artifact_binding_record=binding_record("migration-bundle"),
            expected_source_instance_id=INSTANCE_ID,
        )

        self.assertEqual(payload.source_instance_id, INSTANCE_ID)
        self.assertEqual(
            payload.get_secret("CREDENTIAL_ENCRYPTION_KEY").reveal(),
            credential_key,
        )

    def test_authentication_failures_have_one_redacted_public_result(self):
        external_key, envelope = self._one_time_envelope()
        root = json.loads(envelope.to_bytes())
        variants = []

        wrong_ciphertext = dict(root)
        wrong_ciphertext["ciphertext"] = "!"
        variants.append(
            (
                SecretEnvelope.from_bytes(canonical_json_bytes(wrong_ciphertext)),
                external_key,
                ARTIFACT_ID,
                binding_record("migration-bundle"),
                INSTANCE_ID,
            )
        )

        wrong_nonce = dict(root)
        wrong_nonce["aead"] = dict(root["aead"])
        wrong_nonce["aead"]["nonce"] = "!"
        variants.append(
            (
                SecretEnvelope.from_bytes(canonical_json_bytes(wrong_nonce)),
                external_key,
                ARTIFACT_ID,
                binding_record("migration-bundle"),
                INSTANCE_ID,
            )
        )

        variants.append(
            (
                envelope,
                OneTimeKey.from_bytes(b"z" * 32),
                ARTIFACT_ID,
                binding_record("migration-bundle"),
                INSTANCE_ID,
            )
        )
        variants.append(
            (
                envelope,
                external_key,
                "33333333-3333-4333-8333-333333333333",
                binding_record(
                    "migration-bundle",
                    artifact_id="33333333-3333-4333-8333-333333333333",
                ),
                INSTANCE_ID,
            )
        )
        variants.append(
            (
                envelope,
                external_key,
                ARTIFACT_ID,
                binding_record(
                    "migration-bundle", database_digest="sha256:" + "ef" * 32
                ),
                INSTANCE_ID,
            )
        )
        variants.append(
            (
                envelope,
                external_key,
                ARTIFACT_ID,
                binding_record(
                    "migration-bundle",
                    instance_id="44444444-4444-4444-8444-444444444444",
                ),
                "44444444-4444-4444-8444-444444444444",
            )
        )

        for candidate, candidate_key, artifact_id, record, instance_id in variants:
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(SecretEnvelopeCorruptError) as caught:
                    open_secret_envelope(
                        candidate,
                        external_secret=candidate_key,
                        expected_artifact_type="migration-bundle",
                        expected_artifact_id=artifact_id,
                        expected_artifact_binding_record=record,
                        expected_source_instance_id=instance_id,
                    )
                self.assertEqual(caught.exception.code, "ENVELOPE_AUTHENTICATION_FAILED")
                self.assertEqual(caught.exception.compatibility_outcome, "CORRUPT")
                self.assertEqual(str(caught.exception), "ENVELOPE_AUTHENTICATION_FAILED")

    def test_binding_record_is_canonical_and_rejects_circular_fields(self):
        circular_record = binding_record("migration-bundle")
        circular_record["finalManifestChecksum"] = "sha256:" + "ef" * 32
        with self.assertRaises(SecretEnvelopeInputError) as caught:
            create_secret_envelope(
                external_secret=OneTimeKey.from_bytes(bytes(range(32))),
                artifact_type="migration-bundle",
                artifact_id=ARTIFACT_ID,
                artifact_binding_record=circular_record,
                source_instance_id=INSTANCE_ID,
                secret_entries=(
                    SecretEntry.preserve(
                        "CREDENTIAL_ENCRYPTION_KEY", b"fixture-credential-key"
                    ),
                ),
            )
        self.assertEqual(caught.exception.code, "ARTIFACT_BINDING_INVALID")

    def test_passphrase_suite_has_a_frozen_canonical_known_answer(self):
        expected = (
            b'{"aead":{"algorithm":"AES-256-GCM","keyBytes":32,'
            b'"nonce":"EBESExQVFhcYGRob","nonceBytes":12,'
            b'"nonceEncoding":"base64url","tagBytes":16},"binding":'
            b'{"artifactBindingDigest":"sha256:a91bdc2086569de550c035d31d278880'
            b'47c9345fd248c8d50b2fdf28870cc0f8","artifactId":'
            b'"11111111-1111-4111-8111-111111111111","artifactType":'
            b'"migration-bundle"},"ciphertext":"VWtDGaD0GlEADjqExNo-ZKx3LiKgSDAcQDTg'
            b'RuT7sZpjdlzRZRiSjErW1o5AEkw0I3tTzsOE0F0LaU2tJ6R02Jq85u0cij8be0_1l8oD5m16'
            b'o506GcJfsGrvwaAf2pWtRs3nNVdIVh3lIGTnIqZepl_O7srtR9ZqZvFTm3_SOKM0OG9q3uQf'
            b'CDbxqoPNCnE420cOfHAzryn66rIJ2euXxIKk3EX_gqtprWl1Qbqm3EG719_sxA13jOVtMnxor'
            b'LOkOxX1AEGLwKTe59Joh7mjQseNKDm0gFlkMWn6SPXMMPhICkvG6UrjuYR6hdBDXhAkaqfiI'
            b'SbBvq0kpSU-P6SP2gk_NcKzXpfokztpSz_vIMYe0efJ1CesIWrRte9mzghdCYD5ft8fDmwQI'
            b'YFYA5bNkONA7HtjEbZ8-zASDge8c_ipwIHWm3PWqEbN817cBY8gvMeqzRq9RWnv8RwTu0ODI'
            b'MxvNR873CniAFZThcDowovqQ048Mt-tyyu11v4TZERo2BKcJab75Wk30wCvQkS2m3CGeEdDU'
            b'0nbpIcfE44ptvfQNN97KjVBn9znI3xdKPcO8gkVk_f5wiW-9uJT7fetcFkbJ-DX6Gb_JvcoJ'
            b'YUz5zZeBH-BAzVTGAdrP0Bca0rfVG-M--9TeqrS9iok-TyiAIedzS-zGjsTIiOYGjHlD7QcNV'
            b'cDZw","ciphertextEncoding":'
            b'"base64url","format":'
            b'"animemo.migration-secret-envelope","kdf":{"algorithm":'
            b'"Argon2id","iterations":3,"memoryKiB":65536,"outputBytes":32,'
            b'"parallelism":4,"salt":"AAECAwQFBgcICQoLDA0ODw",'
            b'"saltEncoding":"base64url"},"mode":"passphrase",'
            b'"schemaVersion":1,"suiteId":'
            b'"argon2id-m65536-t3-p4-aes-256-gcm-v1"}'
        )
        passphrase = Passphrase.from_text("correct horse battery staple")
        with patch(
            "durability.secret_envelope.os.urandom",
            side_effect=(bytes(range(16)), bytes(range(16, 28))),
        ):
            envelope = create_secret_envelope(
                external_secret=passphrase,
                artifact_type="migration-bundle",
                artifact_id=ARTIFACT_ID,
                artifact_binding_record=binding_record("migration-bundle"),
                source_instance_id=INSTANCE_ID,
                secret_entries=(
                    SecretEntry.preserve(
                        "CREDENTIAL_ENCRYPTION_KEY", b"fixture-credential-key"
                    ),
                ),
            )

        self.assertEqual(envelope.to_bytes(), expected)
        opened = open_secret_envelope(
            SecretEnvelope.from_bytes(expected),
            external_secret=passphrase,
            expected_artifact_type="migration-bundle",
            expected_artifact_id=ARTIFACT_ID,
            expected_artifact_binding_record=binding_record("migration-bundle"),
            expected_source_instance_id=INSTANCE_ID,
        )
        self.assertEqual(
            opened.get_secret("CREDENTIAL_ENCRYPTION_KEY").reveal(),
            b"fixture-credential-key",
        )
        with self.assertRaises(SecretEnvelopeCorruptError) as wrong_passphrase:
            open_secret_envelope(
                SecretEnvelope.from_bytes(expected),
                external_secret=Passphrase.from_text("different migration passphrase"),
                expected_artifact_type="migration-bundle",
                expected_artifact_id=ARTIFACT_ID,
                expected_artifact_binding_record=binding_record("migration-bundle"),
                expected_source_instance_id=INSTANCE_ID,
            )
        self.assertEqual(
            wrong_passphrase.exception.code, "ENVELOPE_AUTHENTICATION_FAILED"
        )

    def test_unknown_valid_version_and_suite_are_unsupported(self):
        _, envelope = self._one_time_envelope()
        root = json.loads(envelope.to_bytes())
        cases = (
            ("schemaVersion", 2, "ENVELOPE_VERSION_UNSUPPORTED"),
            ("suiteId", "future-reviewed-suite-v2", "ENVELOPE_SUITE_UNSUPPORTED"),
        )
        for field, value, expected_code in cases:
            candidate = dict(root)
            candidate[field] = value
            with self.subTest(field=field):
                with self.assertRaises(SecretEnvelopeUnsupportedError) as caught:
                    open_secret_envelope(
                        SecretEnvelope.from_bytes(canonical_json_bytes(candidate)),
                        external_secret=OneTimeKey.from_bytes(bytes(range(32))),
                        expected_artifact_type="migration-bundle",
                        expected_artifact_id=ARTIFACT_ID,
                        expected_artifact_binding_record=binding_record(
                            "migration-bundle"
                        ),
                        expected_source_instance_id=INSTANCE_ID,
                    )
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(caught.exception.compatibility_outcome, "UNSUPPORTED")

    def test_allowlist_classification_handling_and_repr_are_fail_closed(self):
        external_key = OneTimeKey.from_bytes(bytes(range(32)))
        reconfigure = SecretEntry.reconfigure("DJANGO_SECRET_KEY")
        envelope = create_secret_envelope(
            external_secret=external_key,
            artifact_type="backup",
            artifact_id=ARTIFACT_ID,
            artifact_binding_record=binding_record("backup"),
            source_instance_id=INSTANCE_ID,
            secret_entries=(
                SecretEntry.preserve("CREDENTIAL_ENCRYPTION_KEY", b"independent-key"),
                reconfigure,
            ),
        )
        opened = open_secret_envelope(
            envelope,
            external_secret=external_key,
            expected_artifact_type="backup",
            expected_artifact_id=ARTIFACT_ID,
            expected_artifact_binding_record=binding_record("backup"),
            expected_source_instance_id=INSTANCE_ID,
        )
        self.assertEqual(
            {
                entry.name: (entry.classification, entry.handling)
                for entry in opened.entries
            },
            {
                "CREDENTIAL_ENCRYPTION_KEY": ("PRESERVE", "PRESERVE"),
                "DJANGO_SECRET_KEY": (
                    "PRESERVE_OR_EXPLICIT_RECONFIGURE",
                    "RECONFIGURE",
                ),
            },
        )
        self.assertNotIn(b"DJANGO_SECRET_KEY", envelope.to_bytes())
        self.assertNotIn(b"CREDENTIAL_ENCRYPTION_KEY", envelope.to_bytes())
        self.assertNotIn(b"independent-key", envelope.to_bytes())
        self.assertEqual(repr(external_key), "<OneTimeKey redacted>")
        self.assertEqual(repr(reconfigure), "<SecretEntry redacted>")
        self.assertEqual(repr(envelope), "<SecretEnvelope redacted>")
        self.assertEqual(repr(opened), "<OpenedSecretPayload redacted>")

        for target_local_name in (
            "POSTGRES_PASSWORD",
            "REDIS_URL",
            "GITHUB_TOKEN",
            "GHCR_TOKEN",
        ):
            with self.subTest(target_local_name=target_local_name):
                with self.assertRaises(SecretEnvelopeInputError) as unclassified:
                    SecretEntry.preserve(target_local_name, b"target-local")
                self.assertEqual(unclassified.exception.code, "UNCLASSIFIED_SECRET")

        with self.assertRaises(SecretEnvelopeInputError) as self_encryption:
            create_secret_envelope(
                external_secret=external_key,
                artifact_type="backup",
                artifact_id=ARTIFACT_ID,
                artifact_binding_record=binding_record("backup"),
                source_instance_id=INSTANCE_ID,
                secret_entries=(
                    SecretEntry.preserve(
                        "CREDENTIAL_ENCRYPTION_KEY", external_key.export()
                    ),
                ),
            )
        self.assertEqual(
            self_encryption.exception.code, "EXTERNAL_SECRET_REUSE_FORBIDDEN"
        )

    def test_kdf_metadata_is_bounded_before_argon2_runs(self):
        passphrase = Passphrase.from_text("correct horse battery staple")
        with patch(
            "durability.secret_envelope.os.urandom",
            side_effect=(bytes(range(16)), bytes(range(16, 28))),
        ):
            envelope = create_secret_envelope(
                external_secret=passphrase,
                artifact_type="migration-bundle",
                artifact_id=ARTIFACT_ID,
                artifact_binding_record=binding_record("migration-bundle"),
                source_instance_id=INSTANCE_ID,
                secret_entries=(
                    SecretEntry.preserve(
                        "CREDENTIAL_ENCRYPTION_KEY", b"fixture-credential-key"
                    ),
                ),
            )
        root = json.loads(envelope.to_bytes())
        root["kdf"]["memoryKiB"] = 2**40
        with (
            patch("durability.secret_envelope.Argon2id") as argon2,
            self.assertRaises(SecretEnvelopeCorruptError) as caught,
        ):
            open_secret_envelope(
                SecretEnvelope.from_bytes(canonical_json_bytes(root)),
                external_secret=passphrase,
                expected_artifact_type="migration-bundle",
                expected_artifact_id=ARTIFACT_ID,
                expected_artifact_binding_record=binding_record("migration-bundle"),
                expected_source_instance_id=INSTANCE_ID,
            )
        argon2.assert_not_called()
        self.assertEqual(caught.exception.code, "ENVELOPE_AUTHENTICATION_FAILED")

    def test_crypto_resource_failure_is_operational_not_corrupt(self):
        passphrase = Passphrase.from_text("correct horse battery staple")
        with patch(
            "durability.secret_envelope.os.urandom",
            side_effect=(bytes(range(16)), bytes(range(16, 28))),
        ):
            envelope = create_secret_envelope(
                external_secret=passphrase,
                artifact_type="migration-bundle",
                artifact_id=ARTIFACT_ID,
                artifact_binding_record=binding_record("migration-bundle"),
                source_instance_id=INSTANCE_ID,
                secret_entries=(
                    SecretEntry.preserve(
                        "CREDENTIAL_ENCRYPTION_KEY", b"fixture-credential-key"
                    ),
                ),
            )
        with (
            patch("durability.secret_envelope.Argon2id", side_effect=MemoryError),
            self.assertRaises(SecretEnvelopeOperationalError) as caught,
        ):
            open_secret_envelope(
                envelope,
                external_secret=passphrase,
                expected_artifact_type="migration-bundle",
                expected_artifact_id=ARTIFACT_ID,
                expected_artifact_binding_record=binding_record("migration-bundle"),
                expected_source_instance_id=INSTANCE_ID,
            )
        self.assertEqual(caught.exception.code, "ENVELOPE_CRYPTO_UNAVAILABLE")
        self.assertIsNone(caught.exception.compatibility_outcome)

    def test_validation_exception_log_and_debug_paths_are_redacted(self):
        sentinel = b"sensitive-fixture-never-render"
        entry = SecretEntry.preserve("DJANGO_SECRET_KEY", sentinel)
        try:
            create_secret_envelope(
                external_secret=OneTimeKey.from_bytes(bytes(range(32))),
                artifact_type="backup",
                artifact_id=ARTIFACT_ID,
                artifact_binding_record=binding_record("backup"),
                source_instance_id=INSTANCE_ID,
                secret_entries=(entry,),
            )
        except SecretEnvelopeInputError as error:
            caught_error = error
            debug_output = "".join(traceback.format_exception(error))
        else:
            self.fail("unclassified input must fail closed")

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("animemo.secret-envelope.redaction-test")
        old_handlers = logger.handlers[:]
        old_propagate = logger.propagate
        logger.handlers = [handler]
        logger.propagate = False
        try:
            logger.error("validation=%r material=%r", caught_error, entry)
        finally:
            logger.handlers = old_handlers
            logger.propagate = old_propagate

        self.assertEqual(str(caught_error), "CREDENTIAL_CONTINUITY_REQUIRED")
        self.assertEqual(repr(entry), "<SecretEntry redacted>")
        self.assertEqual(
            stream.getvalue().strip(),
            "validation=SecretEnvelopeInputError('CREDENTIAL_CONTINUITY_REQUIRED') "
            "material=<SecretEntry redacted>",
        )
        for rendered in (
            str(caught_error),
            repr(caught_error),
            repr(entry),
            debug_output,
            stream.getvalue(),
        ):
            self.assertNotIn(sentinel.decode("ascii"), rendered)

    def test_producer_rejects_a_profile_larger_than_consumer_limit(self):
        oversized_value = b"x" * (1024 * 1024)
        entries = tuple(
            SecretEntry.preserve(name, oversized_value)
            for name in (
                "CREDENTIAL_ENCRYPTION_KEY",
                "DJANGO_SECRET_KEY",
                "BANGUMI_OAUTH_CLIENT_SECRET",
                "RESEND_API_KEY",
                "TURNSTILE_SECRET",
            )
        )
        with self.assertRaises(SecretEnvelopeInputError) as caught:
            create_secret_envelope(
                external_secret=OneTimeKey.from_bytes(bytes(range(32))),
                artifact_type="backup",
                artifact_id=ARTIFACT_ID,
                artifact_binding_record=binding_record("backup"),
                source_instance_id=INSTANCE_ID,
                secret_entries=entries,
            )
        self.assertEqual(caught.exception.code, "SECRET_PROFILE_INVALID")


if __name__ == "__main__":
    unittest.main()
