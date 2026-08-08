import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const browsersPath = path.join(projectRoot, ".playwright-browsers");
const playwrightCli = path.join(projectRoot, "node_modules", "playwright", "cli.js");

await mkdir(browsersPath, { recursive: true });

const installer = spawn(process.execPath, [playwrightCli, "install", "chromium"], {
  cwd: projectRoot,
  env: {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: browsersPath,
    npm_config_cache: path.join(projectRoot, ".npm-cache"),
  },
  stdio: "inherit",
  windowsHide: true,
});

installer.on("error", (error) => {
  console.error(`Unable to start Playwright installer: ${error.message}`);
  process.exitCode = 1;
});

installer.on("exit", (code) => {
  if (code !== 0) process.exitCode = code ?? 1;
});
