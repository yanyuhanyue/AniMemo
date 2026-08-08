import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase
from django_redis import get_redis_connection

from .throttling import HashedAccountRateThrottle


@skipUnless(settings.REDIS_URL, "需要真实 Redis 才能验证认证限流原子性。")
class RedisAtomicThrottleTests(SimpleTestCase):
    def setUp(self):
        self.redis = get_redis_connection(settings.AUTH_THROTTLE_CACHE_ALIAS)
        keys = list(self.redis.scan_iter(f"{settings.AUTH_THROTTLE_KEY_PREFIX}:test:*"))
        if keys:
            self.redis.delete(*keys)

    def tearDown(self):
        self.setUp()

    def test_concurrent_window_never_allows_more_than_limit(self):
        key = f"{settings.AUTH_THROTTLE_KEY_PREFIX}:test:concurrent"
        dimensions = [(key, 7, 60)]
        barrier = Barrier(50)

        def attempt(_index):
            barrier.wait()
            return HashedAccountRateThrottle()._atomic_allow(dimensions, time.time())[0]

        with ThreadPoolExecutor(max_workers=50) as executor:
            results = list(executor.map(attempt, range(50)))
        self.assertEqual(sum(results), 7)
        self.assertEqual(self.redis.zcard(key), 7)
        self.assertGreater(self.redis.pttl(key), 0)

    def test_same_millisecond_members_are_unique_and_window_expires(self):
        key = f"{settings.AUTH_THROTTLE_KEY_PREFIX}:test:same-ms"
        dimensions = [(key, 20, 0.2)]
        now = int(time.time() * 1000) / 1000
        results = [HashedAccountRateThrottle()._atomic_allow(dimensions, now)[0] for _index in range(20)]
        self.assertEqual(sum(results), 20)
        self.assertEqual(self.redis.zcard(key), 20)
        denied, retry_after = HashedAccountRateThrottle()._atomic_allow(dimensions, now)
        self.assertFalse(denied)
        self.assertGreaterEqual(retry_after, 1)
        time.sleep(0.25)
        allowed, _retry_after = HashedAccountRateThrottle()._atomic_allow(dimensions, time.time())
        self.assertTrue(allowed)
