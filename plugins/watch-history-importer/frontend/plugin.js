var __defProp = Object.defineProperty;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __esm = (fn, res) => function __init() {
  return fn && (res = (0, fn[__getOwnPropNames(fn)[0]])(fn = 0)), res;
};
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};

// plugins/watch-history-importer/frontend/Icon.jsx
import { jsx } from "react/jsx-runtime";
function Icon({ name, spin = false, className = "" }) {
  return /* @__PURE__ */ jsx(
    "span",
    {
      "aria-hidden": "true",
      className: `ajp-icon${spin ? " is-spinning" : ""}${className ? ` ${className}` : ""}`,
      "data-icon": name,
      children: GLYPHS[name] || "\u2022"
    }
  );
}
var GLYPHS;
var init_Icon = __esm({
  "plugins/watch-history-importer/frontend/Icon.jsx"() {
    GLYPHS = Object.freeze({
      "arrow-left": "\u2190",
      "arrow-up-right": "\u2197",
      check: "\u2713",
      "file-upload": "\u21E7",
      history: "\u25F7",
      layers: "\u25A6",
      reset: "\u21BB",
      save: "\u25A3",
      search: "\u2315",
      spinner: "\u25CC",
      warning: "!"
    });
  }
});

// plugins/watch-history-importer/frontend/errors.js
function readablePluginError(error, fallback) {
  const payload = error?.response?.data;
  if (typeof payload?.detail === "string" && payload.detail.trim()) return payload.detail;
  if (typeof payload?.message === "string" && payload.message.trim()) return payload.message;
  if (error?.response?.status === 403) return "\u5F53\u524D\u8D26\u53F7\u6CA1\u6709\u8FD0\u884C\u6B64\u63D2\u4EF6\u63A5\u53E3\u7684\u6743\u9650\u3002";
  if (error?.response?.status === 401) return "\u767B\u5F55\u72B6\u6001\u5DF2\u5931\u6548\uFF0C\u8BF7\u91CD\u65B0\u767B\u5F55\u3002";
  if (error?.response?.status === 429) return "\u64CD\u4F5C\u8FC7\u4E8E\u9891\u7E41\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002";
  return String(fallback || "\u63D2\u4EF6\u8BF7\u6C42\u5931\u8D25\u3002");
}
var init_errors = __esm({
  "plugins/watch-history-importer/frontend/errors.js"() {
  }
});

// plugins/watch-history-importer/frontend/styles.css
var init_styles = __esm({
  "plugins/watch-history-importer/frontend/styles.css"() {
  }
});

