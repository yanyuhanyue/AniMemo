import {
  closeSync,
  constants,
  lstatSync,
  mkdirSync,
  openSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

const ARTIFACT_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const OUTPUT_ERROR = "FRONTEND_PERF_OUTPUT must name a JSON file inside the project artifacts directory";

export function resolveArtifactOutputPath(rawValue, projectRoot) {
  if (typeof rawValue !== "string" || rawValue === "") throw new Error(OUTPUT_ERROR);

  const artifactsRoot = resolve(projectRoot, "artifacts");
  const candidate = isAbsolute(rawValue)
    ? resolve(rawValue)
    : resolve(projectRoot, rawValue);
  const relativePath = relative(artifactsRoot, candidate);
  const segments = relativePath.split(sep);

  if (
    relativePath === ""
    || relativePath === ".."
    || relativePath.startsWith(`..${sep}`)
    || isAbsolute(relativePath)
    || segments.some((segment) => !ARTIFACT_SEGMENT.test(segment))
    || !segments.at(-1).endsWith(".json")
  ) {
    throw new Error(OUTPUT_ERROR);
  }

  // Rebuild from the trusted root and validated segments before any filesystem sink.
  return join(artifactsRoot, ...segments);
}

function requireRealDirectory(path) {
  try {
    const metadata = lstatSync(path);
    if (metadata.isSymbolicLink()) throw new Error("artifact path must not contain symbolic links");
    if (!metadata.isDirectory()) throw new Error("artifact parent must be a directory");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    mkdirSync(path);
  }
}

export function writeArtifactFileSync(rawValue, content, projectRoot) {
  const outputPath = resolveArtifactOutputPath(rawValue, projectRoot);
  const artifactsRoot = resolve(projectRoot, "artifacts");
  requireRealDirectory(artifactsRoot);

  const parentRelative = relative(artifactsRoot, dirname(outputPath));
  let current = artifactsRoot;
  if (parentRelative) {
    for (const segment of parentRelative.split(sep)) {
      current = join(current, segment);
      requireRealDirectory(current);
    }
  }

  let flags = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL;
  try {
    const metadata = lstatSync(outputPath);
    if (metadata.isSymbolicLink()) throw new Error("artifact path must not contain symbolic links");
    if (!metadata.isFile()) throw new Error("artifact output must be a regular file");
    flags = constants.O_WRONLY | constants.O_TRUNC;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  const descriptor = openSync(outputPath, flags | (constants.O_NOFOLLOW || 0), 0o600);
  try {
    writeFileSync(descriptor, content, "utf8");
  } finally {
    closeSync(descriptor);
  }
  return outputPath;
}
