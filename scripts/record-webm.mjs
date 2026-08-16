import { spawn } from "node:child_process";
import { copyFile, mkdir, readdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { resolveFixedLoopbackOrigin } from "./qa-origin.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const qaDir = path.join(projectRoot, "qa");
const videoTempRoot = path.join(qaDir, ".playwright-video");
const browsersPath = path.join(projectRoot, ".playwright-browsers");
const baseUrl = resolveFixedLoopbackOrigin(process.env.QA_BASE_URL, 5173, "QA_BASE_URL");
const viewport = { width: 1440, height: 900 };

process.env.PLAYWRIGHT_BROWSERS_PATH = browsersPath;
const { chromium } = await import("playwright");

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function serverResponds() {
  try {
    const response = await fetch(baseUrl, { signal: AbortSignal.timeout(1200) });
    return response.ok;
  } catch {
    return false;
  }
}

async function waitForServer(timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await serverResponds()) return;
    await delay(250);
  }
  throw new Error(`Vite did not become ready at ${baseUrl}`);
}

async function startServerWhenNeeded() {
  if (await serverResponds()) return null;

  const viteCli = path.join(projectRoot, "node_modules", "vite", "bin", "vite.js");
  const server = spawn(process.execPath, [viteCli, "--host", "127.0.0.1", "--port", "5173"], {
    cwd: projectRoot,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  let startupOutput = "";
  server.stdout.on("data", (chunk) => { startupOutput += chunk.toString(); });
  server.stderr.on("data", (chunk) => { startupOutput += chunk.toString(); });

  try {
    await waitForServer();
    return server;
  } catch (error) {
    server.kill();
    throw new Error(`${error.message}\n${startupOutput.trim()}`);
  }
}

async function waitForPage(page, route) {
  await page.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});

  if (route === "/") {
    await page.locator(".showcase-page.is-ready").waitFor({ state: "visible", timeout: 20000 });
    await page.locator(".app-boot-loader").waitFor({ state: "detached", timeout: 5000 }).catch(() => {});
  }

  await page.waitForTimeout(500);
}

async function openFeaturedModal(page) {
  const card = page.locator('.featured-card[role="button"]').first();
  await card.waitFor({ state: "visible", timeout: 15000 });
  await card.scrollIntoViewIfNeeded();
  await card.hover();
  await page.waitForTimeout(350);
  await card.click();
  await page.getByRole("dialog").waitFor({ state: "visible", timeout: 10000 });
}

async function openHomeListModal(page) {
  const row = page.locator('.anime-list-row[role="button"]').first();
  await row.waitFor({ state: "visible", timeout: 15000 });
  await row.scrollIntoViewIfNeeded();
  await row.hover();
  await page.waitForTimeout(450);
  await row.click();
  await page.getByRole("dialog").waitFor({ state: "visible", timeout: 10000 });
}

async function closeModal(page) {
  const closeButton = page.locator(".featured-anime-modal__close-top");
  await closeButton.waitFor({ state: "visible", timeout: 10000 });
  await closeButton.click();
  await page.getByRole("dialog").waitFor({ state: "detached", timeout: 10000 });
}

function modalTab(page, label) {
  return page.locator(".featured-anime-modal__tabs button").filter({ hasText: label });
}

