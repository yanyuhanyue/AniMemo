import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

import {
  resolveArtifactOutputPath,
  writeArtifactFileSync,
} from "../scripts/performance-output.mjs";

test("frontend probe uses only the fixed canonical artifact path", () => {
  const source = readFileSync(
    new URL("./performance-frontend-e2e.mjs", import.meta.url),
    "utf8",
  );

  assert.doesNotMatch(source, /FRONTEND_PERF_OUTPUT/);
  assert.match(
    source,
    /resolveArtifactOutputPath\("artifacts\/frontend\.json", projectRoot\)/,
  );
});

test("keeps configured performance output inside the artifacts root", () => {
  const projectRoot = mkdtempSync(join(tmpdir(), "animemo-perf-output-"));
  try {
    const nested = resolveArtifactOutputPath("artifacts/nested/frontend.json", projectRoot);
    assert.equal(nested, resolve(projectRoot, "artifacts/nested/frontend.json"));
    assert.equal(
      resolveArtifactOutputPath(resolve(projectRoot, "artifacts/frontend.json"), projectRoot),
      resolve(projectRoot, "artifacts/frontend.json"),
    );

    writeArtifactFileSync("artifacts/nested/frontend.json", "safe\n", projectRoot);
    assert.equal(readFileSync(nested, "utf8"), "safe\n");
  } finally {
    rmSync(projectRoot, { recursive: true, force: true });
  }
});

test("rejects performance output outside the artifacts root", () => {
  const projectRoot = mkdtempSync(join(tmpdir(), "animemo-perf-output-"));
  try {
    const rejected = [
      "../outside.json",
      "artifacts/../../outside.json",
      resolve(projectRoot, "outside.json"),
      resolve(projectRoot, "artifacts-other/frontend.json"),
      "artifacts/frontend.txt",
    ];
    for (const candidate of rejected) {
      assert.throws(
        () => resolveArtifactOutputPath(candidate, projectRoot),
        /FRONTEND_PERF_OUTPUT must name a JSON file inside the project artifacts directory/,
        candidate,
      );
    }
  } finally {
    rmSync(projectRoot, { recursive: true, force: true });
  }
});

test("rejects an artifact output symlink", (context) => {
  const projectRoot = mkdtempSync(join(tmpdir(), "animemo-perf-output-"));
  const outside = resolve(projectRoot, "outside.json");
  try {
    mkdirSync(resolve(projectRoot, "artifacts"));
    writeFileSync(outside, "outside\n", "utf8");
    try {
      symlinkSync(outside, resolve(projectRoot, "artifacts/frontend.json"), "file");
    } catch (error) {
      if (error?.code === "EPERM") {
        context.skip("symlink creation is unavailable on this Windows host");
        return;
      }
      throw error;
    }

    assert.throws(
      () => writeArtifactFileSync("artifacts/frontend.json", "unsafe\n", projectRoot),
      /artifact path must not contain symbolic links/,
    );
    assert.equal(readFileSync(outside, "utf8"), "outside\n");
  } finally {
    rmSync(projectRoot, { recursive: true, force: true });
  }
});
