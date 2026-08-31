from types import SimpleNamespace

from django.db import models
from django.db.models.functions import Lower
from django.test import SimpleTestCase

from integrations.models import IntegrationActionReceipt, IntegrationConnection
from journal.models import (
    Column,
    ExternalImportSession,
    JournalEntry,
    UserSettings,
    avatar_upload_to,
    column_cover_upload_to,
    poster_upload_to,
)
from plugin_host.models import PluginProject, PluginVersion
from site_config.models import MediaObject


def named_constraint(model, name):
    return next(item for item in model._meta.constraints if item.name == name)


def constraint_fields(model, name):
    return tuple(named_constraint(model, name).fields)


class StableResourceIdentityContractTests(SimpleTestCase):
    def test_owner_resources_keep_stable_integer_ids_and_public_uuid_slugs(self):
        self.assertIsInstance(JournalEntry._meta.pk, models.AutoField)
        self.assertIsInstance(JournalEntry._meta.get_field("share_slug"), models.UUIDField)
        self.assertTrue(JournalEntry._meta.get_field("share_slug").unique)
        self.assertFalse(JournalEntry._meta.get_field("share_slug").editable)

        for model, field_name in ((UserSettings, "public_slug"), (Column, "slug")):
            field = model._meta.get_field(field_name)
            self.assertIsInstance(field, models.UUIDField)
            self.assertTrue(field.unique)
            self.assertFalse(field.editable)

    def test_session_integration_and_media_public_ids_are_uuid_primary_keys(self):
        for model in (ExternalImportSession, IntegrationConnection, MediaObject):
            self.assertIsInstance(model._meta.pk, models.UUIDField)
            self.assertFalse(model._meta.pk.editable)

    def test_plugin_and_integration_business_identities_are_unique(self):
        self.assertTrue(PluginProject._meta.get_field("plugin_id").unique)
        self.assertTrue(PluginProject._meta.get_field("slug").unique)
        plugin_version_identity = named_constraint(
            PluginVersion,
            "plugin_version_ci_unique",
        )
        self.assertIsInstance(plugin_version_identity, models.UniqueConstraint)
        self.assertEqual(
            plugin_version_identity.expressions,
            (models.F("plugin"), Lower("version")),
        )
        self.assertEqual(plugin_version_identity.fields, ())
        self.assertEqual(
            constraint_fields(IntegrationActionReceipt, "integration_action_request_uniq"),
            ("connection", "request_id"),
        )

    def test_media_upload_keys_do_not_depend_on_mutable_names_or_titles(self):
        user_owned = SimpleNamespace(user_id=17, title="可变标题", user=SimpleNamespace(username="mutable"))
        author_owned = SimpleNamespace(author_id=23, title="可变专栏")

        poster_key = poster_upload_to(user_owned, "poster.png")
        avatar_key = avatar_upload_to(user_owned, "avatar.png")
        cover_key = column_cover_upload_to(author_owned, "cover.png")

        self.assertTrue(poster_key.startswith("users/17/posters/"))
        self.assertTrue(avatar_key.startswith("users/17/avatars/"))
        self.assertTrue(cover_key.startswith("users/23/columns/"))
        for key in (poster_key, avatar_key, cover_key):
            self.assertNotIn("mutable", key)
            self.assertNotIn("可变", key)
