import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, getAuthUser, subscribeAuth } from "../lib/api.js";
import { createPublicCatalogClient, initialPublicCatalogState, publicCatalogFailure, publicCatalogQuery } from "../lib/publicCatalog.js";

const ignoreCancellation = (error) => ["PUBLIC_CANCELLED", "PUBLIC_EXPORT_CANCELLED", "AUTH_SESSION_CHANGED"].includes(error?.code);

export async function pickPublicCatalogWriter(name) {
  if (typeof window.showSaveFilePicker !== "function") {
    const error = new Error("当前浏览器不支持完整流式下载。请使用支持文件保存的桌面 Chrome 或 Edge 下载完整数据。");
    error.code = "PUBLIC_EXPORT_UNAVAILABLE";
    throw error;
  }
  const handle = await window.showSaveFilePicker({
    suggestedName: String(name || "animemo-public.json").replace(/[\\/:*?"<>|]+/g, "-").slice(0, 160),
    types: [{ description: "JSON 数据", accept: { "application/json": [".json"] } }],
  });
  return handle.createWritable();
}

export function usePublicCatalog({ kind = "homepage", publicSlug = "", filters = {}, enabled = true }) {
  const [stored, setStored] = useState(() => ({ locationKey: "", queryKey: "", state: initialPublicCatalogState() }));
  const clientRef = useRef(null);
  const queryKey = JSON.stringify(publicCatalogQuery(filters));
  const queryRef = useRef(null);
  queryRef.current = JSON.parse(queryKey);
  const locationKey = `${kind}:${publicSlug}`;

  useEffect(() => {
    if (!enabled) { setStored({ locationKey, queryKey, state: initialPublicCatalogState() }); return undefined; }
    let client;
    try {
      client = createPublicCatalogClient({ api, kind, publicSlug, getActorId: () => getAuthUser()?.id,
        onChange: (snapshot) => { if (clientRef.current === client) setStored({ locationKey, queryKey: JSON.stringify(client.getQuery()), state: snapshot }); } });
    } catch (error) {
      const empty = initialPublicCatalogState();
      setStored({ locationKey, queryKey, state: { ...empty, list: { ...empty.list, status: "error", error: publicCatalogFailure(error) } } });
      return undefined;
    }
    clientRef.current = client;
    setStored({ locationKey, queryKey: JSON.stringify(client.getQuery()), state: client.getSnapshot() });
    const unsubscribe = subscribeAuth((snapshot) => {
      if (client.authChanged(snapshot)) void client.setQuery(queryRef.current, { facets: kind !== "directory" });
    });
    void client.setQuery(queryRef.current, { facets: kind !== "directory" });
    return () => {
      unsubscribe();
      client.dispose();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [enabled, locationKey]);

  useEffect(() => {
    const client = clientRef.current;
    if (client && JSON.stringify(client.getQuery()) !== queryKey) void client.setQuery(queryRef.current);
  }, [queryKey]);

  const run = useCallback(async (method, ...args) => {
    const client = clientRef.current;
    if (!client) return null;
    try { return await client[method](...args); }
    catch { return null; /* The current operation publishes its bounded, visible error. */ }
  }, []);

  const save = useCallback(async (method, args, name) => {
    const client = clientRef.current;
    if (!client || ["choosing", "writing", "finalizing"].includes(client.getSnapshot().exporting.status)) return;
    const intent = client.captureIntent();
    client.beginExportSelection();
    let writer;
    try {
      writer = await pickPublicCatalogWriter(name);
      client.assertIntent(intent);
      return await client[method](...args, writer);
    } catch (error) {
      if (writer) { try { await writer.abort(); } catch { /* A completed or already-aborted stream needs no second cleanup. */ } }
      if (!ignoreCancellation(error) && clientRef.current === client) client.exportSelectionError(error, intent);
      return null;
    }
  }, []);

  const actions = useMemo(() => ({
    refresh: () => run("refresh"),
    autoRefresh: () => { if (clientRef.current?.canAutoRefresh()) return run("refresh"); return null; },
    nextPage: () => run("nextPage"),
    previousPage: () => run("previousPage"),
    loadFacet: (name, options) => run("loadFacet", name, options),
    openDetail: (id) => run("openDetail", id),
    closeDetail: () => clientRef.current?.closeDetail(),
    readEntryField: (name, direction) => run("readEntryField", name, direction),
    readFacetValue: (name, item, direction) => run("readFacetValue", name, item, direction),
    exportAll: (name = "animemo-public.json") => save("exportTo", [], name),
    exportEntryField: (field, name = `animemo-${field}.json`) => save("exportEntryFieldTo", [field], name),
    exportFacetValue: (kind, item) => save("exportFacetValueTo", [kind, item], `animemo-${kind}.json`),
    cancelExport: () => clientRef.current?.cancelExport(),
  }), [run, save]);
  const state = enabled && stored.locationKey === locationKey && stored.queryKey === queryKey ? stored.state : initialPublicCatalogState();
  return { ...state, ...actions };
}
