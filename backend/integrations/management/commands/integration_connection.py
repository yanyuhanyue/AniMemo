import secrets

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from integrations.models import IntegrationConnection


class Command(BaseCommand):
    help = "创建集成连接或轮换共享密钥。"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("create", "rotate-secret"))
        parser.add_argument("connection_id", nargs="?")
        parser.add_argument("--provider")
        parser.add_argument("--instance-id")
        parser.add_argument("--name")

    @staticmethod
    def _new_key_id():
        return f"ak_{secrets.token_hex(12)}"

    @staticmethod
    def _new_secret():
        return secrets.token_urlsafe(32)

    def _require_interactive_secret_output(self):
        if not self.stdout.isatty():
            raise CommandError(
                "共享密钥只能写入交互式终端；已拒绝可能被日志或管道捕获的输出。"
            )

    def handle(self, *args, **options):
        if options["action"] == "create":
            return self._create(options)
        return self._rotate(options)

    def _create(self, options):
        provider = str(options.get("provider") or "").strip()
        instance_id = str(options.get("instance_id") or "").strip()
        name = str(options.get("name") or "").strip()
        if not provider or not instance_id or not name:
            raise CommandError("create 需要 --provider、--instance-id 和 --name。")
        self._require_interactive_secret_output()
        secret = self._new_secret()
        connection = IntegrationConnection(
            provider=provider,
            instance_id=instance_id,
            name=name,
            key_id=self._new_key_id(),
        )
        connection.set_secret(secret)
        try:
            connection.full_clean()
            connection.save()
        except (IntegrityError, ValidationError, ValueError) as error:
            raise CommandError(f"无法创建集成连接：{error}") from error
        self.stdout.write(f"connection_id: {connection.pk}")
        self.stdout.write(f"key_id: {connection.key_id}")
        self.stdout.write(f"secret: {secret}")
        self.stdout.write(self.style.WARNING("请立即安全保存 secret；数据库只保存密文，本命令不会再次显示。"))

    def _rotate(self, options):
        connection_id = str(options.get("connection_id") or "").strip()
        if not connection_id:
            raise CommandError("rotate-secret 需要 connection_id。")
        try:
            connection = IntegrationConnection.objects.get(pk=connection_id)
        except (ValueError, IntegrationConnection.DoesNotExist) as error:
            raise CommandError("集成连接不存在。") from error
        self._require_interactive_secret_output()
        secret = self._new_secret()
        connection.key_id = self._new_key_id()
        connection.set_secret(secret)
        connection.save(update_fields=("key_id", "encrypted_secret", "updated_at"))
        self.stdout.write(f"connection_id: {connection.pk}")
        self.stdout.write(f"key_id: {connection.key_id}")
        self.stdout.write(f"secret: {secret}")
        self.stdout.write(self.style.WARNING("旧 key_id/secret 已立即失效；新 secret 只显示本次。"))
