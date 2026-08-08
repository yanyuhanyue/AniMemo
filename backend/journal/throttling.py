import hashlib
import logging
import math
import re
import secrets
import threading

from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection
from django_redis.exceptions import ConnectionInterrupted
from redis.exceptions import RedisError
from rest_framework.exceptions import APIException
from rest_framework.settings import api_settings
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, SimpleRateThrottle, UserRateThrottle

from .network import client_ip


logger = logging.getLogger(__name__)
_development_lock = threading.Lock()


AUTH_THROTTLE_LUA = """
local now = tonumber(ARGV[1])
local member = ARGV[2]
local allowed = 1
local retry_ms = 0

for index, key in ipairs(KEYS) do
  local offset = 2 + ((index - 1) * 2)
  local window = tonumber(ARGV[offset + 1])
  local limit = tonumber(ARGV[offset + 2])
  redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
  local count = redis.call('ZCARD', key)
  if count >= limit then
    allowed = 0
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if oldest[2] then
      local candidate = tonumber(oldest[2]) + window - now
      if candidate > retry_ms then retry_ms = candidate end
    end
  end
end

if allowed == 1 then
  for index, key in ipairs(KEYS) do
    local offset = 2 + ((index - 1) * 2)
    local window = tonumber(ARGV[offset + 1])
    redis.call('ZADD', key, now, member .. ':' .. index)
    redis.call('PEXPIRE', key, window + 1000)
  end
end

return {allowed, retry_ms}
"""


class AuthThrottleUnavailable(APIException):
    status_code = 503
    default_detail = "安全限流服务暂时不可用，请稍后重试。"
    default_code = "auth_throttle_unavailable"


class TrustedProxyAnonRateThrottle(AnonRateThrottle):
    def get_ident(self, request):
        return client_ip(request) or "unknown"

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except (ConnectionInterrupted, RedisError, OSError) as error:
            if getattr(view, "account_throttle_scope", None):
                raise AuthThrottleUnavailable() from error
            raise


class TrustedProxyUserRateThrottle(UserRateThrottle):
    def get_ident(self, request):
        return client_ip(request) or "unknown"

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except (ConnectionInterrupted, RedisError, OSError) as error:
            if getattr(view, "account_throttle_scope", None):
                raise AuthThrottleUnavailable() from error
            raise


class TrustedProxyScopedRateThrottle(ScopedRateThrottle):
    def get_ident(self, request):
        return client_ip(request) or "unknown"

    def get_rate(self):
        return api_settings.DEFAULT_THROTTLE_RATES.get(self.scope)

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except (ConnectionInterrupted, RedisError, OSError) as error:
            if getattr(view, "account_throttle_scope", None):
                raise AuthThrottleUnavailable() from error
            raise


class SecondaryScopedRateThrottle(TrustedProxyScopedRateThrottle):
    scope_attr = "secondary_throttle_scope"

    def allow_request(self, request, view):
        scope = getattr(view, self.scope_attr, None)
        if not scope or not (request.data.get("otp") or request.data.get("recovery_code")):
            return True
        if getattr(view, "account_throttle_scope", None):
            return True
        self.scope = scope
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super(ScopedRateThrottle, self).allow_request(request, view)


