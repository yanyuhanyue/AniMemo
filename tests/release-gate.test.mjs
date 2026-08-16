import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("production deployment has one environment template", () => {
  assert.equal(existsSync(new URL("../.env.production.example", import.meta.url)), false);
  assert.equal(existsSync(new URL("../deploy/.env.production.example", import.meta.url)), false);
  const compose = read("../deploy/docker-compose.yml");
  assert.match(compose, /\/run\/animemo-updater\/managed\.env/);
  assert.doesNotMatch(compose, /\.env\.production/);
});

test("API healthcheck is secure-forwarded and validates status and body", () => {
  const compose = read("../deploy/docker-compose.yml");
  assert.match(compose, /'X-Forwarded-Proto': 'https'/);
  assert.match(compose, /response\.status == 200/);
  assert.match(compose, /payload\.get\('status'\) == 'ok'/);
});

test("production frontend is a generic artifact without instance Turnstile build configuration", () => {
  const productionCompose = read("../deploy/docker-compose.yml");
  const buildCompose = read("../deploy/docker-compose.build.yml");
  const dockerfile = read("../deploy/frontend.Dockerfile");
  assert.doesNotMatch(productionCompose, /VITE_TURNSTILE_SITE_KEY/);
  assert.doesNotMatch(buildCompose, /VITE_TURNSTILE_SITE_KEY|TURNSTILE_ENABLED|TURNSTILE_SECRET/);
  assert.doesNotMatch(dockerfile, /VITE_TURNSTILE_SITE_KEY|TURNSTILE_ENABLED|TURNSTILE_SECRET/);
  assert.doesNotMatch(dockerfile, /TURNSTILE_SECRET/);
});

test("legacy deploy, smoke, proxy, and certificate entrypoints are removed", () => {
  assert.equal(existsSync(new URL("../deploy/create-admin.sh", import.meta.url)), false);
  for (const path of [
    "../deploy/deploy.sh",
    "../deploy/smoke-test.sh",
    "../deploy/openresty-animemo.conf",
    "../deploy/animemo-certbot.cron",
  ]) {
    assert.equal(existsSync(new URL(path, import.meta.url)), false, path);
  }
});

test("operator docs use the browser first-run flow", () => {
  const readme = readFileSync(new URL("../README.md", import.meta.url), "utf8");
  const localDevelopment = readFileSync(new URL("../docs/local-development.md", import.meta.url), "utf8");

  assert.match(readme, /docs\/first-run-bootstrap\.md/);
  assert.doesNotMatch(readme, /deploy\/deploy\.sh|--fresh|animemo-initial-admin/);
  assert.match(localDevelopment, /\/setup/);
  assert.doesNotMatch(localDevelopment, /createsuperuser/);
});