// plugins/watch-history-importer/frontend/WatchHistoryImporterPage.jsx
var WatchHistoryImporterPage_exports = {};
__export(WatchHistoryImporterPage_exports, {
  default: () => WatchHistoryImporterPage
});
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Fragment, jsx as jsx2, jsxs } from "react/jsx-runtime";
function StatusChip({ value }) {
  const labels = {
    pending: "\u7B49\u5F85\u5339\u914D",
    matched: "\u5DF2\u5339\u914D",
    ambiguous: "\u9700\u8981\u786E\u8BA4",
    low_confidence: "\u4F4E\u7F6E\u4FE1\u5EA6",
    no_result: "\u65E0\u7ED3\u679C",
    season_mismatch: "\u5B63\u6570\u4E0D\u5339\u914D",
    network_error: "\u7F51\u7EDC\u9519\u8BEF",
    episode_mismatch: "\u8BDD\u6570\u4E0D\u4E00\u81F4"
  };
  return /* @__PURE__ */ jsx2("span", { className: `ajp-watch-import__status is-${value}`, children: labels[value] || value });
}
function BangumiLink({ resolution, children }) {
  if (!resolution?.source_url) return /* @__PURE__ */ jsx2("strong", { children });
  return /* @__PURE__ */ jsxs(
    "a",
    {
      className: "ajp-watch-import__bangumi-link",
      href: resolution.source_url,
      target: "_blank",
      rel: "noreferrer",
      "aria-label": `\u5728 Bangumi \u67E5\u770B ${resolution.title || children}`,
      title: `\u5728 Bangumi \u67E5\u770B ${resolution.title || children}`,
      children: [
        /* @__PURE__ */ jsx2("strong", { children }),
        /* @__PURE__ */ jsx2(Icon, { name: "arrow-up-right" })
      ]
    }
  );
}
function Summary({ summary = {} }) {
  return /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__summary", children: [
    /* @__PURE__ */ jsxs("article", { children: [
      /* @__PURE__ */ jsx2("span", { children: "\u89E3\u6790\u8BB0\u5F55" }),
      /* @__PURE__ */ jsx2("strong", { children: summary.parsed ?? 0 })
    ] }),
    /* @__PURE__ */ jsxs("article", { children: [
      /* @__PURE__ */ jsx2("span", { children: "\u756A\u5267\u5206\u7EC4" }),
      /* @__PURE__ */ jsx2("strong", { children: summary.anime_groups ?? 0 })
    ] }),
    /* @__PURE__ */ jsxs("article", { children: [
      /* @__PURE__ */ jsx2("span", { children: "\u5DF2\u5339\u914D" }),
      /* @__PURE__ */ jsx2("strong", { children: summary.matched ?? 0 })
    ] }),
    /* @__PURE__ */ jsxs("article", { children: [
      /* @__PURE__ */ jsx2("span", { children: "\u9700\u8981\u786E\u8BA4" }),
      /* @__PURE__ */ jsx2("strong", { children: summary.manual_review ?? 0 })
    ] }),
    /* @__PURE__ */ jsxs("article", { children: [
      /* @__PURE__ */ jsx2("span", { children: "\u5DF2\u5254\u9664" }),
      /* @__PURE__ */ jsx2("strong", { children: summary.excluded ?? 0 })
    ] })
  ] });
}
function WatchHistoryImporterPage({ host, api: pluginApi }) {
  const client = pluginApi;
  const navigate = useNavigate();
  const inputRef = useRef(null);
  const resolvingRef = useRef(false);
  const [data, setData] = useState(EMPTY);
  const [batch, setBatch] = useState(null);
  const [files, setFiles] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [subjectDrafts, setSubjectDrafts] = useState({});
  const [previewQuery, setPreviewQuery] = useState("");
  const [excludedGroupIndices, setExcludedGroupIndices] = useState(() => /* @__PURE__ */ new Set());
  const flash = useCallback((message) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 2600);
  }, []);
  useEffect(() => {
    if (!host?.auth?.isAuthenticated()) {
      navigate("/login", { replace: true });
      return;
    }
    if (!client) {
      setError("Host SDK API \u4E0D\u53EF\u7528\u3002");
      setLoading(false);
      return;
    }
    client.get("status/").then(({ data: result }) => {
      setData({ ...EMPTY, ...result || {} });
    }).catch((requestError) => setError(readablePluginError(requestError, "\u63D2\u4EF6\u672A\u542F\u7528\u6216\u6682\u65F6\u4E0D\u53EF\u7528\u3002"))).finally(() => setLoading(false));
  }, [client, navigate]);
  const chooseFiles = (list) => {
    const next = Array.from(list || []).filter((file) => file.name.toLowerCase().endsWith(".txt")).slice(0, 8);
    setFiles(next);
    setError(next.length ? "" : "\u8BF7\u9009\u62E9 TXT \u89C2\u770B\u8BB0\u5F55\u6587\u4EF6\u3002");
  };
  const openBatch = async (batchId) => {
    if (!batchId || busy) return;
    setBusy(`batch-${batchId}`);
    setError("");
    try {
      const response = await client.get(`batches/${batchId}/`);
      setBatch(response.data);
      setSubjectDrafts({});
      setPreviewQuery("");
      setExcludedGroupIndices(new Set(response.data?.summary?.excluded_group_indices || []));
    } catch (requestError) {
      setError(readablePluginError(requestError, "\u5BFC\u5165\u6279\u6B21\u8BFB\u53D6\u5931\u8D25\u3002"));
    } finally {
      setBusy("");
    }
  };
  const resetPreview = () => {
    if (busy) return;
    setBatch(null);
    setFiles([]);
    setSubjectDrafts({});
    setPreviewQuery("");
    setExcludedGroupIndices(/* @__PURE__ */ new Set());
    if (inputRef.current) inputRef.current.value = "";
  };
  const createPreview = async () => {
    if (!files.length || busy) return;
    setBusy("preview");
    setError("");
    const payload = new FormData();
    files.forEach((file) => payload.append("files", file));
    try {
      const response = await client.post("preview/", payload);
      setBatch(response.data);
      setExcludedGroupIndices(/* @__PURE__ */ new Set());
      flash("\u89E3\u6790\u5B8C\u6210\uFF0C\u5C1A\u672A\u5199\u5165\u756A\u5267\u5E93");
    } catch (requestError) {
      setError(readablePluginError(requestError, "\u89C2\u770B\u8BB0\u5F55\u89E3\u6790\u5931\u8D25\u3002"));
    } finally {
      setBusy("");
    }
  };
  const resolveAll = async () => {
    if (!batch || resolvingRef.current) return;
    resolvingRef.current = true;
    setBusy("resolve");
    setError("");
    let current = batch;
    try {
      const hasSelectedPending = (candidate) => (candidate.groups || []).some((group, index) => !excludedGroupIndices.has(index) && group.resolution?.status === "pending");
      while (hasSelectedPending(current)) {
        const response = await client.post(`batches/${current.id}/resolve-next/`);
        current = response.data;
        setBatch(current);
      }
      const hasSelectedReview = (current.groups || []).some((group, index) => !excludedGroupIndices.has(index) && !["pending", "matched"].includes(group.resolution?.status));
      flash(hasSelectedReview ? "\u81EA\u52A8\u5339\u914D\u5B8C\u6210\uFF0C\u8BF7\u5904\u7406\u5F85\u786E\u8BA4\u6761\u76EE" : "Bangumi \u5339\u914D\u5B8C\u6210");
    } catch (requestError) {
      setError(readablePluginError(requestError, "Bangumi \u5206\u6279\u5339\u914D\u5931\u8D25\uFF0C\u53EF\u518D\u6B21\u7EE7\u7EED\u3002"));
    } finally {
      resolvingRef.current = false;
      setBusy("");
    }
  };
  const selectSubject = async (groupIndex, fallbackBangumiId) => {
    const bangumiId = Number(subjectDrafts[groupIndex] ?? fallbackBangumiId);
    if (!batch || !bangumiId || busy) return;
    setBusy(`select-${groupIndex}`);
    setError("");
    try {
      const response = await client.post(`batches/${batch.id}/select-subject/`, { group_index: groupIndex, bangumi_id: bangumiId });
      setBatch(response.data);
      flash("Bangumi \u6761\u76EE\u5DF2\u4EBA\u5DE5\u786E\u8BA4");
    } catch (requestError) {
      setError(readablePluginError(requestError, "Bangumi \u6761\u76EE\u786E\u8BA4\u5931\u8D25\u3002"));
    } finally {
      setBusy("");
    }
  };
  const commit = async () => {
    if (!batch || busy) return;
    setBusy("commit");
    setError("");
    try {
      const response = await client.post(`batches/${batch.id}/commit/`, {
        excluded_group_indices: [...excludedGroupIndices].sort((left, right) => left - right)
      });
      setBatch(response.data);
      flash("\u89C2\u770B\u8BB0\u5F55\u5DF2\u901A\u8FC7\u4E8B\u52A1\u5BFC\u5165");
    } catch (requestError) {
      setError(readablePluginError(requestError, "\u6B63\u5F0F\u5BFC\u5165\u88AB\u963B\u6B62\uFF0C\u8BF7\u5148\u5B8C\u6210\u5F85\u786E\u8BA4\u6761\u76EE\u3002"));
    } finally {
      setBusy("");
    }
  };
  const indexedGroups = useMemo(() => (batch?.groups || []).map((group, index) => ({ ...group, index })), [batch]);
  const visibleGroups = useMemo(() => {
    const query = previewQuery.trim().toLocaleLowerCase("zh-CN");
    if (!query) return indexedGroups;
    return indexedGroups.filter((group) => [
      group.source_title,
      group.resolution?.title,
      group.resolution?.japanese_title,
      group.resolution?.studio
    ].some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(query)));
  }, [indexedGroups, previewQuery]);
  const selectedGroups = useMemo(() => indexedGroups.filter((group) => !excludedGroupIndices.has(group.index)), [excludedGroupIndices, indexedGroups]);
  const selectedPendingCount = selectedGroups.filter((group) => group.resolution?.status === "pending").length;
  const reviewGroups = selectedGroups.filter((group) => !["pending", "matched"].includes(group.resolution?.status));
  const selectedMatchedCount = selectedGroups.filter((group) => group.resolution?.status === "matched").length;
  const selectedGroupCount = selectedGroups.length;
  const excludedGroupCount = indexedGroups.length - selectedGroupCount;
  const selectionSummary = batch ? {
    ...batch.summary,
    anime_groups: indexedGroups.length,
    matched: selectedMatchedCount,
    manual_review: reviewGroups.length
  } : {};
  const imported = batch?.status === "imported";
  const setGroupIncluded = (groupIndex, included) => {
    setExcludedGroupIndices((current) => {
      const next = new Set(current);
      if (included) next.delete(groupIndex);
      else next.add(groupIndex);
      return next;
    });
  };
  const setVisibleGroupsIncluded = (included) => {
    setExcludedGroupIndices((current) => {
      const next = new Set(current);
      visibleGroups.forEach((group) => {
        if (included) next.delete(group.index);
        else next.add(group.index);
      });
      return next;
    });
  };
  return /* @__PURE__ */ jsxs("main", { className: "ajp-watch-import", children: [
    /* @__PURE__ */ jsxs("header", { className: "ajp-watch-import__header", children: [
      /* @__PURE__ */ jsxs("div", { children: [
        /* @__PURE__ */ jsx2("span", { children: "WATCH HISTORY IMPORTER" }),
        /* @__PURE__ */ jsx2("h1", { children: "\u5FC6\u5F80\u6614\u89C2\u770B\u8BB0\u5F55\u5BFC\u5165\u5668" }),
        /* @__PURE__ */ jsx2("p", { children: "\u89E3\u6790\u6587\u6863\u3001\u5339\u914D Bangumi\u3001\u4EBA\u5DE5\u786E\u8BA4\uFF0C\u6700\u540E\u4E00\u6B21\u6027\u5199\u5165\u3002" })
      ] }),
      /* @__PURE__ */ jsxs(Link, { to: "/dashboard", children: [
        /* @__PURE__ */ jsx2(Icon, { name: "arrow-left" }),
        " \u8FD4\u56DE\u6211\u7684\u624B\u8D26"
      ] })
    ] }),
    loading ? /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__loading", children: [
      /* @__PURE__ */ jsx2(Icon, { name: "spinner", spin: true }),
      " \u6B63\u5728\u8FDE\u63A5\u63D2\u4EF6"
    ] }) : /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsxs("section", { className: "ajp-watch-import__workspace", children: [
        /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__source", children: [
          /* @__PURE__ */ jsxs(
            "label",
            {
              className: `ajp-watch-import__drop${dragging ? " is-dragging" : ""}`,
              onDragOver: (event) => {
                event.preventDefault();
                setDragging(true);
              },
              onDragLeave: () => setDragging(false),
              onDrop: (event) => {
                event.preventDefault();
                setDragging(false);
                chooseFiles(event.dataTransfer.files);
              },
              children: [
                /* @__PURE__ */ jsx2("input", { ref: inputRef, type: "file", accept: ".txt,text/plain", multiple: true, onChange: (event) => chooseFiles(event.target.files) }),
                /* @__PURE__ */ jsx2(Icon, { name: "file-upload" }),
                /* @__PURE__ */ jsx2("strong", { children: files.length ? `\u5DF2\u9009\u62E9 ${files.length} \u4E2A\u5E74\u5EA6\u6587\u6863` : "\u62D6\u5165 2021-2024 TXT \u6587\u6863" }),
                /* @__PURE__ */ jsx2("small", { children: files.length ? files.map((file) => file.name).join(" / ") : "\u4E5F\u53EF\u4EE5\u70B9\u51FB\u9009\u62E9\uFF0C\u5355\u4E2A\u6587\u4EF6\u6700\u5927 2 MB" })
              ]
            }
          ),
          /* @__PURE__ */ jsxs("button", { type: "button", className: "is-primary", disabled: !files.length || Boolean(batch) || busy === "preview", onClick: createPreview, children: [
            /* @__PURE__ */ jsx2(Icon, { name: "layers" }),
            " ",
            busy === "preview" ? "\u6B63\u5728\u89E3\u6790..." : "\u751F\u6210\u53EA\u8BFB\u9884\u89C8"
          ] }),
          !batch && data.batches.length > 0 && /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__recent", children: [
            /* @__PURE__ */ jsx2("strong", { children: "\u6700\u8FD1\u5BFC\u5165\u6279\u6B21" }),
            data.batches.map((item) => /* @__PURE__ */ jsxs("button", { type: "button", disabled: Boolean(busy), onClick: () => openBatch(item.id), children: [
              /* @__PURE__ */ jsx2("span", { children: item.source_names?.join(" / ") || `\u6279\u6B21 #${item.id}` }),
              /* @__PURE__ */ jsxs("small", { children: [
                item.status,
                " \xB7 ",
                item.summary?.anime_groups ?? 0,
                " \u90E8"
              ] })
            ] }, item.id))
          ] })
        ] }),
        /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__flow", children: [
          /* @__PURE__ */ jsxs("ol", { children: [
            /* @__PURE__ */ jsx2("li", { className: batch ? "is-done" : "is-active", children: "\u89E3\u6790\u6587\u6863" }),
            /* @__PURE__ */ jsx2("li", { className: selectedPendingCount === 0 && batch ? "is-done" : batch ? "is-active" : "", children: "Bangumi \u5339\u914D" }),
            /* @__PURE__ */ jsx2("li", { className: reviewGroups.length === 0 && selectedPendingCount === 0 && batch ? "is-done" : "", children: "\u4EBA\u5DE5\u6838\u5BF9" }),
            /* @__PURE__ */ jsx2("li", { className: imported ? "is-done" : "", children: "\u4E8B\u52A1\u5BFC\u5165" })
          ] }),
          batch ? /* @__PURE__ */ jsxs(Fragment, { children: [
            /* @__PURE__ */ jsx2(Summary, { summary: selectionSummary }),
            /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__actions", children: [
              /* @__PURE__ */ jsxs("button", { type: "button", disabled: Boolean(busy), onClick: resetPreview, children: [
                /* @__PURE__ */ jsx2(Icon, { name: "reset" }),
                " \u91CD\u65B0\u9009\u62E9\u6587\u6863"
              ] }),
              /* @__PURE__ */ jsxs("button", { type: "button", disabled: !selectedPendingCount || busy === "resolve" || imported, onClick: resolveAll, children: [
                /* @__PURE__ */ jsx2(Icon, { name: "search" }),
                " ",
                busy === "resolve" ? `\u6B63\u5728\u5339\u914D\uFF0C\u5269\u4F59 ${selectedPendingCount}` : "\u5F00\u59CB / \u7EE7\u7EED\u5339\u914D"
              ] }),
              /* @__PURE__ */ jsxs("button", { type: "button", className: "is-commit", disabled: !selectedGroupCount || selectedPendingCount > 0 || reviewGroups.length > 0 || busy === "commit" || imported, onClick: commit, children: [
                /* @__PURE__ */ jsx2(Icon, { name: "save" }),
                " ",
                imported ? "\u5DF2\u7ECF\u5BFC\u5165" : busy === "commit" ? "\u4E8B\u52A1\u5904\u7406\u4E2D..." : `\u786E\u8BA4\u5BFC\u5165 ${selectedGroupCount} \u90E8`
              ] })
            ] })
          ] }) : /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__empty", children: [
            /* @__PURE__ */ jsx2(Icon, { name: "history" }),
            /* @__PURE__ */ jsx2("p", { children: "\u4E0A\u4F20\u6587\u6863\u540E\uFF0C\u8FD9\u91CC\u4F1A\u663E\u793A\u5339\u914D\u8FDB\u5EA6\u548C\u5254\u9664\u62A5\u544A\u3002" })
          ] })
        ] })
      ] }),
      batch && /* @__PURE__ */ jsxs("section", { className: "ajp-watch-import__preview", children: [
        /* @__PURE__ */ jsxs("header", { children: [
          /* @__PURE__ */ jsxs("div", { children: [
            /* @__PURE__ */ jsx2("span", { children: "IMPORT SELECTION" }),
            /* @__PURE__ */ jsx2("h2", { children: "\u5BFC\u5165\u5185\u5BB9\u9884\u89C8" }),
            /* @__PURE__ */ jsx2("p", { children: "\u70B9\u51FB\u201C\u786E\u8BA4\u6B63\u5F0F\u5BFC\u5165\u201D\u524D\u4E0D\u4F1A\u5199\u5165\u756A\u5267\u5E93\u3002" })
          ] }),
          /* @__PURE__ */ jsxs("label", { children: [
            /* @__PURE__ */ jsx2(Icon, { name: "search" }),
            /* @__PURE__ */ jsx2("input", { value: previewQuery, onChange: (event) => setPreviewQuery(event.target.value), placeholder: "\u641C\u7D22\u6807\u9898\u6216\u5236\u4F5C\u516C\u53F8" })
          ] })
        ] }),
        /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__selection", "aria-live": "polite", children: [
          /* @__PURE__ */ jsxs("div", { children: [
            /* @__PURE__ */ jsxs("strong", { children: [
              "\u5C06\u5BFC\u5165 ",
              selectedGroupCount,
              " \u90E8"
            ] }),
            /* @__PURE__ */ jsxs("span", { children: [
              "\u5DF2\u6392\u9664 ",
              excludedGroupCount,
              " \u90E8 \xB7 \u5F53\u524D\u7ED3\u679C ",
              visibleGroups.length,
              " \u90E8"
            ] })
          ] }),
          /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__selection-actions", children: [
            /* @__PURE__ */ jsx2("button", { type: "button", className: "is-include", disabled: imported || !visibleGroups.length || visibleGroups.every((group) => !excludedGroupIndices.has(group.index)), onClick: () => setVisibleGroupsIncluded(true), children: "\u5F53\u524D\u7ED3\u679C\u5168\u90E8\u5BFC\u5165" }),
            /* @__PURE__ */ jsx2("button", { type: "button", className: "is-exclude", disabled: imported || !visibleGroups.length || visibleGroups.every((group) => excludedGroupIndices.has(group.index)), onClick: () => setVisibleGroupsIncluded(false), children: "\u5F53\u524D\u7ED3\u679C\u5168\u90E8\u6392\u9664" })
          ] })
        ] }),
        /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__preview-table", role: "region", "aria-label": "\u5BFC\u5165\u5185\u5BB9\u9884\u89C8", tabIndex: "0", children: [
          /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__preview-row is-header", children: [
            /* @__PURE__ */ jsx2("span", { children: "\u5BFC\u5165" }),
            /* @__PURE__ */ jsx2("span", { children: "\u6765\u6E90\u6807\u9898" }),
            /* @__PURE__ */ jsx2("span", { children: "\u89C2\u770B\u8BB0\u5F55" }),
            /* @__PURE__ */ jsx2("span", { children: "Bangumi \u7ED3\u679C" }),
            /* @__PURE__ */ jsx2("span", { children: "\u72B6\u6001" })
          ] }),
          visibleGroups.map((group) => {
            const excluded = excludedGroupIndices.has(group.index);
            return /* @__PURE__ */ jsxs("article", { className: `ajp-watch-import__preview-row${excluded ? " is-excluded" : ""}`, children: [
              /* @__PURE__ */ jsxs("label", { className: "ajp-watch-import__selection-control", children: [
                /* @__PURE__ */ jsx2("input", { type: "checkbox", checked: !excluded, disabled: imported, onChange: (event) => setGroupIncluded(group.index, event.target.checked), "aria-label": `\u5BFC\u5165 ${group.source_title}` }),
                /* @__PURE__ */ jsx2("span", { children: excluded ? "\u6392\u9664" : "\u5BFC\u5165" })
              ] }),
              /* @__PURE__ */ jsxs("div", { children: [
                /* @__PURE__ */ jsx2("strong", { children: group.source_title }),
                /* @__PURE__ */ jsx2("small", { children: group.records?.[0]?.source_file || "" })
              ] }),
              /* @__PURE__ */ jsxs("div", { children: [
                /* @__PURE__ */ jsx2("strong", { children: group.latest_watch_date_label || "\u65E5\u671F\u7F3A\u5931" }),
                /* @__PURE__ */ jsx2("small", { children: group.records?.map((record) => record.brush_label).join(" / ") })
              ] }),
              /* @__PURE__ */ jsxs("div", { children: [
                /* @__PURE__ */ jsx2(BangumiLink, { resolution: group.resolution, children: group.resolution?.title || "\u7B49\u5F85\u5339\u914D" }),
                /* @__PURE__ */ jsx2("small", { children: group.resolution?.studio || group.resolution?.japanese_title || "-" })
              ] }),
              /* @__PURE__ */ jsx2(StatusChip, { value: group.resolution?.status || "pending" })
            ] }, `${group.source_key}-${group.index}`);
          }),
          visibleGroups.length === 0 && /* @__PURE__ */ jsx2("p", { className: "ajp-watch-import__no-result", children: "\u6CA1\u6709\u7B26\u5408\u6761\u4EF6\u7684\u9884\u89C8\u6761\u76EE\u3002" })
        ] }),
        (batch.excluded || []).length > 0 && /* @__PURE__ */ jsxs("details", { className: "ajp-watch-import__excluded", children: [
          /* @__PURE__ */ jsxs("summary", { children: [
            "\u67E5\u770B\u5DF2\u5254\u9664\u8BB0\u5F55\uFF08",
            batch.excluded.length,
            "\uFF09"
          ] }),
          /* @__PURE__ */ jsx2("div", { children: batch.excluded.map((item) => /* @__PURE__ */ jsxs("article", { children: [
            /* @__PURE__ */ jsx2("strong", { children: item.source_title }),
            /* @__PURE__ */ jsx2("span", { children: item.exclusion_reason }),
            /* @__PURE__ */ jsxs("small", { children: [
              item.source_file,
              " \xB7 \u7B2C ",
              item.source_line,
              " \u884C"
            ] })
          ] }, `${item.source_file}-${item.source_line}-${item.source_title}`)) })
        ] })
      ] }),
      reviewGroups.length > 0 && /* @__PURE__ */ jsxs("section", { className: "ajp-watch-import__review", children: [
        /* @__PURE__ */ jsxs("header", { children: [
          /* @__PURE__ */ jsxs("div", { children: [
            /* @__PURE__ */ jsx2("span", { children: "MANUAL REVIEW" }),
            /* @__PURE__ */ jsx2("h2", { children: "\u9700\u8981\u4EBA\u5DE5\u786E\u8BA4\u7684\u756A\u5267" })
          ] }),
          /* @__PURE__ */ jsx2("b", { children: reviewGroups.length })
        ] }),
        /* @__PURE__ */ jsx2("div", { children: reviewGroups.map((group) => {
          const currentBangumiId = subjectDrafts[group.index] ?? String(group.resolution?.bangumi_id || "");
          const confirming = busy === `select-${group.index}`;
          return /* @__PURE__ */ jsxs("article", { children: [
            /* @__PURE__ */ jsxs("div", { children: [
              /* @__PURE__ */ jsx2(BangumiLink, { resolution: group.resolution, children: group.source_title }),
              /* @__PURE__ */ jsxs("small", { children: [
                group.records?.[0]?.source_file,
                " \xB7 ",
                group.records?.length || 0,
                " \u6761\u89C2\u770B\u8BB0\u5F55"
              ] }),
              group.resolution?.title && /* @__PURE__ */ jsxs("small", { className: "ajp-watch-import__candidate", children: [
                "\u5F53\u524D\u5019\u9009\uFF1A",
                group.resolution.title,
                " \xB7 ID ",
                group.resolution.bangumi_id
              ] })
            ] }),
            /* @__PURE__ */ jsx2(StatusChip, { value: group.resolution?.status }),
            /* @__PURE__ */ jsxs("label", { children: [
              /* @__PURE__ */ jsx2("span", { children: "Bangumi ID" }),
              /* @__PURE__ */ jsx2("input", { type: "number", value: currentBangumiId, onChange: (event) => setSubjectDrafts((current) => ({ ...current, [group.index]: event.target.value })) })
            ] }),
            /* @__PURE__ */ jsx2("button", { type: "button", disabled: !currentBangumiId || confirming, onClick: () => selectSubject(group.index, group.resolution?.bangumi_id), children: confirming ? "\u786E\u8BA4\u4E2D..." : group.resolution?.bangumi_id ? "\u76F4\u63A5\u786E\u8BA4" : "\u786E\u8BA4" })
          ] }, `${group.source_key}-${group.index}`);
        }) })
      ] })
    ] }),
    error && /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__message is-error", role: "alert", children: [
      /* @__PURE__ */ jsx2(Icon, { name: "warning" }),
      " ",
      error
    ] }),
    notice && /* @__PURE__ */ jsxs("div", { className: "ajp-watch-import__message is-success", role: "status", children: [
      /* @__PURE__ */ jsx2(Icon, { name: "check" }),
      " ",
      notice
    ] })
  ] });
}
var EMPTY;
var init_WatchHistoryImporterPage = __esm({
  "plugins/watch-history-importer/frontend/WatchHistoryImporterPage.jsx"() {
    init_Icon();
    init_errors();
    init_styles();
    EMPTY = { batches: [], config: {}, plugin: {} };
  }
});

// plugins/watch-history-importer/frontend/index.jsx
import { lazy } from "react";
import { jsx as jsx3 } from "react/jsx-runtime";
var WatchHistoryImporterPage2 = lazy(() => Promise.resolve().then(() => (init_WatchHistoryImporterPage(), WatchHistoryImporterPage_exports)));
function createPlugin(host) {
  if (!host?.api) throw new Error("watch-history-importer requires host.api");
  const pluginApi = host.api.plugin("watch-history-importer");
  return Object.freeze({
    id: "com.anime-journal.watch-history-importer",
    version: "0.4.0",
    routes: [{
      path: "/plugins/watch-history-importer",
      Component: (props) => /* @__PURE__ */ jsx3(WatchHistoryImporterPage2, { ...props, host, api: pluginApi }),
      area: "dashboard",
      access: "auth"
    }],
    navigation: [{
      id: "watch-history-importer.home",
      area: "dashboard",
      label: "\u5FC6\u5F80\u6614\u5BFC\u5165",
      path: "/plugins/watch-history-importer",
      icon: "history",
      order: 120
    }],
    dispose() {
    }
  });
}
export {
  createPlugin as default
};