const scenarios = {
  "home-to-featured": {
    file: "qa-featured-three-color-transition.webm",
    run: async (page) => {
      await waitForPage(page, "/");
      const featuredEntry = page.locator(".hero-action--featured");
      await featuredEntry.waitFor({ state: "visible", timeout: 10000 });
      await featuredEntry.hover();
      await page.waitForTimeout(250);
      await featuredEntry.click();
      await page.waitForURL("**/featured", { timeout: 10000 });
      await page.locator(".featured-page").waitFor({ state: "visible", timeout: 10000 });
      await page.waitForTimeout(1500);
    },
  },
  "featured-open": {
    file: "qa-featured-modal-open.webm",
    run: async (page) => {
      await waitForPage(page, "/featured");
      await openFeaturedModal(page);
      await page.waitForTimeout(1700);
      await closeModal(page);
      await page.waitForTimeout(600);
    },
  },
  "featured-tabs": {
    file: "qa-featured-tab-switch.webm",
    run: async (page) => {
      await waitForPage(page, "/featured");
      await openFeaturedModal(page);
      await page.waitForTimeout(900);
      await modalTab(page, "个人评价").click();
      await page.waitForTimeout(850);
      await modalTab(page, "剧情简介").click();
      await page.waitForTimeout(900);
    },
  },
  "featured-tabs-rapid": {
    file: "qa-featured-tab-switch-rapid.webm",
    run: async (page) => {
      await waitForPage(page, "/featured");
      await openFeaturedModal(page);
      await page.waitForTimeout(800);
      const reviewTab = modalTab(page, "个人评价");
      const summaryTab = modalTab(page, "剧情简介");
      for (let index = 0; index < 5; index += 1) {
        await reviewTab.click();
        await page.waitForTimeout(90);
        await summaryTab.click();
        await page.waitForTimeout(90);
      }
      await page.waitForTimeout(900);
    },
  },
  "home-list-open": {
    file: "qa-home-list-modal-open.webm",
    run: async (page) => {
      await waitForPage(page, "/");
      await openHomeListModal(page);
      await page.waitForTimeout(1500);
      await modalTab(page, "个人评价").click();
      await page.waitForTimeout(850);
      await closeModal(page);
      await page.waitForTimeout(500);
    },
  },
  "home-catalog-motion": {
    file: "qa-home-catalog-motion-after.webm",
    run: async (page) => {
      await waitForPage(page, "/");
      const results = page.locator("#anime-results");
      await results.scrollIntoViewIfNeeded();
      await page.waitForTimeout(900);

      const gridToggle = page.locator("#btnViewGrid");
      await gridToggle.click();
      await page.waitForTimeout(900);

      await page.mouse.wheel(0, 520);
      await page.waitForTimeout(900);

      const listToggle = page.locator("#btnViewList");
      await listToggle.click();
      await page.waitForTimeout(900);
    },
  },
  "auth-login-switch": {
    file: "qa-auth-login-register-switch.webm",
    viewport: { width: 2048, height: 1024 },
    run: async (page) => {
      await page.goto(`${baseUrl}/login`, { waitUntil: "domcontentloaded" });
      await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
      await page.waitForTimeout(1450);

      await page.getByRole("tab", { name: "注册", exact: true }).click();
      await page.waitForTimeout(1250);
      await page.getByRole("tab", { name: "登录", exact: true }).click();
      await page.waitForTimeout(1250);
    },
  },
  "auth-staff-transition": {
    file: "qa-auth-staff-transition.webm",
    viewport: { width: 2048, height: 1024 },
    run: async (page) => {
      await page.goto(`${baseUrl}/login`, { waitUntil: "domcontentloaded" });
      await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
      await page.waitForTimeout(1450);

      const staffButton = page.getByRole("button", { name: "STAFF ONLY", exact: true });
      await staffButton.hover();
      await page.waitForTimeout(300);
      await staffButton.click();
      await page.waitForURL("**/admin-login", { timeout: 10000 });
      await page.waitForTimeout(1350);
    },
  },
};

async function recordScenario(browser, name, scenario) {
  const tempDir = path.join(videoTempRoot, name);
  const outputPath = path.join(qaDir, scenario.file);
  await rm(tempDir, { recursive: true, force: true });
  await mkdir(tempDir, { recursive: true });
  await rm(outputPath, { force: true });

  const scenarioViewport = scenario.viewport || viewport;
  const context = await browser.newContext({
    viewport: scenarioViewport,
    recordVideo: { dir: tempDir, size: scenarioViewport },
  });
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  try {
    await scenario.run(page);
  } finally {
    await page.close();
    await context.close();
  }

  const recordedFiles = (await readdir(tempDir)).filter((file) => file.endsWith(".webm"));
  if (recordedFiles.length !== 1) {
    throw new Error(`${name}: expected one WebM file, found ${recordedFiles.length}`);
  }

  await copyFile(path.join(tempDir, recordedFiles[0]), outputPath);
  await rm(tempDir, { recursive: true, force: true });

  if (pageErrors.length) {
    throw new Error(`${name}: page errors detected: ${pageErrors.join(" | ")}`);
  }

  console.log(`Recorded ${name}: ${outputPath}`);
}

const requestedNames = process.argv.slice(2);
const selectedNames = requestedNames.length ? requestedNames : Object.keys(scenarios);
const unknownNames = selectedNames.filter((name) => !scenarios[name]);
if (unknownNames.length) {
  throw new Error(`Unknown scenario(s): ${unknownNames.join(", ")}`);
}

await mkdir(qaDir, { recursive: true });
await mkdir(videoTempRoot, { recursive: true });

const localServer = await startServerWhenNeeded();
let browser;

try {
  browser = await chromium.launch({ headless: true });
  for (const name of selectedNames) {
    await recordScenario(browser, name, scenarios[name]);
  }
} finally {
  await browser?.close();
  if (localServer) localServer.kill();
  await rm(videoTempRoot, { recursive: true, force: true });
}
