import assert from "node:assert/strict";
import { randomBytes, randomUUID } from "node:crypto";
import { readFile, writeFile, mkdir, cp } from "node:fs/promises";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  installInstance,
  updateInstance,
  backupInstance,
  instanceStatus,
  removeTestInstance,
  restorePlan,
} from "./instance.mjs";
import { runProcess } from "./process.mjs";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
async function port() {
  const s = createServer();
  await new Promise((r, j) => {
    s.once("error", j);
    s.listen(0, "127.0.0.1", r);
  });
  const p = s.address().port;
  await new Promise((r) => s.close(r));
  return p;
}
function client(origin) {
  let cookie = "";
  return async (method, url, data, want = 200, binary = false) => {
    const r = await fetch(origin + url, {
      method,
      headers: {
        Origin: origin,
        Cookie: cookie,
        ...(data === undefined
          ? {}
          : { "Content-Type": binary ? "image/png" : "application/json" }),
      },
      body:
        data === undefined ? undefined : binary ? data : JSON.stringify(data),
      signal: AbortSignal.timeout(15000),
    });
    const c = r.headers.get("set-cookie");
    if (c) cookie = c.split(";")[0];
    const bytes = Buffer.from(await r.arrayBuffer());
    assert.equal(
      r.status,
      want,
      `${method} ${url}: ${r.status} ${r.status === want ? "" : bytes.toString().slice(0, 300)}`,
    );
    return r.headers.get("content-type")?.includes("json")
      ? JSON.parse(bytes)
      : bytes;
  };
}
async function config(name) {
  return JSON.parse(
    await readFile(
      path.join(root, ".local/instances", name, "config.json"),
      "utf8",
    ),
  );
}
export async function memoryInstanceSmoke({ image, previousImage = image }) {
  if (!image)
    throw Error("Set ANIMEMO_CANDIDATE_IMAGE to a local candidate image.");
  const names = [0, 1].map(() => `probe-${randomBytes(6).toString("hex")}`),
    created = [];
  const report = {
    status: "RUNNING",
    started_at: new Date().toISOString(),
    image,
    previous_image: previousImage,
    checks: [],
  };
  const mark = (s) => {
    report.checks.push(s);
    console.log(`Memory instance: ${s}`);
  };
  try {
    created.push(names[0]);
    const first = await installInstance({
      name: names[0],
      image: previousImage,
      port: await port(),
    });
    let cfg = await config(names[0]);
    report.previous_image = cfg.image;
    const credentials = {
      email: `memory-probe-${randomUUID()}@example.test`,
      password: `test-${randomUUID()}`,
    };
    let call = client(first.origin);
    const user = await call(
      "POST",
      "/api/v1/setup",
      { ...credentials, display_name: "记忆恢复验收", token: cfg.setupToken },
      201,
    );
    const entry = await call(
      "POST",
      "/api/v1/entries",
      { title: "升级前的故事", total_episodes: 12 },
      201,
    );
    await call(
      "POST",
      `/api/v1/entries/${entry.id}/history`,
      {
        request_id: randomUUID(),
        watched_on: "2026-10-07",
        episode_from: 1,
        episode_to: 3,
        note: "升级前的观看",
      },
      201,
    );
    const remembered = await call(
      "POST",
      "/api/v1/entries",
      { title: "只记得看过的旧番", notes: "高中时看过" },
      201,
    );
    assert.equal(remembered.status, "recorded");
    assert.equal(remembered.watched_episodes, 0);
    assert.equal(
      (await call("GET", `/api/v1/entries/${remembered.id}/history`)).items
        .length,
      0,
    );
    const character = await call(
      "POST",
      "/api/v1/memory/characters",
      {
        name: "合成角色",
        aliases: ["别名"],
        anime_ids: [entry.anime_id],
        favorite: true,
      },
      201,
    );
    const episode = await call(
      "POST",
      "/api/v1/memory/episodes",
      { anime_id: entry.anime_id, title: "归途", number: 1 },
      201,
    );
    const png = Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMISNnyHwAE2AJoEeqx4gAAAABJRU5ErkJggg==",
      "base64",
    );
    const media = await call(
      "POST",
      "/api/v1/memory/media",
      { byte_size: png.length },
      201,
    );
    await call("PUT", `/api/v1/memory/media/${media.id}`, png, 200, true);
    const note = await call(
      "POST",
      "/api/v1/memory/notes",
      {
        kind: "moment",
        title: "会留下的光",
        body: "恢复前的文字",
        anime_id: entry.anime_id,
        character_id: character.id,
        episode_id: episode.id,
        media_ids: [media.id],
        occurred_on: "2026-10",
        time_precision: "month",
        visibility: "unlisted",
      },
      201,
    );
    const yearly = await call(
      "POST",
      "/api/v1/memory/yearly",
      {
        year: 2026,
        timezone: "Asia/Shanghai",
        title: "冻结的 2026",
        note_ids: [note.id],
        visibility: "unlisted",
      },
      201,
    );
    await call(
      "POST",
      "/api/v1/memory/collections",
      {
        title: "共同的小册",
        items: [
          { kind: "character", id: character.id },
          { kind: "moment", id: note.id },
        ],
      },
      201,
    );
    // Seed the complete library on the old image, not after the upgrade.
    // This baseline requires the 1.3 RC2 (or newer) recording API.
    let beforeUpgrade;
    for (let n = 0; n < 100; n++) {
      beforeUpgrade = await call("GET", "/api/v1/export");
      if (beforeUpgrade.library.achievements.unlocks.length >= 2) break;
      assert.ok(n < 99, "baseline worker did not award the seeded memories");
      await new Promise((r) => setTimeout(r, 100));
    }
    if (previousImage !== image) {
      const previousConfig = cfg;
      await updateInstance(names[0], image);
      cfg = await config(names[0]);
      assert.notEqual(cfg.image, previousConfig.image, "upgrade baseline must be a different image");
      call = client(first.origin);
      await call("POST", "/api/v1/auth/login", credentials);
      const upgraded = await call("GET", "/api/v1/export");
      assert.deepEqual(upgraded.entries, beforeUpgrade.entries);
      for (const key of Object.keys(beforeUpgrade.library)) {
        if (key !== "achievements")
          assert.deepEqual(upgraded.library[key], beforeUpgrade.library[key], `upgrade changed ${key}`);
      }
      for (const unlock of beforeUpgrade.library.achievements.unlocks)
        assert.deepEqual(upgraded.library.achievements.unlocks.find((u) => u.unlock_id === unlock.unlock_id), unlock);
      assert.deepEqual(await call("GET", `/api/v1/memory/media/${media.id}`), png);
      mark("different-image upgrade preserves existing records, full memory library, private image bytes and historical achievement revisions");
    } else {
      mark("first-release installation baseline; no cross-version upgrade claimed");
    }
    report.image = cfg.image;
    const art = await call("POST", "/api/v1/admin/achievements/images", png, 201, true);
    const artBytes = await call("GET", `/api/v1/memory/achievement-images/${art.id}`);
    const customRule = await call("PUT", "/api/v1/admin/achievements/rules", {
      series_id: `restore-art-${randomUUID()}`,
      series_title: "恢复验收图案",
      tier: 3,
      title: "升级后自动获得的纪念",
      description: "合成恢复验证",
      badge: "book",
      badge_image_id: art.id,
      metric: "recorded_anime",
      threshold: 1,
      active: true,
    });
    // Wait before starting manual backfill: publication itself must wake the
    // already-qualified idle account through the real worker.
    for (let n = 0; n < 100; n++) {
      const awarded = (await call("GET", "/api/v1/export")).library.achievements.unlocks.find((u) => u.rule.id === customRule.id);
      if (awarded) {
        assert.equal(awarded.source, "automatic");
        assert.equal(awarded.rule.badge_image_id, art.id);
        await call("PUT", "/api/v1/memory/achievements/showcase", { unlock_ids: [awarded.unlock_id] }, 204);
        break;
      }
      assert.ok(n < 99, "new image rule did not automatically award an existing user");
      await new Promise((r) => setTimeout(r, 100));
    }
    mark("publishing an uploaded badge automatically awards an idle eligible account and preserves its showcase selection");
    const backfill = await call(
      "POST",
      "/api/v1/admin/achievements/backfills",
      undefined,
      201,
    );
    await call("POST", `/api/v1/admin/achievements/backfills/${backfill.id}`, {
      action: "run",
    });
    const plugins = (await call("GET", "/api/v1/admin/plugins")).items;
    const converter = plugins.find(
      (p) =>
        p.manifest.slug === "watch-history-text" &&
        p.manifest.version === "1.1.1",
    );
    assert.ok(converter);
    await call("POST", "/api/v1/admin/plugins/watch-history-text", {
      action: "activate",
      version: converter.manifest.version,
      revision: converter.revision,
    });
    let original;
    for (let n = 0; n < 80; n++) {
      original = await call("GET", "/api/v1/export");
      if (
        original.library?.achievements.unlocks.length >= 2 &&
        (await call("GET", "/api/v1/admin/achievements/backfills")).items.find(
          (j) => j.id === backfill.id,
        )?.state === "done"
      )
        break;
      assert.ok(n < 79, "achievement worker did not project");
      await new Promise((r) => setTimeout(r, 100));
    }
    // The export read above can precede the backfill's final commit. Freeze the
    // test worker after its queue drains, then capture the actual backup baseline.
    assert.equal(await runProcess("docker", ["exec", `${cfg.project}-db-1`, "psql", "-U", "animemo", "-d", cfg.database, "-Atc", "SELECT count(*) FROM achievement_pending"], { capture: true }), "0");
    await runProcess("docker", ["stop", "-t", "15", `${cfg.project}-worker-1`]);
    original = await call("GET", "/api/v1/export");
    assert.equal(original.schema, "animemo.journal/v3");
    assert.equal(original.library.notes[0].id, note.id);
    mark(
      "real worker projects achievements; private media, frozen annual revision and bundled TXT activation are available in Linux containers",
    );
    const backup = await backupInstance(names[0]);
    // Emulate a backup produced by classic Docker: its image identity is the
    // config digest, even when this daemon exposes an OCI manifest digest.
    const portableBackup = `${backup}-classic`;
    await cp(backup, portableBackup, { recursive: true });
    const manifestFile = path.join(portableBackup, "manifest.json");
    const manifest = JSON.parse(await readFile(manifestFile, "utf8"));
    manifest.image = manifest.image_config;
    delete manifest.image_config;
    await writeFile(manifestFile, JSON.stringify(manifest, null, 2) + "\n");
    assert.equal((await restorePlan({ backup: portableBackup, name: names[1], image })).compatible, true);
    const mismatched = { ...manifest, image: `sha256:${"a".repeat(64)}` };
    await writeFile(manifestFile, JSON.stringify(mismatched, null, 2) + "\n");
    assert.equal((await restorePlan({ backup: portableBackup, name: names[1], image })).compatible, false);
    await writeFile(manifestFile, JSON.stringify(manifest, null, 2) + "\n");
    created.push(names[1]);
    const restored = await installInstance({
      name: names[1],
      backup: portableBackup,
      image,
      port: await port(),
    });
    const read = client(restored.origin);
    await read("POST", "/api/v1/auth/login", credentials);
    const after = await read("GET", "/api/v1/export");
    assert.deepEqual(after.library, original.library);
    assert.deepEqual(await read("GET", `/api/v1/memory/achievement-images/${art.id}`), artBytes);
    const rememberedAfter = after.entries.find((e) => e.id === remembered.id);
    assert.equal(rememberedAfter.status, "recorded");
    assert.equal(rememberedAfter.notes, "高中时看过");
    assert.equal(rememberedAfter.watched_episodes, 0);
    assert.deepEqual(
      await read("GET", `/api/v1/memory/media/${media.id}`),
      png,
    );
    const annual = await read("GET", `/api/v1/memory/yearly/${yearly.id}`);
    assert.equal(annual.revisions[0].items[0].body, "恢复前的文字");
    assert.equal(annual.revisions[0].items[0].source_visibility, "unlisted");
    mark(
      "portable image identity and pg_dump restore preserve full library relationships, date precision, original bytes, annual snapshots and achievements; mismatched image rejected",
    );
    assert.ok(
      (await read("GET", "/api/v1/admin/plugins")).items.every(
        (p) => !p.enabled,
      ),
    );
    assert.ok(
      !(await read("GET", "/api/v1/admin/achievements/backfills")).items.some(
        (j) => j.state === "running",
      ),
    );
    assert.ok(
      (await instanceStatus(names[1])).services.some(
        (s) => s.service === "app" && s.health === "healthy",
      ),
    );
    mark(
      "restored clone is healthy; extensions require review and backfills do not resume unattended",
    );
    report.status = "PASS";
  } catch (e) {
    report.status = "FAIL";
    report.error = e.message;
    throw e;
  } finally {
    report.cleanup = [];
    for (const name of created.reverse()) {
      try {
        await removeTestInstance(name);
        report.cleanup.push({ name, removed: true });
      } catch (e) {
        report.cleanup.push({ name, removed: false, error: e.message });
      }
    }
    if (report.cleanup.some((c) => !c.removed)) report.status = "FAIL";
    report.finished_at = new Date().toISOString();
    await mkdir(path.join(root, ".local/output"), { recursive: true });
    await writeFile(
      path.join(root, ".local/output/memory-instance-smoke.json"),
      JSON.stringify(report, null, 2) + "\n",
    );
    if (report.cleanup.some((c) => !c.removed))
      throw Error("Isolated memory instance cleanup failed.");
  }
  return report;
}
if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
)
  memoryInstanceSmoke({
    image: process.env.ANIMEMO_CANDIDATE_IMAGE,
    previousImage: process.env.ANIMEMO_PREVIOUS_IMAGE,
  })
    .then((r) => console.log(`Memory instance ${r.status}`))
    .catch((e) => {
      console.error(e.message);
      process.exitCode = 1;
    });
