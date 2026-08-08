import io
import threading
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from config.credentials import CredentialCipherError
from django.utils.encoding import force_bytes

from site_config.models import MediaStorageBackend

from .common import MediaStorageOffline, safe_error_summary, safe_object_key


R2_IO_ERRORS = (BotoCoreError, ClientError, CredentialCipherError, OSError, TimeoutError)
R2_CLIENT_CONFIG = Config(
    connect_timeout=8,
    read_timeout=25,
    retries={"mode": "standard", "max_attempts": 3},
)


class DynamicR2Backend:
    _clients = {}
    _lock = threading.Lock()

    @classmethod
    def clear_client_cache(cls):
        with cls._lock:
            cls._clients.clear()

    @classmethod
    def client_for(cls, backend):
        version = MediaStorageBackend.objects.filter(pk=backend.pk).values_list("config_version", flat=True).get()
        cached = cls._clients.get(backend.pk)
        if cached and cached[0] == version:
            return cached[1]
        with cls._lock:
            cached = cls._clients.get(backend.pk)
            if cached and cached[0] == version:
                return cached[1]
            current = MediaStorageBackend.objects.get(pk=backend.pk)
            if not current.access_key_configured or not current.secret_key_configured:
                raise MediaStorageOffline("R2 凭证尚未配置。")
            client = boto3.client(
                "s3",
                endpoint_url=current.endpoint_url,
                aws_access_key_id=current.get_access_key_id(),
                aws_secret_access_key=current.get_secret_access_key(),
                region_name=current.region or "auto",
                config=R2_CLIENT_CONFIG,
            )
            cls._clients[current.pk] = (current.config_version, client)
            return client

    def __init__(self, backend):
        self.backend = backend

    def write(self, key, content, *, content_type="application/octet-stream"):
        key = safe_object_key(key)
        try:
            client = self.client_for(self.backend)
            client.put_object(
                Bucket=self.backend.bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type or "application/octet-stream",
            )
            self._last_client = client
        except R2_IO_ERRORS as error:
            raise MediaStorageOffline(safe_error_summary(error)) from error

    def open(self, key):
        key = safe_object_key(key)
        try:
            response = self.client_for(self.backend).get_object(Bucket=self.backend.bucket_name, Key=key)
            return io.BytesIO(response["Body"].read())
        except R2_IO_ERRORS as error:
            raise MediaStorageOffline(safe_error_summary(error)) from error

    def exists(self, key):
        try:
            self.client_for(self.backend).head_object(Bucket=self.backend.bucket_name, Key=safe_object_key(key))
            return True
        except R2_IO_ERRORS:
            return False

    def delete(self, key):
        client = getattr(self, "_last_client", None) or self.client_for(self.backend)
        try:
            client.delete_object(Bucket=self.backend.bucket_name, Key=safe_object_key(key))
        except R2_IO_ERRORS as error:
            raise MediaStorageOffline(safe_error_summary(error)) from error

    def url(self, key):
        return f"{self.backend.public_base_url.rstrip('/')}/{safe_object_key(key)}"

    def test_connection(self):
        key = f"site/healthchecks/{uuid.uuid4().hex}"
        client = self.client_for(self.backend)
        wrote = False
        try:
            client.put_object(Bucket=self.backend.bucket_name, Key=key, Body=force_bytes("anime-journal-r2-healthcheck"), ContentType="text/plain")
            wrote = True
            client.head_object(Bucket=self.backend.bucket_name, Key=key)
            return "R2 read/write connection OK"
        except R2_IO_ERRORS as error:
            raise MediaStorageOffline(safe_error_summary(error, "R2 连接测试失败。")) from error
        finally:
            if wrote:
                try:
                    client.delete_object(Bucket=self.backend.bucket_name, Key=key)
                except R2_IO_ERRORS:
                    pass
