import hashlib
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor

from accounts.models import LoginEvent
from config.api_errors import public_failure
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import login as session_login
from django.contrib.auth import logout as session_logout
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.middleware.csrf import get_token, rotate_token
from django.utils.decorators import method_decorator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from drf_spectacular.utils import OpenApiParameter, OpenApiRequest, extend_schema
from plugin_host.hooks import RegistrationHookRejected, RegistrationHookUnavailable
from plugin_host.permissions import plugin_permissions_for_user
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import AuthenticationFailed, Throttled
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from site_config.models import InstallationState, SiteSettings

from .account_security import AccountDeletionError, delete_current_account
from .admin_security_middleware import (
    clear_staff_second_factor,
    mark_staff_second_factor_verified,
)
from .auth_service import authenticate_with_second_factor
from .auth_tokens import (
    RefreshReplayError,
    create_refresh_token,
    issue_token_pair,
    revoke_access_token,
    rotate_refresh,
)
from .emails import EmailDeliveryError, send_transactional_email
from .openapi_serializers import (
    TOKEN_LOGIN_REQUEST_SCHEMA,
    AccessTokenResponseSerializer,
    AccountDeleteRequestSerializer,
    CsrfTokenResponseSerializer,
    LoginResponseSerializer,
    MessageResponseSerializer,
    PasswordChangeRequestSerializer,
    PasswordResetConfirmRequestSerializer,
    PasswordResetRequestSerializer,
    RegistrationVerificationResponseSerializer,
)
from .registration import (
    build_verify_url,
    complete_registration,
    email_digest,
    request_pending_registration,
    verify_registration_token,
)
from .serializers import (
    RegistrationCompleteSerializer,
    RegistrationRequestSerializer,
    RegistrationVerifySerializer,
)
from .staff_services import (
    get_security_profile,
    record_audit,
    record_login_event,
    resolve_staff_role,
    staff_capabilities,
    update_user_password,
)
from .web_auth_adapter import (
    access_token_from_request,
    clear_refresh_cookie,
    no_store,
    require_anti_abuse_challenge,
    set_refresh_cookie,
)

User = get_user_model()
logger = logging.getLogger(__name__)
_EMAIL_DELIVERY_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="animemo-email",
)


def _submit_email_task(delivery):
    try:
        _EMAIL_DELIVERY_EXECUTOR.submit(delivery)
    except RuntimeError:
        logger.error("Transactional email executor is unavailable.")


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = "username"
    otp = serializers.CharField(required=False, allow_blank=True, write_only=True)
    recovery_code = serializers.CharField(required=False, allow_blank=True, write_only=True)

    @classmethod
    def get_token(cls, user):
        return create_refresh_token(user)

    def validate(self, attrs):
        otp = attrs.pop("otp", "")
        recovery_code = attrs.pop("recovery_code", "")
        account = str(attrs.get("username", "")).strip()
        request = self.context.get("request")
        try:
            result = authenticate_with_second_factor(
                request=request,
                username=account,
                password=attrs.get("password", ""),
                otp=otp,
                recovery_code=recovery_code,
            )
        except AuthenticationFailed:
            if request:
                matched_user = User.objects.filter(email__iexact=account).first() if "@" in account else User.objects.filter(username__iexact=account).first()
                record_login_event(request, event_type=LoginEvent.EventType.LOGIN_FAILED, success=False, user=matched_user, account=account)
            raise AuthenticationFailed("用户名、密码或验证码不正确。") from None
        refresh, access = issue_token_pair(result.user)
        if request:
            record_login_event(request, event_type=LoginEvent.EventType.LOGIN, success=True, user=result.user, account=account)
        return {
            "refresh": str(refresh),
            "access": str(access),
            "user": {"id": result.user.id, "username": result.user.username, "email": result.user.email},
            "used_recovery_code": result.used_recovery_code,
            "remaining_recovery_codes": result.remaining_recovery_codes,
        }


@method_decorator(csrf_protect, name="dispatch")
class EmailTokenObtainPairView(TokenObtainPairView):
    serializer_class = EmailTokenObtainPairSerializer
    throttle_scope = "login"
    secondary_throttle_scope = "two_factor"
    account_throttle_scope = "login"
    throttle_account_fields = ("username",)

    @extend_schema(request=OpenApiRequest(TOKEN_LOGIN_REQUEST_SCHEMA), responses=LoginResponseSerializer, auth=[])
    def post(self, request, *args, **kwargs):
        turnstile_response = require_anti_abuse_challenge(request)
        if turnstile_response is not None:
            return turnstile_response
        response = super().post(request, *args, **kwargs)
        raw_refresh = response.data.pop("refresh", None)
        if raw_refresh:
            set_refresh_cookie(response, raw_refresh)
            rotate_token(request._request)
        return no_store(response)


