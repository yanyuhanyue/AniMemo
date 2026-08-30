import assert from "node:assert/strict";
import test from "node:test";

import { parseApiError, readableApiError } from "../src/lib/apiCore.js";

test("parseApiError exposes the strict public failure contract", () => {
  const parsed = parseApiError({
    response: {
      status: 400,
      data: {
        code: "validation_error",
        detail: "请求参数无效。",
        correlation_id: "a".repeat(32),
        fields: { title: ["must-not-be-consumed"] },
      },
      headers: {},
    },
  });

  assert.deepEqual(parsed, {
    code: "validation_error",
    detail: "请求参数无效。",
    correlationId: "a".repeat(32),
    status: 400,
    retryAfterSeconds: null,
  });
});

test("readableApiError keeps rate-limit feedback human-friendly", () => {
  assert.equal(
    readableApiError({
      response: {
        status: 429,
        data: {
          code: "rate_limited",
          detail: "操作过于频繁，请稍后重试。",
          correlation_id: "b".repeat(32),
          retry_after_seconds: 99,
        },
        headers: { "retry-after": "9" },
      },
    }),
    "操作过于频繁，请在 9 秒后重试。",
  );
});

test("readableApiError uses the stable CSRF code", () => {
  assert.equal(
    readableApiError({
      response: {
        status: 403,
        data: {
          code: "csrf_failed",
          detail: "安全验证已过期，请刷新页面后重试。",
          correlation_id: "c".repeat(32),
        },
        headers: {},
      },
    }),
    "安全验证已过期，请刷新页面后重试。",
  );
});

test("readableApiError preserves the scoped updater unavailable detail", () => {
  assert.equal(
    readableApiError({
      response: {
        status: 503,
        data: {
          code: "updater_unavailable",
          detail: "系统更新服务暂时不可用，请联系服务器管理员。",
          correlation_id: "d".repeat(32),
        },
        headers: {},
      },
    }),
    "系统更新服务暂时不可用，请联系服务器管理员。",
  );
});

test("parseApiError ignores malformed correlation and non-contract extras", () => {
  assert.deepEqual(
    parseApiError({
      response: {
        status: 503,
        data: {
          code: "service_unavailable",
          detail: "服务暂时不可用，请稍后重试。",
          correlation_id: "client-controlled",
          metadata: { traceback: "must-not-be-consumed" },
        },
        headers: {},
      },
    }),
    {
      code: "service_unavailable",
      detail: "服务暂时不可用，请稍后重试。",
      correlationId: null,
      status: 503,
      retryAfterSeconds: null,
    },
  );
});
