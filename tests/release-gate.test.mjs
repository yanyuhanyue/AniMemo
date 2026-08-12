import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("production deployment has one environment template", () => {
  assert.equal(existsSync(new URL("../.env.production.example", import.meta.url)), true);
  assert.equal(existsSync(new URL("../deploy/.env.production.example", import.meta.url)), false);
  assert.match(read("../.env.production.example"), /^DATABASE_SSL_REQUIRE=false$/m);
});

test("API healthcheck is secure-forwarded and validates status and body", () => {
  const compose = read("../deploy/docker-compose.yml");
  assert.match(compose, /'X-Forwarded-Proto': 'https'/);
  assert.match(compose, /response\.status == 200/);
  assert.match(compose, /payload\.get\('status'\) == 'ok'/);
});

test("production frontend receives only the public Turnstile build value", () => {
  const productionCompose = read("../deploy/docker-compose.yml");
  const buildCompose = read("../deploy/docker-compose.build.yml");
  const dockerfile = read("../deploy/frontend.Dockerfile");
  assert.doesNotMatch(productionCompose, /VITE_TURNSTILE_SITE_KEY/);
  assert.match(buildCompose, /VITE_TURNSTILE_SITE_KEY: \$\{VITE_TURNSTILE_SITE_KEY:\?VITE_TURNSTILE_SITE_KEY is required\}/);
  assert.match(dockerfile, /ARG VITE_TURNSTILE_SITE_KEY/);
  assert.doesNotMatch(dockerfile, /TURNSTILE_SECRET/);
});

test("smoke test requires HTTP 200 and valid health JSON", () => {
  const smoke = read("../deploy/smoke-test.sh");
  assert.match(smoke, /\[ "\$HTTP_STATUS" != "200" \]/);
  assert.match(smoke, /payload\.get\('status'\) == 'ok'/);
  assert.match(smoke, /X-Forwarded-Proto: https/);
});

test("legacy ZIP deployer is explicit bootstrap or break-glass recovery", () => {
  const deploy = read("../deploy/deploy.sh");
  assert.equal(existsSync(new URL("../deploy/create-admin.sh", import.meta.url)), false);
  assert.match(deploy, /DEFAULT_APP_ROOT=\/opt\/1panel\/docker\/compose\/anime-journal\/app/);
  assert.match(deploy, /DEFAULT_DATA_ROOT=\/data\/anime-journal/);
  assert.match(deploy, /--bootstrap/);
  assert.match(deploy, /--break-glass/);
  assert.match(deploy, /normal updates use the AniMemo Update Agent/);
  assert.match(deploy, /--reset-data/);
  assert.match(deploy, /--reset-data requires --bootstrap/);
  assert.match(deploy, /--create-admin[\s\S]*removed[\s\S]*\/setup/);
  assert.match(deploy, /tr -d '\\r' < "\$SHA_FILE"/);
  assert.match(deploy, /ARCHIVE%\.zip/);
  assert.match(deploy, /ARCHIVE_BACKEND=python3/);
  assert.match(deploy, /from zipfile import ZipFile/);
  assert.match(deploy, /deploy\/docker-compose\.build\.yml/);
  assert.match(deploy, /run --rm --no-deps migration/);
  assert.match(deploy, /run --rm --no-deps bootstrap/);
  assert.match(deploy, /up -d --no-deps --force-recreate api web/);
  assert.match(deploy, /docker volume rm anime-journal-data/);
  assert.doesNotMatch(deploy, /stage_compose down/);
  assert.doesNotMatch(deploy, /STACK_STOPPED/);
  assert.doesNotMatch(deploy, /docker\s+(?:system|volume)\s+prune/);
  assert.doesNotMatch(deploy, /docker compose .*down .*--volumes/);
  assert.match(deploy, /re-anime\.cc\.conf/);
  assert.doesNotMatch(deploy, /createsuperuser|get_or_create\(username|ANIME_JOURNAL_ADMIN_PASSWORD/);
});

test("operator docs use the browser first-run flow and current deploy modes", () => {
  const readme = readFileSync(new URL("../README.md", import.meta.url), "utf8");
  const localDevelopment = readFileSync(new URL("../docs/local-development.md", import.meta.url), "utf8");

  assert.match(readme, /docs\/first-run-bootstrap\.md/);
  assert.match(readme, /--bootstrap/);
  assert.doesNotMatch(readme, /--fresh|anime-journal-initial-admin/);
  assert.match(localDevelopment, /\/setup/);
  assert.doesNotMatch(localDevelopment, /createsuperuser/);
});
