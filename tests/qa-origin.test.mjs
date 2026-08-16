import test from "node:test";
import assert from "node:assert/strict";

import { resolveFixedLoopbackOrigin } from "../scripts/qa-origin.mjs";

test("accepts only the canonical loopback QA origin", () => {
  assert.equal(resolveFixedLoopbackOrigin(undefined, 5173, "QA_BASE_URL"), "http://127.0.0.1:5173");
  assert.equal(resolveFixedLoopbackOrigin("http://127.0.0.1:5173", 5173, "QA_BASE_URL"), "http://127.0.0.1:5173");
  assert.equal(resolveFixedLoopbackOrigin("http://127.0.0.1:5173/", 5173, "QA_BASE_URL"), "http://127.0.0.1:5173");
  assert.equal(resolveFixedLoopbackOrigin(undefined, 4174, "AUTH_FOCUS_BASE_URL"), "http://127.0.0.1:4174");
});

test("rejects origins outside the fixed QA trust boundary", () => {
  const rejected = [
    "https://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:4174",
    "http://user:password@127.0.0.1:5173",
    "http://127.0.0.1:5173/unexpected",
    "http://127.0.0.1:5173/?target=remote",
    "http://127.0.0.1:5173/#remote",
    "http://example.invalid:5173",
    "not-a-url",
  ];

  for (const value of rejected) {
    assert.throws(
      () => resolveFixedLoopbackOrigin(value, 5173, "QA_BASE_URL"),
      /QA_BASE_URL must be exactly http:\/\/127\.0\.0\.1:5173/,
      value,
    );
  }
});