@method_decorator(csrf_protect, name="dispatch")
class StaffLoginView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = "login"
    secondary_throttle_scope = "two_factor"
    account_throttle_scope = "login"
    throttle_account_fields = ("username",)

    @extend_schema(request=OpenApiRequest(TOKEN_LOGIN_REQUEST_SCHEMA), responses=LoginResponseSerializer, auth=[])
    def post(self, request):
        turnstile_response = require_anti_abuse_challenge(request)
        if turnstile_response is not None:
            return turnstile_response
        account = str(request.data.get("username", "")).strip()
        password = str(request.data.get("password", ""))
        try:
            result = authenticate_with_second_factor(
                request=request,
                username=account,
                password=password,
                otp=request.data.get("otp", ""),
                recovery_code=request.data.get("recovery_code", ""),
                staff_only=True,
            )
        except AuthenticationFailed:
            matched_user = User.objects.filter(email__iexact=account).first() if "@" in account else User.objects.filter(username__iexact=account).first()
            record_login_event(request, event_type=LoginEvent.EventType.LOGIN_FAILED, success=False, user=matched_user, account=account)
            return Response(
                {
                    "code": "invalid_credentials",
                    "detail": "用户名、密码或验证码不正确。",
                    "two_factor_required": True,
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        refresh, access = issue_token_pair(result.user)
        session_login(request, result.user)
        security_profile = get_security_profile(result.user)
        admin_access = bool(result.second_factor_verified and security_profile.two_factor_enabled)
        if admin_access:
            mark_staff_second_factor_verified(request, result.user, security_profile)
        else:
            clear_staff_second_factor(request)
        record_login_event(request, event_type=LoginEvent.EventType.LOGIN, success=True, user=result.user, account=account)
        requested_admin_path = str(request.data.get("next", "")).strip()
        if not requested_admin_path.startswith("/admin/") and requested_admin_path != "/admin":
            requested_admin_path = "/admin/"
        response = Response({
            "access": str(access),
            "admin_url": request.build_absolute_uri(requested_admin_path),
            "admin_access": admin_access,
            "user": {"id": result.user.id, "username": result.user.username, "email": result.user.email, "is_staff": True, "role": resolve_staff_role(result.user), "capabilities": staff_capabilities(result.user), "pluginPermissions": plugin_permissions_for_user(result.user)},
            "used_recovery_code": result.used_recovery_code,
            "remaining_recovery_codes": result.remaining_recovery_codes,
        })
        set_refresh_cookie(response, refresh)
        return no_store(response)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfTokenView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(responses=CsrfTokenResponseSerializer, auth=[])
    def get(self, request):
        return no_store(Response({"csrf_token": get_token(request)}))


@method_decorator(csrf_protect, name="dispatch")
class CookieTokenRefreshView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = "login"

    @extend_schema(request=None, responses=AccessTokenResponseSerializer, auth=[{"refreshCookie": []}], parameters=[OpenApiParameter("X-CSRFToken", str, OpenApiParameter.HEADER, required=True)])
    def post(self, request):
        raw_refresh = request.COOKIES.get(settings.REFRESH_COOKIE_NAME)
        if not raw_refresh:
            return no_store(Response({"code": "authentication_required", "detail": "刷新凭据缺失，请重新登录。"}, status=status.HTTP_401_UNAUTHORIZED))
        try:
            user, access, rotated = rotate_refresh(raw_refresh)
        except RefreshReplayError as error:
            record_audit(
                request,
                action="security.refresh_replay_rejected",
                target=error.user,
                metadata={"token_jti_hash": hashlib.sha256(error.jti.encode("utf-8")).hexdigest()[:16]},
            )
            response = Response({"code": "session_expired", "detail": "登录会话已失效，请重新登录。"}, status=status.HTTP_401_UNAUTHORIZED)
            clear_refresh_cookie(response)
            return no_store(response)
        except (TokenError, AuthenticationFailed, ValueError, TypeError):
            response = Response({"code": "session_expired", "detail": "登录会话已失效，请重新登录。"}, status=status.HTTP_401_UNAUTHORIZED)
            clear_refresh_cookie(response)
            return no_store(response)
        response = Response({
            "access": str(access),
            "user": {"id": user.id, "username": user.username, "email": user.email, "is_staff": user.is_staff},
        })
        if rotated is not None:
            set_refresh_cookie(response, rotated)
        return no_store(response)


@method_decorator(csrf_protect, name="dispatch")
class LogoutView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(request=None, responses=MessageResponseSerializer, auth=[{"refreshCookie": []}])
    def post(self, request):
        raw_refresh = request.COOKIES.get(settings.REFRESH_COOKIE_NAME)
        if raw_refresh:
            try:
                RefreshToken(raw_refresh).blacklist()
            except TokenError:
                pass
        revoke_access_token(access_token_from_request(request))
        clear_staff_second_factor(request)
        session_logout(request._request)
        response = Response({"detail": "已安全退出。"})
        clear_refresh_cookie(response)
        return no_store(response)


class RegistrationThrottleAuditMixin:
    def check_throttles(self, request):
        try:
            return super().check_throttles(request)
        except Throttled:
            value = request.data.get("email") or request.data.get("token") or request.data.get("completion_token")
            record_audit(request, action="registration_rate_limited", metadata={"identifier_hash": email_digest(value), "result": "rate_limited"})
            raise


def installation_registration_guard():
    if InstallationState.is_initialized():
        return None
    return no_store(Response(
        {
            "code": "installation_uninitialized",
            "detail": "站点尚未完成首次初始化，暂不开放注册。",
        },
        status=status.HTTP_403_FORBIDDEN,
    ))


@method_decorator(csrf_protect, name="dispatch")
class RegisterView(RegistrationThrottleAuditMixin, APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "register_request"
    account_throttle_scope = "register_request"
    throttle_account_fields = ("email",)

    @extend_schema(request=RegistrationRequestSerializer, responses=MessageResponseSerializer, auth=[])
    def post(self, request):
        installation_response = installation_registration_guard()
        if installation_response is not None:
            return installation_response
        turnstile_response = require_anti_abuse_challenge(request)
        if turnstile_response is not None:
            return turnstile_response
        site_settings = SiteSettings.load()
        if not site_settings.registration_enabled:
            return Response(
                public_failure(
                    request=request,
                    candidate_code="permission_denied",
                    status_code=status.HTTP_403_FORBIDDEN,
                ),
                status=status.HTTP_403_FORBIDDEN,
            )
        if not site_settings.email_delivery_enabled:
            return Response(
                public_failure(
                    request=request,
                    candidate_code="email_delivery_failed",
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                ),
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        serializer = RegistrationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        try:
            pending, raw_token, should_send = request_pending_registration(request=request, email=email)
        except RegistrationHookRejected:
            record_audit(request, action="registration_rejected_by_policy", metadata={"email_hash": email_digest(email), "result": "rejected"})
            return Response(
                public_failure(request=request, candidate_code="registration_policy_rejected", status_code=status.HTTP_403_FORBIDDEN),
                status=status.HTTP_403_FORBIDDEN,
            )
        except RegistrationHookUnavailable:
            record_audit(request, action="registration_rejected_by_policy", metadata={"email_hash": email_digest(email), "result": "unavailable"})
            return Response(
                public_failure(request=request, candidate_code="registration_policy_unavailable", status_code=status.HTTP_503_SERVICE_UNAVAILABLE),
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        record_audit(request, action="registration_requested", metadata={"email_hash": email_digest(email), "result": "accepted"})
        if should_send and pending and raw_token:
            verify_url = build_verify_url(request, raw_token)

            def deliver_registration_email():
                try:
                    send_transactional_email(
                        to=email,
                        subject=f"继续创建你的 {site_settings.site_name} 账号",
                        html=(
                            f"<h1>继续创建 {site_settings.site_name} 账号</h1>"
                            "<p>有人使用此邮箱请求创建 AniMemo 账号。</p>"
                            f"<p>如果是你本人，请点击<a href=\"{verify_url}\">此链接继续注册</a>。</p>"
                            "<p>完成邮箱验证后，你还需要自行设置用户名和密码。</p>"
                            "<p>如果你没有发起注册，请忽略此邮件。点击此链接不会自动为你创建带已有密码的账号。</p>"
                        ),
                        text=(
                            "有人使用此邮箱请求创建 AniMemo 账号。\n\n"
                            f"如果是你本人，请点击下面的链接继续注册：{verify_url}\n"
                            "完成邮箱验证后，你还需要自行设置用户名和密码。\n\n"
                            "如果你没有发起注册，请忽略此邮件。点击此链接不会自动为你创建带已有密码的账号。"
                        ),
                    )
                    record_audit(request, action="registration_email_sent", metadata={"email_hash": email_digest(email), "result": "sent"})
                except EmailDeliveryError:
                    logger.error(
                        "registration_email_delivery_failed",
                        extra={
                            "animemo_stage": "registration_email_delivery",
                            "correlation_id": secrets.token_hex(16),
                            "animemo_exception_class": "EmailDeliveryError",
                        },
                    )

            transaction.on_commit(lambda: _submit_email_task(deliver_registration_email))
        else:
            transaction.on_commit(lambda: _submit_email_task(lambda: None))
        return Response({"detail": "如果该邮箱可以注册，我们已经发送验证邮件。"}, status=status.HTTP_201_CREATED)


@method_decorator(csrf_protect, name="dispatch")
class VerifyRegistrationView(RegistrationThrottleAuditMixin, APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "register_verify"
    account_throttle_scope = "register_verify"
    throttle_account_fields = ("token",)

    @extend_schema(request=RegistrationVerifySerializer, responses=RegistrationVerificationResponseSerializer, auth=[])
    def post(self, request):
        installation_response = installation_registration_guard()
        if installation_response is not None:
            return installation_response
        serializer = RegistrationVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pending, completion_token = verify_registration_token(raw_token=serializer.validated_data["token"])
        if not pending:
            return Response({"detail": "注册链接无效、已过期或已经使用。"}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(request, action="registration_verified", metadata={"email_hash": email_digest(pending.email), "result": "verified"})
        return Response({
            "detail": "邮箱验证成功，请完成账号设置。",
            "completion_token": completion_token,
            "email": pending.email,
        })


@method_decorator(csrf_protect, name="dispatch")
class CompleteRegistrationView(RegistrationThrottleAuditMixin, APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "register_complete"
    account_throttle_scope = "register_complete"
    throttle_account_fields = ("completion_token",)

    @extend_schema(request=RegistrationCompleteSerializer, responses=MessageResponseSerializer, auth=[])
    def post(self, request):
        installation_response = installation_registration_guard()
        if installation_response is not None:
            return installation_response
        turnstile_response = require_anti_abuse_challenge(request)
        if turnstile_response is not None:
            return turnstile_response
        serializer = RegistrationCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user, error_code = complete_registration(
                request=request,
                completion_token=serializer.validated_data["completion_token"],
                username=serializer.validated_data["username"],
                password=serializer.validated_data["password"],
            )
        except RegistrationHookRejected:
            return Response(
                public_failure(request=request, candidate_code="registration_policy_rejected", status_code=status.HTTP_403_FORBIDDEN),
                status=status.HTTP_403_FORBIDDEN,
            )
        except RegistrationHookUnavailable:
            return Response(
                public_failure(request=request, candidate_code="registration_policy_unavailable", status_code=status.HTTP_503_SERVICE_UNAVAILABLE),
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if error_code == "invalid_completion":
            return Response({"detail": "注册完成凭证无效、已过期或已经使用。"}, status=status.HTTP_400_BAD_REQUEST)
        if error_code == "email_exists":
            return Response({"detail": "该邮箱已注册。"}, status=status.HTTP_400_BAD_REQUEST)
        if error_code == "username_exists":
            return Response({"detail": "该用户名已被使用。"}, status=status.HTTP_400_BAD_REQUEST)
        record_audit(
            request,
            action="registration_completed",
            target=user,
            metadata={"email_hash": email_digest(user.email), "result": "created"},
        )
        return Response({"detail": "注册完成，请使用新账号登录。"}, status=status.HTTP_201_CREATED)


@method_decorator(csrf_protect, name="dispatch")
class PasswordResetView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "password_reset"
    account_throttle_scope = "password_reset"
    throttle_account_fields = ("email",)

    @extend_schema(request=PasswordResetRequestSerializer, responses=MessageResponseSerializer, auth=[])
    def post(self, request):
        turnstile_response = require_anti_abuse_challenge(request)
        if turnstile_response is not None:
            return turnstile_response
        email = serializers.EmailField().run_validation(request.data.get("email", ""))
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        def deliver_password_reset():
            if user is None:
                return
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = (
                f"{settings.FRONTEND_URL}/login?reset_uid={uid}&reset_token={token}"
            )
            try:
                send_transactional_email(
                    to=user.email,
                    subject=f"重置 {SiteSettings.load().site_name} 密码",
                    html=f"<h1>重置密码</h1><p><a href=\"{reset_url}\">设置新密码</a></p><p>如果不是你发起的，请忽略此邮件。</p>",
                    text=f"请打开以下链接重置密码：{reset_url}",
                )
            except EmailDeliveryError:
                pass
        _submit_email_task(deliver_password_reset)
        return Response({"detail": "如果邮箱存在，重置邮件已发送。"})


@method_decorator(csrf_protect, name="dispatch")
class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "password_reset"
    account_throttle_scope = "password_reset"
    throttle_account_fields = ("uid",)

    @extend_schema(request=PasswordResetConfirmRequestSerializer, responses=MessageResponseSerializer, auth=[])
    def post(self, request):
        turnstile_response = require_anti_abuse_challenge(request)
        if turnstile_response is not None:
            return turnstile_response
        try:
            user_id = force_str(urlsafe_base64_decode(request.data.get("uid", "")))
            user = User.objects.get(pk=user_id, is_active=True)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"detail": "重置链接无效或已过期。"}, status=status.HTTP_400_BAD_REQUEST)
        token = request.data.get("token", "")
        if not default_token_generator.check_token(user, token):
            return Response({"detail": "重置链接无效或已过期。"}, status=status.HTTP_400_BAD_REQUEST)
        password = request.data.get("password", "")
        confirm = request.data.get("password_confirm", "")
        if password != confirm:
            return Response({"password_confirm": ["两次输入的密码不一致。"]}, status=status.HTTP_400_BAD_REQUEST)
        from django.contrib.auth.password_validation import validate_password
        try:
            validate_password(password, user=user)
        except DjangoValidationError as error:
            return Response({"password": error.messages}, status=status.HTTP_400_BAD_REQUEST)
        update_user_password(user, password)
        return Response({"detail": "密码已更新，所有设备需要重新登录。"})


class PasswordChangeView(APIView):
    throttle_scope = "two_factor"
    account_throttle_scope = "two_factor"
    throttle_account_fields = ()

    @extend_schema(request=PasswordChangeRequestSerializer, responses=MessageResponseSerializer)
    def post(self, request):
        current_password = str(request.data.get("current_password", ""))
        password = str(request.data.get("password", ""))
        confirm = str(request.data.get("password_confirm", ""))
        if not request.user.check_password(current_password):
            return Response({"current_password": ["当前密码不正确。"]}, status=status.HTTP_400_BAD_REQUEST)
        if password != confirm:
            return Response({"password_confirm": ["两次输入的密码不一致。"]}, status=status.HTTP_400_BAD_REQUEST)
        from django.contrib.auth.password_validation import validate_password
        try:
            validate_password(password, user=request.user)
        except DjangoValidationError as error:
            return Response({"password": error.messages}, status=status.HTTP_400_BAD_REQUEST)
        update_user_password(request.user, password)
        response = Response({"detail": "密码已更新，所有设备需要重新登录。"})
        clear_refresh_cookie(response)
        return no_store(response)


class AccountView(APIView):
    throttle_scope = "two_factor"
    account_throttle_scope = "two_factor"

    @extend_schema(request=AccountDeleteRequestSerializer, responses={204: None})
    def delete(self, request):
        current_password = str(request.data.get("current_password", ""))
        try:
            delete_current_account(
                user=request.user,
                current_password=current_password,
                otp=request.data.get("otp", ""),
                recovery_code=request.data.get("recovery_code", ""),
                request=request,
            )
        except AccountDeletionError as error:
            from .staff_services import record_audit
            record_audit(
                request,
                action="security.account_deletion_rejected",
                target=request.user,
                metadata={"reason": error.reason},
            )
            return Response({error.field: [error.detail]}, status=status.HTTP_400_BAD_REQUEST)
        clear_staff_second_factor(request)
        session_logout(request._request)
        response = Response(status=status.HTTP_204_NO_CONTENT)
        clear_refresh_cookie(response)
        return no_store(response)
