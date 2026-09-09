import { createAuthSessionChangedError } from "./authSession.js";

export const BUNDLE_CHUNK_BYTES = 1024 * 1024;
export const BUNDLE_PREVIEW_ITEMS = 50;
export const LOCAL_IMPORT_BYTES = 2 * 1024 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
const STATES = new Set(["receiving", "validating", "ready", "committing", "completed", "cancelled", "failed", "expired"]);
const TERMINAL = new Set(["completed", "cancelled", "failed", "expired"]);
const PREFIX = "bundle-restores/";
const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
const rotate = (value, bits) => (value >>> bits) | (value << (32 - bits));

/** Incremental SHA-256: one 64-byte block and schedule, independent of file size. */
export class BundleSha256 {
  constructor() {
    this.state = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    this.block = new Uint8Array(64);
    this.words = new Uint32Array(64);
    this.used = 0;
    this.bytes = 0;
    this.finished = false;
  }

  compress(bytes, offset = 0) {
    const words = this.words;
    const view = new DataView(bytes.buffer, bytes.byteOffset + offset, 64);
    for (let index = 0; index < 16; index += 1) words[index] = view.getUint32(index * 4);
    for (let index = 16; index < 64; index += 1) {
      const left = words[index - 15];
      const right = words[index - 2];
      const small0 = rotate(left, 7) ^ rotate(left, 18) ^ (left >>> 3);
      const small1 = rotate(right, 17) ^ rotate(right, 19) ^ (right >>> 10);
      words[index] = words[index - 16] + small0 + words[index - 7] + small1;
    }
    let [a, b, c, d, e, f, g, h] = this.state;
    for (let index = 0; index < 64; index += 1) {
      const large1 = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + large1 + choice + K[index] + words[index]) | 0;
      const large0 = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (large0 + majority) | 0;
      h = g; g = f; f = e; e = (d + temp1) | 0;
      d = c; c = b; b = a; a = (temp1 + temp2) | 0;
    }
    [a, b, c, d, e, f, g, h].forEach((value, index) => { this.state[index] += value; });
  }

  update(input) {
    if (this.finished) throw new Error("SHA-256 is already finalized");
    const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
    this.bytes += bytes.byteLength;
    if (!Number.isSafeInteger(this.bytes)) throw new Error("File length is not a safe integer");
    let offset = 0;
    if (this.used) {
      const count = Math.min(64 - this.used, bytes.byteLength);
      this.block.set(bytes.subarray(0, count), this.used);
      this.used += count;
      offset += count;
      if (this.used === 64) { this.compress(this.block); this.used = 0; }
    }
    while (offset + 64 <= bytes.byteLength) { this.compress(bytes, offset); offset += 64; }
    if (offset < bytes.byteLength) {
      this.block.set(bytes.subarray(offset), this.used);
      this.used += bytes.byteLength - offset;
    }
    return this;
  }

  digest() {
    if (this.finished) throw new Error("SHA-256 is already finalized");
    this.finished = true;
    this.block[this.used++] = 0x80;
    if (this.used > 56) {
      this.block.fill(0, this.used);
      this.compress(this.block);
      this.used = 0;
    }
    this.block.fill(0, this.used, 56);
    const view = new DataView(this.block.buffer);
    view.setUint32(56, Math.floor(this.bytes / 0x20000000));
    view.setUint32(60, (this.bytes * 8) >>> 0);
    this.compress(this.block);
    return Array.from(this.state, (value) => value.toString(16).padStart(8, "0")).join("");
  }
}

function restoreError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function checkAbort(signal) {
  if (signal?.aborted) throw signal.reason || restoreError("ERR_CANCELED", "操作已暂停。");
}

async function readFileChunk(file, offset, signal) {
  checkAbort(signal);
  const end = Math.min(file.size, offset + BUNDLE_CHUNK_BYTES);
  const buffer = await file.slice(offset, end).arrayBuffer();
  checkAbort(signal);
  if (buffer.byteLength !== end - offset) throw restoreError("BUNDLE_FILE_CHANGED", "文件读取不完整，请重新选择原文件。");
  return buffer;
}

