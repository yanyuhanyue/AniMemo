import logging
import hashlib

import requests
from django.conf import settings

from site_config.models import SiteSettings
from .secrets import SecretDecryptionError


logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    pass


class EmailDeliveryDisabled(EmailDeliveryError):
    pass


class EmailDeliveryNotConfigured(EmailDeliveryError):
    pass


def send_transactional_email(*, to, subject, html, text):
    site_settings = SiteSettings.load()
    if not site_settings.email_delivery_enabled:
        raise EmailDeliveryDisabled("管理员已关闭激活邮件服务。")

    try:
        api_key = site_settings.get_resend_api_key()
    except SecretDecryptionError as error:
        raise EmailDeliveryNotConfigured(str(error)) from error

    email_from = site_settings.get_email_from()
    if api_key:
        try:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": email_from, "to": [to], "subject": subject, "html": html, "text": text},
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            recipient_hash = hashlib.sha256(str(to).strip().casefold().encode("utf-8")).hexdigest()
            logger.warning("Transactional email failed for recipient_hash=%s: %s", recipient_hash, error.__class__.__name__)
            raise EmailDeliveryError("邮件服务请求失败，请检查密钥、发件域名和网络状态。") from error

    if settings.DEBUG:
        # Development delivery must not leak verification tokens or complete URLs.
        logger.info("DEV EMAIL queued recipient_domain=%s subject=%s", str(to).rsplit("@", 1)[-1], subject)
        return {"id": "development-console"}

    raise EmailDeliveryNotConfigured("邮件服务尚未配置 Resend API Key。")
