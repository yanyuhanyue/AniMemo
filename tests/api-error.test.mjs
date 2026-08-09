import assert from "node:assert/strict";
import test from "node:test";

import { parseApiError, readableApiError } from "../src/lib/api.js";

test("parseApiError exposes stable code, fields, status and retry metadata", () => {
  const parsed = parseApiError({
    response: {
      status: 400,
      data: { code: "validation_error", detail: "请求参数无效。", fields: { title: ["必填"] } },
      headers: {},
    },
  });

  assert.deepEqual(parsed, {
    code: "validation_error",
    detail: "请求参数无效。",
    fields: { title: ["必填"] },
    status: 400,
    retryAfterSeconds: null,
  });
});

test("readableApiError keeps rate-limit feedback human-friendly", () => {
  assert.equal(
    readableApiError({
      response: {
        status: 429,
        data: { code: "rate_limited", detail: "访问频率过高。", retry_after_seconds: 9 },
        headers: {},
      },
    }),
    "操作过于频繁，请在 9 秒后重试。",
  );
});