export async function hashBundleFile(file, { signal, onProgress = () => {} } = {}) {
  if (!file || !Number.isSafeInteger(file.size) || file.size < 1 || typeof file.slice !== "function") {
    throw restoreError("BUNDLE_FILE_INVALID", "请选择非空的 JSON 数据包。");
  }
  const hash = new BundleSha256();
  onProgress({ loaded: 0, total: file.size });
  for (let offset = 0; offset < file.size; offset += BUNDLE_CHUNK_BYTES) {
    const buffer = await readFileChunk(file, offset, signal);
    hash.update(buffer);
    checkAbort(signal);
    onProgress({ loaded: Math.min(file.size, offset + buffer.byteLength), total: file.size });
  }
  return hash.digest();
}

const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value : 0;
const short = (value, limit = 200) => typeof value === "string" ? value.slice(0, limit) : "";

export function boundedImportPreview(preview) {
  if (!preview || typeof preview !== "object" || !Number.isSafeInteger(preview.total) || !Number.isSafeInteger(preview.ready)) return null;
  const rawItems = Array.isArray(preview.items) ? preview.items : [];
  const errors = Array.isArray(preview.errors) ? preview.errors : [];
  return {
    total: count(preview.total),
    ready: count(preview.ready),
    skipped_duplicates: count(preview.skipped_duplicates),
    invalid_count: count(preview.invalid_count) || errors.length,
    errors: errors.slice(0, BUNDLE_PREVIEW_ITEMS).map((item) => ({ row: count(item?.row), code: short(item?.code, 64) })),
    items: rawItems.slice(0, BUNDLE_PREVIEW_ITEMS).map((item) => ({
      row: count(item?.row), title: short(item?.title),
      status: ["ready", "duplicate", "invalid"].includes(item?.status) ? item.status : "invalid",
      reason: short(item?.reason, 160),
    })),
    items_truncated: Boolean(preview.items_truncated) || rawItems.length > BUNDLE_PREVIEW_ITEMS,
  };
}

function normalizeStatus(value, expectedId = null) {
  if (!value || !UUID.test(value.id) || expectedId && value.id !== expectedId
    || !STATES.has(value.state) || !Number.isSafeInteger(value.generation) || value.generation < 1
    || !Number.isSafeInteger(value.expected_bytes) || value.expected_bytes < 1
    || !Number.isSafeInteger(value.received_bytes) || value.received_bytes < 0 || value.received_bytes > value.expected_bytes
    || value.received_bytes !== value.expected_bytes && value.received_bytes % BUNDLE_CHUNK_BYTES !== 0
    || value.chunk_bytes !== BUNDLE_CHUNK_BYTES || !SHA256.test(value.sha256)) {
    throw restoreError("BUNDLE_PROTOCOL_INVALID", "恢复会话响应无效，请重新查询状态。");
  }
  let receipt = null;
  if (value.state === "completed") {
    if (value.receipt?.format !== "animemo-data-bundle" || value.receipt?.schema_version !== 1
      || !Number.isSafeInteger(value.receipt.created) || value.receipt.created < 0
      || value.receipt.total !== value.receipt.created) {
      throw restoreError("BUNDLE_RECEIPT_INVALID", "完成收据尚未确认，请查询恢复状态。");
    }
    receipt = { format: "animemo-data-bundle", schema_version: 1, created: value.receipt.created, total: value.receipt.total };
  }
  return {
    id: value.id, generation: value.generation, state: value.state,
    expected_bytes: value.expected_bytes, received_bytes: value.received_bytes,
    sha256: value.sha256, chunk_bytes: BUNDLE_CHUNK_BYTES,
    preview: boundedImportPreview(value.preview), receipt,
    error_code: short(value.error_code, 64), cleanup_pending: Boolean(value.cleanup_pending),
    expires_at: short(value.expires_at, 64),
  };
}

export function bundleRestoreStorage(browser) {
  // Access to the storage property itself can throw in restricted browsers.
  try { return browser?.localStorage || null; } catch { return null; }
}

function storageKey(ownerId) { return `animemo_bundle_restore_v1:${ownerId}`; }

