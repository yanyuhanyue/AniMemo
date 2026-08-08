import assert from "node:assert/strict";
import { resolve } from "node:path";

process.env.PLAYWRIGHT_BROWSERS_PATH ||= resolve(".playwright-browsers");
const { chromium } = await import("@playwright/test");

const baseUrl = process.env.AUTH_FOCUS_BASE_URL || "http://127.0.0.1:5174";
const viewport = {
  width: Number(process.env.AUTH_FOCUS_VIEWPORT_WIDTH || 1440),
  height: Number(process.env.AUTH_FOCUS_VIEWPORT_HEIGHT || 900),
};

async function inspectLeadingIcon(input) {
  const icon = input.locator("xpath=../*[name()='svg'][1]");
  await icon.waitFor({ state: "visible" });
  return icon.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return {
      hitTag: hit?.tagName || null,
      iconOwnsPoint: hit === element || element.contains(hit),
      opacity: getComputedStyle(element).opacity,
      visibility: getComputedStyle(element).visibility,
    };
  });
}

async function assertIconSurvivesFocus(page, placeholder) {
  const input = page.getByPlaceholder(placeholder);
  await input.waitFor({ state: "visible" });
  const icon = input.locator("xpath=../*[name()='svg'][1]");
  const before = await inspectLeadingIcon(input);
  assert.equal(before.iconOwnsPoint, true, `${placeholder}: icon is covered before focus (${JSON.stringify(before)})`);
  await icon.click();
  await page.waitForTimeout(80);
  assert.equal(await input.evaluate((element) => document.activeElement === element), true, `${placeholder}: icon click did not focus the input`);
  const after = await inspectLeadingIcon(input);
  assert.equal(after.iconOwnsPoint, true, `${placeholder}: icon is covered after focus (${JSON.stringify(after)})`);
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport });

try {
  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  await assertIconSurvivesFocus(page, "请输入用户名或注册邮箱");
  await assertIconSurvivesFocus(page, "请输入登录密码");
  await page.getByRole("button", { name: "忘记密码？→" }).click();
  await assertIconSurvivesFocus(page, "请输入注册时使用的邮箱");
  if (process.env.AUTH_FOCUS_SCREENSHOT) {
    await page.screenshot({ path: resolve(process.env.AUTH_FOCUS_SCREENSHOT), fullPage: true });
  }
  process.stdout.write(`auth field leading icons remain visible on focus at ${viewport.width}x${viewport.height}\n`);
} finally {
  await browser.close();
}
