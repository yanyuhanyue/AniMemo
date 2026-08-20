import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import PendingRegistration
from .models import UserSettings
from plugin_host.hooks import run_hook, run_registration_hook
from plugin_host.sdk import UserHookContext
from .staff_services import get_security_profile


User = get_user_model()


def normalize_email(value):
    return str(value or "").strip().casefold()


def token_digest(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def email_digest(value):
    return token_digest(normalize_email(value))


def user_agent_digest(request):
    return token_digest(request.META.get("HTTP_USER_AGENT", ""))


def new_registration_token():
    return secrets.token_urlsafe(32)


def _ttl(name, default):
    try:
        return max(60, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


def registration_token_ttl():
    return _ttl("REGISTRATION_TOKEN_TTL_SECONDS", 3600)


def completion_token_ttl():
    return _ttl("REGISTRATION_COMPLETION_TOKEN_TTL_SECONDS", 900)


def build_verify_url(request, raw_token):
    base = str(getattr(settings, "FRONTEND_URL", "")).rstrip("/")
    return f"{base}/register/verify?{urlencode({'token': raw_token})}"


def request_pending_registration(*, request, email):
    normalized = normalize_email(email)
    now = timezone.now()
    run_registration_hook("registration.before_request", request=request, email=normalized)
    raw_token = new_registration_token()
    values = {
        "token_hash": token_digest(raw_token),
        "expires_at": now + timedelta(seconds=registration_token_ttl()),
        "verified_at": None,
        "consumed_at": None,
        "completion_token_hash": "",
        "completion_token_expires_at": None,
        "requested_ip": request.META.get("REMOTE_ADDR") or None,
        "user_agent_digest": user_agent_digest(request),
        "last_sent_at": now,
    }
    with transaction.atomic():
        if User.objects.filter(email__iexact=normalized).exists():
            return None, None, False
        pending, created = PendingRegistration.objects.select_for_update().get_or_create(
            email=normalized,
            defaults={"resend_count": 0, **values},
        )
        # Re-check after taking the pending row lock so a concurrent completion
        # cannot leave a fresh pending row behind for an already-created user.
        if User.objects.filter(email__iexact=normalized).exists():
            return None, None, False
        if not created:
            values["resend_count"] = pending.resend_count + 1
            for key, value in values.items():
                setattr(pending, key, value)
            pending.save(update_fields=[*values.keys()])
        return pending, raw_token, True


def verify_registration_token(*, raw_token):
    digest = token_digest(raw_token)
    now = timezone.now()
    with transaction.atomic():
        pending = PendingRegistration.objects.select_for_update().filter(token_hash=digest).first()
        if not pending or pending.consumed_at or pending.verified_at or pending.expires_at <= now:
            return None, None
        completion_token = new_registration_token()
        pending.verified_at = now
        pending.completion_token_hash = token_digest(completion_token)
        pending.completion_token_expires_at = now + timedelta(seconds=completion_token_ttl())
        pending.save(update_fields=["verified_at", "completion_token_hash", "completion_token_expires_at"])
        return pending, completion_token


def complete_registration(*, request, completion_token, username, password):
    digest = token_digest(completion_token)
    normalized_username = str(username or "").strip()
    now = timezone.now()
    with transaction.atomic():
        pending = PendingRegistration.objects.select_for_update().filter(completion_token_hash=digest).first()
        if (
            not pending
            or not pending.verified_at
            or pending.consumed_at
            or not pending.completion_token_expires_at
            or pending.completion_token_expires_at <= now
        ):
            return None, "invalid_completion"
        run_registration_hook(
            "registration.before_complete",
            request=request,
            email=pending.email,
            username=normalized_username,
        )
        if User.objects.filter(email__iexact=pending.email).exists():
            return None, "email_exists"
        if User.objects.filter(username__iexact=normalized_username).exists():
            return None, "username_exists"
        try:
            user = User.objects.create_user(
                username=normalized_username,
                email=pending.email,
                password=password,
                is_active=True,
            )
            UserSettings.objects.create(user=user, nickname=normalized_username)
            security = get_security_profile(user)
            security.email_verified = True
            security.save(update_fields=["email_verified", "updated_at"])
        except IntegrityError as error:
            message = str(error).lower()
            if "email" in message:
                return None, "email_exists"
            if "username" in message:
                return None, "username_exists"
            raise
        pending.consumed_at = now
        pending.completion_token_hash = ""
        pending.completion_token_expires_at = None
        pending.save(update_fields=["consumed_at", "completion_token_hash", "completion_token_expires_at"])
    run_registration_hook("registration.after_complete", request=request, user=user)
    run_hook("user.after_created", UserHookContext(user_id=user.pk, source="registration"))
    return user, None