export function readBundleRestorePointer(storage, ownerId) {
  try {
    const raw = storage?.getItem(storageKey(ownerId));
    if (!raw || raw.length > 4096) return null;
    const value = JSON.parse(raw);
    if (value?.version !== 1 || value.owner_id !== String(ownerId)
      || value.session_id !== null && !UUID.test(value.session_id)
      || value.idempotency_key !== null && !UUID.test(value.idempotency_key)
      || !SHA256.test(value.sha256) || !Number.isSafeInteger(value.expected_bytes) || value.expected_bytes < 1) return null;
    return {
      version: 1, owner_id: String(ownerId), session_id: value.session_id, idempotency_key: value.idempotency_key,
      expected_bytes: value.expected_bytes, sha256: value.sha256, file_name: short(value.file_name),
    };
  } catch { return null; }
}

export function initialBundleRestoreState() {
  return { phase: "idle", busy: false, session: null, preview: null, receipt: null, progress: null, error: null, fileName: "", hasFile: false };
}

/** Account-bound controller. It owns no whole-file text or buffers between chunks. */
export function createBundleRestoreClient({ api, ownerId, getOwnerId, onChange = () => {}, storage = null, createUuid = () => globalThis.crypto.randomUUID() }) {
  const owner = String(ownerId || "");
  if (!/^\d+$/.test(owner) || !api || typeof getOwnerId !== "function") throw new Error("An authenticated restore owner is required");
  let pointer = readBundleRestorePointer(storage, owner);
  let selectedFile = null;
  let snapshot = { ...initialBundleRestoreState(), fileName: pointer?.file_name || "" };
  let requestId = 0;
  let active = null;
  let disposed = false;
  let identityLost = false;
  let authGeneration = null;

  function publish(changes) {
    if (disposed) return;
    snapshot = { ...snapshot, ...changes };
    onChange(snapshot);
  }
  function persist() {
    try {
      if (pointer) storage?.setItem(storageKey(owner), JSON.stringify(pointer));
      else storage?.removeItem(storageKey(owner));
    } catch { /* Server current/status remains the authority when storage is unavailable. */ }
  }
  function assertCurrent(operation) {
    if (identityLost || String(getOwnerId() || "") !== owner) throw createAuthSessionChangedError();
    if (disposed || operation.id !== requestId) throw restoreError("ERR_CANCELED", "操作已暂停。");
    checkAbort(operation.controller.signal);
  }
  function stop(reason) {
    requestId += 1;
    active?.controller.abort(reason);
    active = null;
  }
  function authChanged(value) {
    if (disposed) return;
    const changed = !value?.access || String(value.user?.id || "") !== owner
      || authGeneration !== null && value.generation !== authGeneration
      || authGeneration === null && active !== null;
    if (changed) {
      identityLost = true;
      stop(createAuthSessionChangedError());
      selectedFile = null;
      publish({ ...initialBundleRestoreState(), phase: "authentication_required", error: createAuthSessionChangedError() });
    } else if (authGeneration === null) authGeneration = value.generation;
  }
  async function send(operation, method, path, body, config = {}) {
    assertCurrent(operation);
    const options = {
      serverStateInvalidation: false, ...config, signal: operation.controller.signal,
      ...(authGeneration === null ? {} : { _authGeneration: authGeneration }),
    };
    const response = method === "get" ? await api.get(path, options) : await api[method](path, body, options);
    assertCurrent(operation);
    const generation = response.config?._authGeneration;
    if (generation !== undefined) {
      if (authGeneration !== null && authGeneration !== generation) throw createAuthSessionChangedError();
      authGeneration = generation;
    }
    return response.data;
  }
  function accept(value, expectedId = null) {
    const session = normalizeStatus(value, expectedId);
    if (pointer?.session_id === session.id && (pointer.sha256 !== session.sha256 || pointer.expected_bytes !== session.expected_bytes)) {
      throw restoreError("BUNDLE_PROTOCOL_INVALID", "恢复文件身份发生变化，请重新查询状态。");
    }
    const sameFile = pointer?.sha256 === session.sha256 && pointer?.expected_bytes === session.expected_bytes;
    pointer = {
      version: 1, owner_id: owner, session_id: session.id,
      idempotency_key: sameFile && (pointer?.session_id === session.id || pointer?.session_id === null) ? pointer.idempotency_key : null,
      expected_bytes: session.expected_bytes, sha256: session.sha256, file_name: sameFile ? pointer.file_name : "",
    };
    persist();
    publish({ session, preview: session.preview, receipt: session.receipt, phase: session.state, fileName: pointer.file_name,
      progress: { loaded: session.received_bytes, total: session.expected_bytes } });
    return session;
  }
  async function status(operation) {
    if (pointer?.session_id) {
      try { return accept(await send(operation, "get", `${PREFIX}${pointer.session_id}/`), pointer.session_id); }
      catch (error) {
        assertCurrent(operation);
        if (![404, 410].includes(error?.response?.status)) throw error;
        // Receipts have a retention period; an expired local pointer must not trap the owner.
        pointer = null;
        persist();
        publish({ session: null, preview: null, receipt: null, fileName: "" });
      }
    }
    const data = await send(operation, "get", `${PREFIX}current/`);
    if (!data || !Object.hasOwn(data, "session")) throw restoreError("BUNDLE_PROTOCOL_INVALID", "恢复会话响应无效。");
    return data.session === null ? null : accept(data.session);
  }
  function createPayload() {
    return { idempotency_key: pointer.idempotency_key, expected_bytes: pointer.expected_bytes, sha256: pointer.sha256, schema_version: 1 };
  }
  async function recoverCreation(operation) {
    if (!pointer?.session_id && pointer?.idempotency_key) return accept(await send(operation, "post", PREFIX, createPayload()));
    return status(operation);
  }
  async function run(phase, task) {
    if (snapshot.busy) throw restoreError("BUNDLE_BUSY", "当前操作仍在进行。");
    const operation = { id: ++requestId, controller: new AbortController() };
    active = operation;
    try {
      assertCurrent(operation);
      publish({ phase, busy: true, error: null });
      const result = await task(operation);
      assertCurrent(operation);
      return result;
    } catch (error) {
      if (!disposed && operation.id === requestId) {
        if (error?.code === "AUTH_SESSION_CHANGED" || String(getOwnerId() || "") !== owner) {
          authChanged({});
        } else {
          publish({ phase: snapshot.session && TERMINAL.has(snapshot.session.state) ? snapshot.session.state : "paused", error });
        }
      }
      throw error;
    } finally {
      if (!disposed && operation.id === requestId) { active = null; publish({ busy: false }); }
    }
  }
  async function validate(operation, session) {
    if (session.state !== "receiving" || session.received_bytes !== session.expected_bytes) return session;
    publish({ phase: "validating" });
    return accept(await send(operation, "post", `${PREFIX}${session.id}/validate/`, { generation: session.generation }, { timeout: 300000 }), session.id);
  }
  async function upload(operation, file, session) {
    while (session.state === "receiving" && session.received_bytes < session.expected_bytes) {
      assertCurrent(operation);
      const offset = session.received_bytes;
      publish({ phase: "uploading", progress: { loaded: offset, total: file.size } });
      const buffer = await readFileChunk(file, offset, operation.controller.signal);
      const result = await send(operation, "put", `${PREFIX}${session.id}/chunks/`, buffer, {
        params: { offset, generation: session.generation }, timeout: 60000, headers: { "Content-Type": "application/octet-stream" },
      });
      session = accept(result, session.id);
      if (session.state === "receiving" && session.received_bytes < offset + buffer.byteLength) {
        throw restoreError("BUNDLE_PROGRESS_INVALID", "上传进度未被服务器确认，请继续恢复。");
      }
    }
    return validate(operation, session);
  }
  function discover() {
    return run("checking", async (operation) => {
      const session = await status(operation);
      if (!session) publish({ phase: pointer?.idempotency_key ? "paused" : "idle" });
      return session;
    });
  }
  function prepare(file, { startNew = false } = {}) {
    return run("checking", async (operation) => {
      let session = await status(operation);
      if (session && TERMINAL.has(session.state)) {
        if (!startNew) return session;
        pointer = null;
        session = null;
        persist();
        publish({ session: null, preview: null, receipt: null });
      }
      selectedFile = file;
      publish({ fileName: short(file?.name), hasFile: true });
      if (session && file.size !== session.expected_bytes || !session && pointer && file.size !== pointer.expected_bytes) {
        throw restoreError("BUNDLE_FILE_MISMATCH", "请选择该恢复会话的原始 JSON 文件；若要导入其他文件，请先取消当前会话。");
      }
      publish({ phase: "hashing", progress: { loaded: 0, total: file.size } });
      const sha256 = await hashBundleFile(file, {
        signal: operation.controller.signal,
        onProgress: (progress) => { assertCurrent(operation); publish({ progress }); },
      });
      assertCurrent(operation);
      if (pointer && (pointer.sha256 !== sha256 || pointer.expected_bytes !== file.size)) {
        throw restoreError("BUNDLE_FILE_MISMATCH", "文件摘要不匹配，请选择原始 JSON 文件，或取消当前会话后开始新的恢复。");
      }
      if (!pointer) {
        const key = createUuid();
        if (!UUID.test(key)) throw restoreError("BUNDLE_ID_UNAVAILABLE", "浏览器无法创建恢复身份，请使用安全连接重试。");
        pointer = { version: 1, owner_id: owner, session_id: null, idempotency_key: key, expected_bytes: file.size, sha256, file_name: short(file.name) };
        persist();
      } else {
        pointer = { ...pointer, file_name: short(file.name) };
        persist();
      }
      if (!session) session = await recoverCreation(operation);
      if (!session) throw restoreError("BUNDLE_SESSION_MISSING", "尚未确认恢复会话，请重新选择文件。");
      return upload(operation, file, session);
    });
  }
  function resume() {
    if (selectedFile && (!snapshot.session || snapshot.session.state === "receiving")) return prepare(selectedFile);
    return run("checking", async (operation) => {
      const session = await recoverCreation(operation);
      if (!session) { publish({ phase: "idle" }); return null; }
      if (session.state === "receiving" && session.received_bytes < session.expected_bytes) {
        throw restoreError("BUNDLE_FILE_REQUIRED", "请选择原始 JSON 文件，核对摘要后继续上传。");
      }
      return validate(operation, session);
    });
  }
  function commit() {
    return run("committing", async (operation) => {
      const session = await recoverCreation(operation);
      if (session?.state === "completed") return session;
      if (session?.state !== "ready") throw restoreError("BUNDLE_NOT_READY", "请先完成上传与校验，或查询当前恢复状态。");
      publish({ phase: "committing" });
      try {
        return accept(await send(operation, "post", `${PREFIX}${session.id}/commit/`, { generation: session.generation }, { timeout: 300000 }), session.id);
      } catch (error) {
        assertCurrent(operation);
        // A lost commit response is not a failed transaction. Read once, never blindly replay.
        if (error?.code !== "AUTH_SESSION_CHANGED") {
          try {
            const confirmed = await status(operation);
            if (confirmed?.state === "completed") return confirmed;
          } catch (statusError) {
            assertCurrent(operation);
            if (statusError?.code === "AUTH_SESSION_CHANGED") throw statusError;
          }
        }
        throw error;
      }
    });
  }
  function pause() {
    stop(restoreError("ERR_CANCELED", "恢复已暂停，可在当前账号下继续。"));
    publish({ busy: false, phase: snapshot.session && TERMINAL.has(snapshot.session.state) ? snapshot.session.state : "paused" });
  }
  function cancel() {
    pause();
    return run("cancelling", async (operation) => {
      const session = await recoverCreation(operation);
      if (!session) { selectedFile = null; publish({ ...initialBundleRestoreState(), phase: "cancelled" }); return null; }
      if (TERMINAL.has(session.state)) return session;
      const result = accept(await send(operation, "post", `${PREFIX}${session.id}/cancel/`, { generation: session.generation }, { timeout: 300000 }), session.id);
      selectedFile = null;
      publish({ hasFile: false });
      return result;
    });
  }
  return Object.freeze({
    getSnapshot: () => snapshot,
    getAuthGeneration: () => authGeneration,
    discover, prepare, resume, commit, cancel, pause, authChanged,
    dispose() { stop(restoreError("ERR_CANCELED", "页面已关闭。")); disposed = true; selectedFile = null; },
  });
}
