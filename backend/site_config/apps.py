from django.apps import AppConfig


class SiteConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "site_config"
    label = "site"
    verbose_name = "站点配置"