class HashedAccountRateThrottle(SimpleRateThrottle):
    scope_attr = "account_throttle_scope"

    def get_rate(self):
        return None

    def parse_rate(self, rate):
        if rate is None:
            return None, None
        num, period = rate.split("/", 1)
        match = re.fullmatch(r"(?P<count>\d+)?(?P<unit>s(?:ec(?:ond)?)?|m(?:in(?:ute)?)?|h(?:our)?|d(?:ay)?)s?", period.strip().lower())
        if not match:
            return super().parse_rate(rate)
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        multiplier = int(match.group("count") or 1)
        return int(num), multiplier * units[match.group("unit")[0]]

    def allow_request(self, request, view):
        self.view = view
        dimensions = self.get_cache_dimensions(request, view)
        if not dimensions:
            return True
        now = self.timer()
        parsed = []
        for key, rate in dimensions:
            num_requests, duration = self.parse_rate(rate)
            if num_requests and duration:
                parsed.append((self._redis_key(key), num_requests, duration))
        if not parsed:
            return True

        allowed, retry_after = self._atomic_allow(parsed, now)
        self.retry_after = retry_after
        self.now = now
        self.duration = parsed[0][2]
        self.num_requests = parsed[0][1]
        self.key = parsed[0][0]
        return allowed

    @staticmethod
    def _redis_key(key):
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        return f"{settings.AUTH_THROTTLE_KEY_PREFIX}:{digest}"

    def _atomic_allow(self, dimensions, now):
        if settings.REDIS_URL:
            args = [str(int(now * 1000)), f"{int(now * 1000)}:{secrets.token_hex(12)}"]
            for _key, limit, duration in dimensions:
                args.extend((str(int(duration * 1000)), str(limit)))
            try:
                redis_client = get_redis_connection(settings.AUTH_THROTTLE_CACHE_ALIAS)
                result = redis_client.eval(AUTH_THROTTLE_LUA, len(dimensions), *[item[0] for item in dimensions], *args)
                return bool(int(result[0])), max(0, math.ceil(int(result[1]) / 1000))
            except (RedisError, OSError, TypeError, ValueError) as error:
                logger.error("Authentication throttle Redis operation failed: %s", error.__class__.__name__)
                if settings.AUTH_THROTTLE_FAIL_CLOSED:
                    raise AuthThrottleUnavailable() from error
        elif settings.AUTH_THROTTLE_FAIL_CLOSED:
            logger.error("Authentication throttle Redis is not configured in fail-closed mode")
            raise AuthThrottleUnavailable()
        return self._development_atomic_allow(dimensions, now)

    @staticmethod
    def _development_atomic_allow(dimensions, now):
        """Thread-safe single-process fallback used only in explicit development/test mode."""
        with _development_lock:
            histories = []
            retry_after = 0
            for key, limit, duration in dimensions:
                history = list(cache.get(key, []))
                history = [timestamp for timestamp in history if timestamp > now - duration]
                if len(history) >= limit:
                    retry_after = max(retry_after, math.ceil(history[-1] + duration - now))
                histories.append((key, history, duration, limit))
            if retry_after > 0:
                return False, retry_after
            for key, history, duration, _limit in histories:
                history.insert(0, now)
                cache.set(key, history, duration)
            return True, 0

    def wait(self):
        return self.retry_after or None

    def _account_digest(self, request, view, scope=None):
        self.view = view
        scope = scope or getattr(view, self.scope_attr, None)
        if not scope:
            return None
        values = []
        for field in getattr(view, "throttle_account_fields", ()):
            value = request.data.get(field) if hasattr(request, "data") else None
            if value in (None, ""):
                value = request.query_params.get(field)
            normalized = str(value or "").strip().casefold()
            if normalized:
                values.append(f"{field}:{normalized}")
        if request.user and request.user.is_authenticated:
            values.append(f"user:{request.user.pk}")
        if not values:
            return None
        if not values:
            return None
        return scope, hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()

    def get_cache_dimensions(self, request, view):
        rates = api_settings.DEFAULT_THROTTLE_RATES
        primary_scope = getattr(view, self.scope_attr, None)
        if not primary_scope:
            return []
        scopes = [primary_scope]
        secondary_scope = getattr(view, "secondary_throttle_scope", None)
        if secondary_scope and (request.data.get("otp") or request.data.get("recovery_code")):
            scopes.append(secondary_scope)
        ip_digest = hashlib.sha256((client_ip(request) or "unknown").encode("utf-8")).hexdigest()
        dimensions = []
        for scope in scopes:
            base_rate = rates.get(scope)
            if not base_rate:
                continue
            dimensions.append((
                self.cache_format % {"scope": f"{scope}-ip", "ident": ip_digest},
                rates.get(f"{scope}_ip", base_rate),
            ))
            account_data = self._account_digest(request, view, scope)
            if account_data:
                _scope, account_digest = account_data
                dimensions.append((
                    self.cache_format % {"scope": f"{scope}-account", "ident": account_digest},
                    rates.get(f"{scope}_account", base_rate),
                ))
                combined = hashlib.sha256(f"{ip_digest}:{account_digest}".encode("utf-8")).hexdigest()
                dimensions.append((
                    self.cache_format % {"scope": f"{scope}-combined", "ident": combined},
                    rates.get(f"{scope}_combined", base_rate),
                ))
        return dimensions
