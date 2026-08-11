import { useRef, useState } from "react";

import { api, readableApiError } from "../lib/api.js";

import {
  importIdentityValues,
  parseLocalImportRecords,
} from "./dashboardData.js";

export function useDashboardImport({ isDemo, presetColors, records, setRecords, refreshEntries, flash }) {
  const fileRef = useRef(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importFile, setImportFile] = useState(null);
  const [importPreview, setImportPreview] = useState(null);
  const [importRecords, setImportRecords] = useState([]);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState("");

  const importData = async (event) => {
    const file = event.target.files?.[0];
    const input = event.target;
    if (!file) return;
    const extension = file.name.toLowerCase();
    if (!extension.endsWith(".json") && !extension.endsWith(".csv")) {
      flash("导入失败：请选择 JSON 或 CSV 手账备份文件");
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      flash("导入失败：文件不能超过 2 MB");
      input.value = "";
      return;
    }
    setImportFile(file);
    setImportPreview(null);
    setImportRecords([]);
    setImportError("");
    setImportOpen(true);
    setImportBusy(true);
    try {
      if (!isDemo) {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("preview", "true");
        const response = await api.post("import/", formData, { serverStateInvalidation: false });
        setImportPreview(response.data || {});
        return;
      }

      const text = await file.text();
      let payload;
      if (extension.endsWith(".csv")) {
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
          ...(item?.entry || {}),
          watch_history: item?.watch_history || [],
          external_identities: item?.external_identities || [],
        }));
      }
      const imported = parseLocalImportRecords(payload, presetColors);
      const existingKeys = new Set(records.flatMap(importIdentityValues));
      const seenKeys = new Set();
      const items = [];
      const readyRecords = [];
      const errors = [];
      imported.forEach((record, index) => {
        const row = index + 1;
        if (!record?.title) {
          const reason = "缺少番剧名称";
          errors.push({ row, errors: { title: [reason] } });
          items.push({ row, title: "", status: "invalid", reason });
          return;
        }
        const identity = importIdentityValues(record);
        const duplicate = [...identity].some((key) => existingKeys.has(key) || seenKeys.has(key));
        if (duplicate) {
          items.push({ row, title: record.title, status: "duplicate", reason: "已存在或在本文件中重复" });
          return;
        }
        readyRecords.push(record);
        identity.forEach((key) => seenKeys.add(key));
        items.push({ row, title: record.title, status: "ready", reason: "等待导入" });
      });
      setImportRecords(readyRecords);
      setImportPreview({
        total: imported.length,
        ready: readyRecords.length,
        skipped_duplicates: items.filter((item) => item.status === "duplicate").length,
        errors,
        items,
      });
    } catch (error) {
      setImportError(readableApiError(error, "导入失败：请选择有效的手账 JSON 或 CSV 文件"));
    } finally {
      input.value = "";
      setImportBusy(false);
    }
  };

  const closeImport = (force = false) => {
    if (importBusy && !force) return;
    setImportOpen(false);
    setImportFile(null);
    setImportPreview(null);
    setImportRecords([]);
    setImportError("");
  };

  const confirmImport = async () => {
    if (!importFile || importBusy || !importPreview || Number(importPreview.ready || 0) < 1) return;
    setImportBusy(true);
    try {
      if (isDemo) {
        setRecords((current) => [...importRecords, ...current]);
        flash(`成功导入 ${importRecords.length} 条记录，已跳过 ${importPreview.skipped_duplicates || 0} 条重复记录`);
        setImportBusy(false);
        closeImport(true);
        return;
      }
      const formData = new FormData();
      formData.append("file", importFile);
      const response = await api.post("import/", formData);
      const result = response.data || {};
      refreshEntries();
      flash(`成功导入 ${result.created || 0} 条记录，已跳过 ${result.skipped_duplicates || 0} 条重复记录`);
      setImportBusy(false);
      closeImport(true);
    } catch (error) {
      setImportError(readableApiError(error, "导入失败，请稍后重试。"));
      setImportBusy(false);
    }
  };

  return {
    closeImport,
    confirmImport,
    fileRef,
    importBusy,
    importData,
    importError,
    importFile,
    importOpen,
    importPreview,
  };
}
