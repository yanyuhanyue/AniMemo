from django.apps import AppConfig


class PluginHostConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "plugin_host"
    verbose_name = "插件宿主"

    def ready(self):
        from journal.mutation_ports import bind_mutation_ports

        from .hooks import run_filter, run_hook

        bind_mutation_ports(policy_runner=run_filter, event_publisher=run_hook)
