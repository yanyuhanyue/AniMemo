import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import test from "node:test";

import {
  formatLocalDateTime,
  translateError,
  translateStatus,
} from "../bridges/astrbot_plugin_animemo_bridge/pages/status/app.js";

const appUrl = new URL("../bridges/astrbot_plugin_animemo_bridge/pages/status/app.js", import.meta.url);
const pageUrl = new URL("../bridges/astrbot_plugin_animemo_bridge/pages/status/index.html", import.meta.url);

function formatInTimezone(timezone, value) {
  const script = [
    `import { formatLocalDateTime } from ${JSON.stringify(appUrl.href)};`,
    `process.stdout.write(formatLocalDateTime(${JSON.stringify(value)}));`,
  ].join("\n");
  const result = spawnSync(process.execPath, ["--input-type=module", "--eval", script], {
    encoding: "utf8",
    env: { ...process.env, TZ: timezone },
  });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}

test("Diagnostics 使用浏览器本地时区并动态显示 UTC offset", () => {
  const value = "2026-08-10T06:53:36+00:00";
  assert.equal(formatInTimezone("Asia/Shanghai", value), "2026-08-10 14:53:36 (UTC+08:00)");
  assert.equal(formatInTimezone("America/New_York", value), "2026-08-10 02:53:36 (UTC-04:00)");
});

test("Diagnostics 时间格式异常时安全回退", () => {
  assert.equal(formatLocalDateTime(null), "未运行");
  assert.equal(formatLocalDateTime(undefined), "未运行");
  assert.equal(formatLocalDateTime(""), "未运行");
  assert.equal(formatLocalDateTime("not-a-timestamp"), "not-a-timestamp");
});

test("Diagnostics 状态值中文化且未知值保留原文", () => {
  assert.equal(translateStatus("RUNNING"), "运行中");
  assert.equal(translateStatus("STOPPED"), "已停止");
  assert.equal(translateStatus("OK"), "正常");
  assert.equal(translateStatus("NONE"), "无");
  assert.equal(translateStatus("NOT RUN"), "未运行");
  assert.equal(translateStatus("SOMETHING_NEW"), "SOMETHING_NEW");
  assert.equal(translateStatus(null), "无");
  assert.equal(translateStatus(undefined), "无");
  assert.equal(translateStatus(""), "无");
});

test("Diagnostics 错误翻译保留异常类型诊断价值", () => {
  assert.equal(translateError("BridgeAuthError"), "认证错误（BridgeAuthError）");
  assert.equal(translateError("BridgeConnectionError"), "连接错误（BridgeConnectionError）");
  assert.equal(translateError("BridgeEventError"), "事件错误（BridgeEventError）");
  assert.equal(translateError("BridgeProtocolError"), "协议错误（BridgeProtocolError）");
  assert.equal(translateError("BridgeRateLimitError"), "限流错误（BridgeRateLimitError）");
  assert.equal(translateError("FutureBridgeError"), "FutureBridgeError");
  assert.equal(translateError("NONE"), "无");
});

test("Diagnostics 页面保持中文展示与凭证隔离", () => {
  const script = readFileSync(appUrl, "utf8");
  const html = readFileSync(pageUrl, "utf8");
  for (const legacyLabel of [
    "Server",
    "Event poller",
    "Current route",
    "HMAC connectivity",
    "Last successful poll",
    "Last error",
  ]) {
    assert.ok(!script.includes(legacyLabel), `发现未中文化文案：${legacyLabel}`);
    assert.ok(!html.includes(legacyLabel), `发现未中文化文案：${legacyLabel}`);
  }
  for (const forbidden of [
    "document.cookie",
    "localStorage",
    "pairing_code",
    "hmac_signature",
    ".secret",
    "Asia/Shanghai",
    "UTC+08:00",
    "28800",
    "timeZone:",
  ]) {
    assert.ok(!script.includes(forbidden), `Diagnostics 不得读取或展示：${forbidden}`);
  }
});
