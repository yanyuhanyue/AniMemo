import { useCallback, useEffect, useRef, useState } from "react";

import { api, getAuthUser, readableApiError, subscribeAuth } from "../lib/api.js";
import { invalidateServerState } from "../lib/serverState.js";
import {
  LOCAL_IMPORT_BYTES,
  boundedImportPreview,
  bundleRestoreStorage,
  createBundleRestoreClient,
  initialBundleRestoreState,
} from "../lib/bundleRestore.js";
import { importIdentityValues, parseLocalImportRecords } from "./dashboardData.js";

const terminal = (session) => ["completed", "cancelled", "failed", "expired"].includes(session?.state);

function restoreMessage(state) {
  const error = state.error;
  if (error?.code === "AUTH_SESSION_CHANGED") return "登录状态已变化，请在当前账号下重新打开导入。";
  if (error?.code?.startsWith("BUNDLE_")) return error.message;
  if (error) return readableApiError(error, "恢复结果尚未确认，请查询状态或选择原文件继续。");
  if (state.session?.state === "failed") return "此次恢复未完成。请查看文件及目标手账状态，再开始新的恢复。";
  if (state.session?.state === "expired") return "恢复会话已过期，请重新选择文件开始恢复。";
  return "";
}

export function useDashboardImport({ isDemo, presetColors, records, setRecords, refreshEntries, flash }) {
  const fileRef = useRef(null);
  const clientRef = useRef(null);
  const mountedRef = useRef(false);
  const ownerRef = useRef(getAuthUser()?.id ?? null);
  const authGenerationRef = useRef(null);
  const localGenerationRef = useRef(0);
  const localControllerRef = useRef(null);
  const previewIdentityRef = useRef(null);
  const completedReceiptRef = useRef(null);
  const [identity, setIdentity] = useState(() => ({ owner: ownerRef.current, revision: 0 }));
  const [importOpen, setImportOpen] = useState(false);
  const [importFile, setImportFile] = useState(null);
  const [importKind, setImportKind] = useState(isDemo ? "demo" : "bundle");
  const [localPreview, setLocalPreview] = useState(null);
  const [importRecords, setImportRecords] = useState([]);
  const [localBusy, setLocalBusy] = useState(false);
  const [localError, setLocalError] = useState("");
  const [restore, setRestore] = useState(initialBundleRestoreState);

  const cancelLocal = useCallback(() => {
    localGenerationRef.current += 1;
    localControllerRef.current?.abort();
    localControllerRef.current = null;
    previewIdentityRef.current = null;
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const unsubscribe = subscribeAuth((snapshot) => {
      const client = clientRef.current;
      const previous = client?.getAuthGeneration() ?? authGenerationRef.current;
      const owner = snapshot.user?.id ?? null;
      const changed = !snapshot.access || String(owner) !== String(ownerRef.current)
        || previous !== null && previous !== snapshot.generation;
      client?.authChanged(snapshot);
      authGenerationRef.current = snapshot.generation;
      ownerRef.current = owner;
      if (changed || client?.getSnapshot().phase === "authentication_required") {
        cancelLocal();
        setImportFile(null);
        setLocalPreview(null);
        setImportRecords([]);
        setLocalBusy(false);
        setLocalError("登录状态已变化，请在当前账号下重新打开导入。");
        setRestore(initialBundleRestoreState());
        setIdentity((current) => ({ owner, revision: current.revision + 1 }));
      }
    });
    return () => {
      mountedRef.current = false;
      unsubscribe();
      cancelLocal();
    };
  }, [cancelLocal]);

  useEffect(() => {
    if (isDemo || !identity.owner) return undefined;
    const client = createBundleRestoreClient({
      api,
      ownerId: identity.owner,
      getOwnerId: () => getAuthUser()?.id,
      storage: bundleRestoreStorage(typeof window === "undefined" ? null : window),
      onChange: (state) => {
        if (mountedRef.current && clientRef.current === client) setRestore(state);
      },
    });
    clientRef.current = client;
    setRestore(client.getSnapshot());
    return () => {
      client.dispose();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [identity.owner, identity.revision, isDemo]);

  const hasCurrentClient = (client) => mountedRef.current && clientRef.current === client
    && String(getAuthUser()?.id || "") === String(identity.owner || "");

  const completed = (client, session, announce = false) => {
    if (!hasCurrentClient(client) || session?.state !== "completed" || !session.receipt) return;
    if (completedReceiptRef.current?.client === client && completedReceiptRef.current.id === session.id) return;
    completedReceiptRef.current = { client, id: session.id };
    invalidateServerState("analytics");
    refreshEntries();
    if (announce) flash(`成功恢复 ${session.receipt.created} 条记录，完成收据已保存。`);
  };

  const openImport = async () => {
    setImportOpen(true);
    setLocalError("");
    if (isDemo) { setImportKind("demo"); return; }
    setImportKind("bundle");
    const client = clientRef.current;
    if (!client) { setLocalError("请先登录，再打开手账恢复。"); return; }
    try { completed(client, await client.discover()); }
    catch { /* The account-bound controller supplies the visible failure state. */ }
  };

  const importData = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const extension = file.name.toLowerCase();
    const isJson = extension.endsWith(".json");
    if (!isJson && !extension.endsWith(".csv")) {
      flash("导入失败：请选择 JSON 或 CSV 手账备份文件");
      return;
    }
    if ((isDemo || !isJson) && file.size > LOCAL_IMPORT_BYTES) {
      setLocalError(isDemo ? "演示模式只支持本机导入 2 MiB 以内的文件；较大 JSON 数据包请登录后恢复。" : "CSV 文件不能超过 2 MiB；JSON 数据包使用可恢复上传。");
      setImportOpen(true);
      return;
    }
    const client = clientRef.current;
    if (!isDemo && !isJson && client?.getSnapshot().session && !terminal(client.getSnapshot().session)) {
      setLocalError("当前账号有未完成的 JSON 恢复，请先继续或取消该会话，再导入 CSV。");
      return;
    }
    setImportFile(file);
    setLocalPreview(null);
    setImportRecords([]);
    setLocalError("");
    setImportOpen(true);
    if (!isDemo && isJson) {
      setImportKind("bundle");
      if (!client) { setLocalError("请先登录，再开始手账恢复。"); return; }
      const startNew = terminal(client.getSnapshot().session);
      try { completed(client, await client.prepare(file, { startNew })); }
      catch { /* No synchronous JSON fallback: errors and resume remain in this session. */ }
      return;
    }

    setImportKind(isDemo ? "demo" : "csv");
    cancelLocal();
    const generation = localGenerationRef.current;
    const owner = getAuthUser()?.id ?? null;
    const controller = new AbortController();
    localControllerRef.current = controller;
    const current = () => mountedRef.current && generation === localGenerationRef.current
      && String(owner) === String(getAuthUser()?.id ?? null);
    setLocalBusy(true);
    try {
      if (!isDemo) {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("preview", "true");
        const response = await api.post("import/", formData, {
          serverStateInvalidation: false, signal: controller.signal,
          ...(authGenerationRef.current === null ? {} : { _authGeneration: authGenerationRef.current }),
        });
        if (!current()) return;
        authGenerationRef.current = response.config?._authGeneration ?? authGenerationRef.current;
        previewIdentityRef.current = { owner, generation, authGeneration: authGenerationRef.current };
        setLocalPreview(boundedImportPreview(response.data || {}));
        return;
      }

      // Demo is explicitly local and capped above; server JSON never uses file.text().
      const text = await file.text();
      if (!current()) return;
      let payload;
      if (!isJson) {
        const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/).filter((line) => line.trim());
        const headers = (lines.shift() || "").split(",").map((header) => header.trim());
        payload = lines.map((line) => {
          const cells = line.split(",");
          return Object.fromEntries(headers.map((header, index) => [header, cells[index] || ""]));
        });
      } else {
        const parsed = JSON.parse(text);
        if (parsed?.format !== "animemo-data-bundle" || parsed?.schema_version !== 1 || !Array.isArray(parsed.entries)) {
          throw new Error("unsupported_import_schema");
        }
        payload = parsed.entries.map((item) => ({
          ...(item?.entry || {}), watch_history: item?.watch_history || [], external_identities: item?.external_identities || [],
        }));
      }
      const imported = parseLocalImportRecords(payload, presetColors);
      const existingKeys = new Set(records.flatMap((record) => [...importIdentityValues(record)]));
      const seenKeys = new Set();
      const items = [];
      const readyRecords = [];
      let duplicates = 0;
      let invalid = 0;
      imported.forEach((record, index) => {
        const row = index + 1;
        if (!record?.title) {
          invalid += 1;
          if (items.length < 50) items.push({ row, title: "", status: "invalid", reason: "缺少番剧名称" });
          return;
        }
        const keys = importIdentityValues(record);
        if ([...keys].some((key) => existingKeys.has(key) || seenKeys.has(key))) {
          duplicates += 1;
          if (items.length < 50) items.push({ row, title: record.title, status: "duplicate", reason: "已存在或在本文件中重复" });
          return;
        }
        readyRecords.push(record);
        keys.forEach((key) => seenKeys.add(key));
        if (items.length < 50) items.push({ row, title: record.title, status: "ready", reason: "等待导入" });
      });
      if (!current()) return;
      previewIdentityRef.current = { owner, generation, authGeneration: authGenerationRef.current };
      setImportRecords(readyRecords);
      setLocalPreview(boundedImportPreview({ total: imported.length, ready: readyRecords.length,
        skipped_duplicates: duplicates, invalid_count: invalid, items, items_truncated: imported.length > items.length }));
    } catch (error) {
      if (current()) setLocalError(readableApiError(error, "导入失败：请选择有效的手账 JSON 或 CSV 文件"));
    } finally {
      if (current()) { localControllerRef.current = null; setLocalBusy(false); }
    }
  };

  const closeImport = (force = false) => {
    if (importKind === "bundle") {
      clientRef.current?.pause();
      setImportOpen(false);
      return;
    }
    if (localBusy && !force) return;
    cancelLocal();
    setImportOpen(false);
    setImportFile(null);
    setLocalPreview(null);
    setImportRecords([]);
    setLocalError("");
  };

  const confirmImport = async () => {
    if (importKind === "bundle") {
      const client = clientRef.current;
      if (!client || restore.busy || restore.session?.state !== "ready") return;
      setLocalError("");
      try { completed(client, await client.commit(), true); }
      catch { /* An unconfirmed commit remains visible with its status/receipt recovery path. */ }
      return;
    }
    const expected = previewIdentityRef.current;
    if (!importFile || localBusy || !localPreview || Number(localPreview.ready || 0) < 1) return;
    if (!expected || expected.generation !== localGenerationRef.current || String(expected.owner) !== String(getAuthUser()?.id ?? null)) {
      setLocalError("登录状态已变化，请重新选择导入文件。");
      return;
    }
    setLocalBusy(true);
    const current = () => mountedRef.current && expected.generation === localGenerationRef.current
      && String(expected.owner) === String(getAuthUser()?.id ?? null);
    try {
      if (isDemo) {
        setRecords((previous) => [...importRecords, ...previous]);
        flash(`成功导入 ${importRecords.length} 条记录，已跳过 ${localPreview.skipped_duplicates || 0} 条重复记录`);
        setLocalBusy(false);
        closeImport(true);
        return;
      }
      const controller = new AbortController();
      localControllerRef.current = controller;
      const formData = new FormData();
      formData.append("file", importFile);
      const response = await api.post("import/", formData, {
        signal: controller.signal,
        ...(expected.authGeneration === null ? {} : { _authGeneration: expected.authGeneration }),
      });
      if (!current()) return;
      const result = response.data || {};
      refreshEntries();
      flash(`成功导入 ${result.created || 0} 条记录，已跳过 ${result.skipped_duplicates || 0} 条重复记录`);
      setLocalBusy(false);
      closeImport(true);
    } catch (error) {
      if (current()) setLocalError(readableApiError(error, "导入失败，请稍后重试。"));
    } finally {
      if (current()) { localControllerRef.current = null; setLocalBusy(false); }
    }
  };

  const resumeImport = async () => {
    const client = clientRef.current;
    if (!client || restore.busy) return;
    setLocalError("");
    if (!restore.hasFile && restore.session?.state === "receiving" && restore.session.received_bytes < restore.session.expected_bytes) {
      fileRef.current?.click();
      return;
    }
    try { completed(client, await client.resume()); }
    catch { /* The controller retains the exact session and its retryable error. */ }
  };

  const cancelImport = async () => {
    const client = clientRef.current;
    if (!client) return;
    setLocalError("");
    try { completed(client, await client.cancel()); }
    catch { /* Cancellation may race a commit; only its authoritative receipt determines completion. */ }
  };

  return {
    closeImport, confirmImport, cancelImport, resumeImport, openImport, fileRef, importData,
    chooseImportFile: () => fileRef.current?.click(),
    importBusy: importKind === "bundle" ? restore.busy : localBusy,
    importError: localError || (importKind === "bundle" ? restoreMessage(restore) : ""),
    importFile, importOpen,
    importPreview: importKind === "bundle" ? restore.preview : localPreview,
    importRestore: importKind === "bundle" ? restore : null,
    importDemo: importKind === "demo",
  };
}
