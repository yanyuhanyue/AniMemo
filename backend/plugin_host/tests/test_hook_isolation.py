from django.contrib.auth import get_user_model
from django.test import TestCase

from journal.models import Column
from plugin_host.hooks import HookRegistry
from plugin_host.models import PluginProject, UserPluginInstallation
from plugin_host.sdk.types import ColumnHookContext, JournalHookContext


class HookTenantIsolationTests(TestCase):
    def setUp(self):
        self.user_a = get_user_model().objects.create_user("hook-a", password="password-123")
        self.user_b = get_user_model().objects.create_user("hook-b", password="password-123")

    def test_user_hook_only_runs_for_installed_enabled_target_user(self):
        project = PluginProject.objects.create(
            plugin_id="com.example.user-hook",
            slug="user-hook",
            name="User Hook",
            description="test",
            installation_mode=PluginProject.InstallationMode.USER,
        )
        installation = UserPluginInstallation.objects.create(user=self.user_a, plugin=project, enabled=True)
        registry = HookRegistry()
        seen = []
        registry.register("journal.after_create", lambda context: seen.append(context.user_id), (project.slug, "1.0.0", "runtime-a"))

        registry.run_hook("journal.after_create", JournalHookContext(self.user_a.pk, 1, "test"))
        registry.run_hook("journal.after_create", JournalHookContext(self.user_b.pk, 2, "test"))
        self.assertEqual(seen, [self.user_a.pk])

        installation.enabled = False
        installation.save(update_fields=["enabled"])
        registry.run_hook("journal.after_create", JournalHookContext(self.user_a.pk, 3, "test"))
        self.assertEqual(seen, [self.user_a.pk])

    def test_system_plugin_user_scoped_hook_keeps_site_wide_semantics(self):
        project = PluginProject.objects.create(
            plugin_id="com.example.system-hook",
            slug="system-hook",
            name="System Hook",
            description="test",
            installation_mode=PluginProject.InstallationMode.SYSTEM,
        )
        registry = HookRegistry()
        seen = []
        registry.register("journal.after_create", lambda context: seen.append(context.user_id), (project.slug, "1.0.0", "runtime-system"))

        registry.run_hook("journal.after_create", JournalHookContext(self.user_a.pk, 1, "test"))
        registry.run_hook("journal.after_create", JournalHookContext(self.user_b.pk, 2, "test"))

        self.assertEqual(seen, [self.user_a.pk, self.user_b.pk])

    def test_user_plugin_system_scoped_hook_is_defensively_skipped(self):
        project = PluginProject.objects.create(
            plugin_id="com.example.invalid-user-hook",
            slug="invalid-user-hook",
            name="Invalid User Hook",
            description="test",
            installation_mode=PluginProject.InstallationMode.USER,
        )
        registry = HookRegistry()
        seen = []
        registry.register("registration.after_complete", lambda context: seen.append(context), (project.slug, "1.0.0", "runtime-invalid"))

        registry.run_hook("registration.after_complete", {"source": "test"})

        self.assertEqual(seen, [])

    def test_column_hook_resolves_author_instead_of_actor(self):
        project = PluginProject.objects.create(
            plugin_id="com.example.column-hook",
            slug="column-hook",
            name="Column Hook",
            description="test",
            installation_mode=PluginProject.InstallationMode.USER,
        )
        UserPluginInstallation.objects.create(user=self.user_a, plugin=project, enabled=True)
        column = Column.objects.create(author=self.user_a, title="Tenant Column", body="Body")
        registry = HookRegistry()
        seen = []
        registry.register("column.after_publish", lambda context: seen.append(context.column_id), (project.slug, "1.0.0", "runtime-column"))

        registry.run_hook(
            "column.after_publish",
            ColumnHookContext(column_id=column.pk, actor_id=self.user_b.pk, source="test"),
        )

        self.assertEqual(seen, [column.pk])
