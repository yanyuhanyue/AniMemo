import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "../Icon.jsx";
import { WatchHistoryList } from "../WatchHistoryList.jsx";
import { buildPresetColorMap, FALLBACK_TAG_PRESETS, isCustomTagColor, TAG_COLOR_OPTIONS, tagColorKey, tagColorStyle } from "../../lib/tagPresets.js";
import { normalizeTrustedPosterHosts, validateTrustedPosterUrl } from "../../lib/posterSources.js";
import { deriveEntryStatistics, formatEpisodeRange, suggestNextEpisode } from "../../lib/journalExperience.js";
import { ExternalMediaIdentityPanel } from "./ExternalMediaIdentityPanel.jsx";
import { api, readableApiError } from "../../lib/api.js";

const STATUS_OPTIONS = [
  ["planned", "想看"],
  ["watching", "在看"],
  ["completed", "看过"],
  ["on_hold", "搁置"],
  ["dropped", "弃番"],
];

const HUB_TABS = [
  ["overview", "概览", "list"],
  ["history", "观看记录", "history"],
  ["statistics", "统计", "chart"],
  ["external", "外部资料", "link"],
];

const MODULES = [
  ["tags", "标签管理", "tags"],
  ["intro", "剧情简介", "list"],
  ["review", "个人评价", "edit"],
];

const BRUSH_NUMBERS = { 首刷: 1, 一刷: 1, 二刷: 2, 三刷: 3, 四刷: 4, 五刷: 5, 六刷: 6, 七刷: 7, 八刷: 8, 九刷: 9, 十刷: 10 };

function localDateValue() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function blankWatchHistoryRecord(suggestion = {}) {
  return { watchedOn: localDateValue(), brushLabel: "首刷", episodeStart: suggestion.episodeStart || "", episodeEnd: suggestion.episodeEnd || "", notes: "" };
}

function formatWatchDate(value) {
  const [year, month, day] = String(value || "").split("-").map(Number);
  return year && month && day ? `${year}年${month}月${day}日` : "";
}

function friendlyDate(value) {
  if (!value) return "暂无记录";
  const parsed = new Date(`${value}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("zh-CN");
}

function optionalPositiveNumber(value) {
  if (value === "" || value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function watchHistoryKey(record) {
  return [record.id || "", record.watched_on, record.brush_label, record.episode_start ?? "", record.episode_end ?? ""].join("|");
}

function sortWatchHistory(records) {
  return [...records].sort((left, right) => {
    const watchedDifference = String(right.watched_on || "").localeCompare(String(left.watched_on || ""));
    if (watchedDifference) return watchedDifference;
    return Number(right.sequence || right.id || 0) - Number(left.sequence || left.id || 0);
  });
}

function historySummary(records) {
  const sorted = sortWatchHistory(records);
  const stats = deriveEntryStatistics(sorted);
  const latest = sorted[0];
  return {
    watchHistory: sorted,
    watchHistoryCount: sorted.length,
    firstWatchedOn: stats.firstWatchedOn,
    lastWatchedOn: stats.lastWatchedOn,
    latestEpisodeStart: latest?.episode_start ?? null,
    latestEpisodeEnd: latest?.episode_end ?? null,
  };
}

function isHttpUrl(value) {
  try {
    const url = new URL(String(value || "").trim());
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function EditAnimeRecordContent({
  draft,
  setDraft,
  posterFile,
  setPosterFile,
  busy,
  error,
  closeRef,
  onSave,
  onDelete,
  onClose,
  tagPresets = FALLBACK_TAG_PRESETS,
  trustedPosterHosts,
  onIdentityChange,
  onOpenExternalAccount,
  initialTab = "overview",
  isDemo = false,
}) {
  const fileRef = useRef(null);
  const editorRef = useRef(null);
  const [hubTab, setHubTab] = useState(initialTab);
  const [module, setModule] = useState("tags");
  const [customTag, setCustomTag] = useState("");
  const [manualHistory, setManualHistory] = useState(blankWatchHistoryRecord);
  const [historyEditingKey, setHistoryEditingKey] = useState("");
  const [historyFormError, setHistoryFormError] = useState("");
  const [historyFormNotice, setHistoryFormNotice] = useState("");
  const [historyLoaded, setHistoryLoaded] = useState(isDemo);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historySaving, setHistorySaving] = useState(false);
  const [historyNextPage, setHistoryNextPage] = useState(null);
  const presetColors = useMemo(() => buildPresetColorMap(tagPresets), [tagPresets]);
  const watchHistory = Array.isArray(draft.watchHistory) ? draft.watchHistory : [];
  const statistics = useMemo(() => deriveEntryStatistics(watchHistory), [watchHistory]);
  const metadataSource = (draft.externalIdentities || []).find((identity) => identity.is_metadata_source);

  useEffect(() => {
    setHubTab(HUB_TABS.some(([value]) => value === initialTab) ? initialTab : "overview");
    setModule("tags");
    setCustomTag("");
    setManualHistory(blankWatchHistoryRecord());
    setHistoryEditingKey("");
    setHistoryFormError("");
    setHistoryFormNotice("");
    setHistoryLoaded(isDemo);
    setHistoryLoading(false);
    setHistorySaving(false);
    setHistoryNextPage(null);
    if (fileRef.current) fileRef.current.value = "";
  }, [draft.id, initialTab, isDemo]);

  const update = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const selectModule = (value) => {
    setModule(value);
    window.requestAnimationFrame(() => editorRef.current?.scrollIntoView({ block: "start" }));
  };

  const loadHistory = async (page = 1) => {
    if ((page === 1 && historyLoaded) || historyLoading || isDemo || !Number.isFinite(Number(draft.id))) return;
    setHistoryLoading(true);
    setHistoryFormError("");
    try {
      const response = await api.get(`entries/${draft.id}/watch-history/`, { params: { page, page_size: 100 } });
      const records = Array.isArray(response.data?.results) ? response.data.results : [];
      const current = page === 1 ? records : [...watchHistory, ...records];
      const unique = [...new Map(current.map((record) => [watchHistoryKey(record), record])).values()];
      setDraft((previous) => ({ ...previous, ...historySummary(unique), watchHistoryCount: Number(response.data?.count ?? unique.length) }));
      setHistoryNextPage(response.data?.next_page || null);
      setHistoryLoaded(true);
      if (page === 1) setManualHistory(blankWatchHistoryRecord(suggestNextEpisode(unique, draft.episodes)));
    } catch (requestError) {
      setHistoryFormError(readableApiError(requestError, "观看记录读取失败，请稍后重试。"));
    } finally {
      setHistoryLoading(false);
    }
  };

  const selectHubTab = (value) => {
    setHubTab(value);
    if (value === "history" || value === "statistics") loadHistory();
    window.requestAnimationFrame(() => editorRef.current?.scrollIntoView({ block: "start" }));
  };

  const toggleHistoryModule = () => {
    const opening = hubTab !== "history";
    selectHubTab(opening ? "history" : "overview");
  };

  const handleHubKeys = (event) => {
    if (!event.key.startsWith("Arrow")) return;
    const index = HUB_TABS.findIndex(([value]) => value === hubTab);
    const direction = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
    const next = HUB_TABS[(index + direction + HUB_TABS.length) % HUB_TABS.length][0];
    event.preventDefault();
    selectHubTab(next);
    document.getElementById(`entry-hub-tab-${next}`)?.focus();
  };

  const updateStatus = (value) => {
    const label = STATUS_OPTIONS.find(([key]) => key === value)?.[1] || "想看";
    setDraft((current) => ({ ...current, status: value, statusLabel: label }));
  };

  const renameTag = (index, value) => {
    setDraft((current) => {
      const previous = current.tags[index];
      const colors = { ...(current.tagColors || {}) };
      const nextTags = current.tags.map((tag, tagIndex) => tagIndex === index ? value : tag);
      const previousColor = isCustomTagColor(colors[previous]) ? colors[previous] : tagColorKey(previous, colors[previous], presetColors);
      delete colors[previous];
      if (value.trim()) colors[value] = previousColor;
      return { ...current, tags: nextTags, tagColors: colors };
    });
  };

  const removeTag = (index) => {
    setDraft((current) => {
      const colors = { ...(current.tagColors || {}) };
      delete colors[current.tags[index]];
      return { ...current, tags: current.tags.filter((_, tagIndex) => tagIndex !== index), tagColors: colors };
    });
  };

  const setTagColor = (tag, value) => update("tagColors", { ...(draft.tagColors || {}), [tag]: value });
  const addTag = (rawTag, requestedColor) => {
    const tag = rawTag.trim();
    if (!tag) return;
    setDraft((current) => current.tags.includes(tag) ? current : ({
      ...current,
      tags: [...current.tags, tag],
      tagColors: { ...(current.tagColors || {}), [tag]: tagColorKey(tag, current.tagColors?.[tag] || requestedColor, presetColors) },
    }));
  };

  const addCustomTag = () => { addTag(customTag); setCustomTag(""); };
  const selectPoster = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!/^image\/(jpeg|png|webp)$/.test(file.type) || file.size > 5 * 1024 * 1024) { event.target.value = ""; return; }
    const reader = new FileReader();
    reader.onload = () => {
      setPosterFile(file);
      setDraft((current) => ({ ...current, poster: String(reader.result), customPosterUrl: "", clearCustomPoster: false, posterSource: "upload" }));
    };
    reader.readAsDataURL(file);
  };

  const updatePosterUrl = (value) => {
    setPosterFile(null);
    if (fileRef.current) fileRef.current.value = "";
    const validationError = validateTrustedPosterUrl(value, trustedPosterHosts);
    setDraft((current) => ({ ...current, customPosterUrl: value, clearCustomPoster: false, poster: value.trim() && !validationError ? value.trim() : current.posterUrl || current.poster, posterSource: value.trim() && !validationError ? "trusted_url" : current.posterSource }));
  };

  const restoreDefaultPoster = () => {
    setPosterFile(null);
    if (fileRef.current) fileRef.current.value = "";
    setDraft((current) => ({ ...current, poster: current.posterUrl || "/assets/posters/poster-01.webp", customPosterUrl: "", clearCustomPoster: true, posterSource: current.posterUrl ? "default_url" : "none" }));
  };

  const updateManualHistory = (key, value) => {
    setManualHistory((current) => ({ ...current, [key]: value }));
    setHistoryFormError("");
    setHistoryFormNotice("");
  };

  const resetHistoryForm = (records = watchHistory) => {
    setManualHistory(blankWatchHistoryRecord(suggestNextEpisode(records, draft.episodes)));
    setHistoryEditingKey("");
  };

  const editWatchRecord = (index) => {
    const record = watchHistory[index];
    if (!record) return;
    setHistoryEditingKey(watchHistoryKey(record));
    setManualHistory({ watchedOn: record.watched_on || localDateValue(), brushLabel: record.brush_label || "首刷", episodeStart: record.episode_start ?? "", episodeEnd: record.episode_end ?? "", notes: Array.isArray(record.notes) ? record.notes.join(" · ") : "" });
    setHistoryFormError("");
    setHistoryFormNotice("正在编辑这条观看记录。");
  };

  const addWatchRecord = async (event) => {
    event.preventDefault();
    const watchedOn = manualHistory.watchedOn;
    const brushLabel = manualHistory.brushLabel.trim() || "首刷";
    const episodeStart = optionalPositiveNumber(manualHistory.episodeStart);
    const episodeEnd = optionalPositiveNumber(manualHistory.episodeEnd);
    if (!watchedOn) { setHistoryFormError("请选择观看日期。"); return; }
    if (manualHistory.episodeStart !== "" && episodeStart === null) { setHistoryFormError("起始话数必须是正整数。"); return; }
    if (manualHistory.episodeEnd !== "" && episodeEnd === null) { setHistoryFormError("结束话数必须是正整数。"); return; }
    if (episodeStart !== null && episodeEnd !== null && episodeEnd < episodeStart) { setHistoryFormError("结束话数不能小于起始话数。"); return; }

    const payload = { watched_on: watchedOn, watched_label: formatWatchDate(watchedOn), brush_number: BRUSH_NUMBERS[brushLabel] || null, brush_label: brushLabel, episode_start: episodeStart, episode_end: episodeEnd, notes: manualHistory.notes.trim() ? [manualHistory.notes.trim()] : [] };
    const editing = watchHistory.find((record) => watchHistoryKey(record) === historyEditingKey);
    setHistorySaving(true);
    try {
      const saved = isDemo
        ? { ...payload, id: editing?.id || `demo-${Date.now()}` }
        : editing
          ? (await api.patch(`entries/${draft.id}/watch-history/${editing.id}/`, payload)).data
          : (await api.post(`entries/${draft.id}/watch-history/`, payload)).data?.record;
      if (!saved) throw new Error("观看记录响应无效。");
      const nextHistory = editing
        ? watchHistory.map((record) => watchHistoryKey(record) === historyEditingKey ? saved : record)
        : watchHistory.some((record) => record.id === saved.id || watchHistoryKey(record) === watchHistoryKey(saved))
          ? watchHistory.map((record) => record.id === saved.id || watchHistoryKey(record) === watchHistoryKey(saved) ? saved : record)
          : [saved, ...watchHistory];
      const summary = historySummary(nextHistory);
      setDraft((current) => ({ ...current, ...summary }));
      resetHistoryForm(summary.watchHistory);
      setHistoryFormError("");
      setHistoryFormNotice(editing ? "观看记录已更新。" : "观看记录已保存。");
    } catch (requestError) {
      setHistoryFormError(readableApiError(requestError, "观看记录保存失败，请稍后重试。"));
    } finally {
      setHistorySaving(false);
    }
  };

  const removeWatchRecord = async (index) => {
    const record = watchHistory[index];
    setHistorySaving(true);
    try {
      if (!isDemo) await api.delete(`entries/${draft.id}/watch-history/${record.id}/`);
      const nextHistory = watchHistory.filter((_, itemIndex) => itemIndex !== index);
      setDraft((current) => ({ ...current, ...historySummary(nextHistory) }));
      if (watchHistoryKey(record) === historyEditingKey) resetHistoryForm(nextHistory);
      setHistoryFormError("");
      setHistoryFormNotice("观看记录已删除。");
    } catch (requestError) {
      setHistoryFormError(readableApiError(requestError, "观看记录删除失败，请稍后重试。"));
    } finally {
      setHistorySaving(false);
    }
  };

  const baikeEnabled = isHttpUrl(draft.baikeUrl);
  const poster = draft.poster || "/assets/posters/poster-01.webp";
  const posterUrlError = posterFile ? "" : validateTrustedPosterUrl(draft.customPosterUrl, trustedPosterHosts);
  const trustedHostLabel = normalizeTrustedPosterHosts(trustedPosterHosts).join("、");
  const hasCustomPoster = Boolean(posterFile || draft.customPosterUrl || draft.posterSource === "upload" || draft.posterSource === "trusted_url") && !draft.clearCustomPoster;

  return (
    <>
      <header className="anime-edit-modal__header anime-edit-modal__piece">
        <div className="anime-edit-modal__header-fields">
          <span className="anime-edit-modal__kicker">ENTRY HUB / 作品中心</span>
          <div className="anime-edit-modal__title-grid">
            <label htmlFor="anime-modal-title">番剧名称（中文）<input id="anime-modal-title" value={draft.title} onChange={(event) => update("title", event.target.value)} /></label>
            <label htmlFor="anime-modal-japanese-title">番剧名称（日文）<input id="anime-modal-japanese-title" value={draft.japaneseTitle} onChange={(event) => update("japaneseTitle", event.target.value)} /></label>
          </div>
        </div>
        <button ref={closeRef} className="anime-edit-modal__close" type="button" onClick={onClose} disabled={busy} aria-label="关闭"><Icon name="close" /></button>
      </header>

      <div className="entry-hub-tabs anime-edit-modal__piece" role="tablist" aria-label="作品中心" onKeyDown={handleHubKeys}>
        {HUB_TABS.map(([value, label, icon]) => <button id={`entry-hub-tab-${value}`} type="button" role="tab" aria-selected={hubTab === value} aria-controls={`entry-hub-panel-${value}`} tabIndex={hubTab === value ? 0 : -1} className={hubTab === value ? "is-active" : ""} onClick={() => selectHubTab(value)} key={value}><Icon name={icon} /> {label}</button>)}
      </div>

      <div className="anime-edit-modal__body" ref={editorRef}>
        {hubTab === "overview" && <div id="entry-hub-panel-overview" role="tabpanel" aria-labelledby="entry-hub-tab-overview" className="anime-edit-modal__workspace">
          <aside className="anime-edit-modal__poster-column anime-edit-modal__piece">
            <div className="anime-edit-modal__poster"><img src={poster} alt={`${draft.title} 海报`} /></div>
            <label className="anime-edit-modal__upload"><Icon name="upload" /> {posterFile ? "已选择：更换文件" : "从本地上传海报"}<input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" onChange={selectPoster} /></label>
            {posterFile && <p className="anime-edit-modal__file-name">{posterFile.name}</p>}
            <label className={`anime-edit-modal__source-field${posterUrlError ? " has-error" : ""}`} htmlFor="anime-modal-poster-url">受信任图片 URL<input id="anime-modal-poster-url" type="url" value={draft.customPosterUrl || ""} onChange={(event) => updatePosterUrl(event.target.value)} placeholder="https://lain.bgm.tv/..." aria-invalid={Boolean(posterUrlError)} aria-describedby="anime-modal-poster-help" /></label>
            <p id="anime-modal-poster-help" className={`anime-edit-modal__source-help${posterUrlError ? " has-error" : ""}`}>{posterUrlError || `允许域名：${trustedHostLabel}`}</p>
            <button className="anime-edit-modal__restore-poster" type="button" onClick={restoreDefaultPoster} disabled={!hasCustomPoster || busy}><Icon name="reset" /> 恢复公共 / Bangumi 封面</button>
            <label className="anime-edit-modal__source-field" htmlFor="anime-modal-baike-url">萌娘百科 URL<input id="anime-modal-baike-url" type="url" value={draft.baikeUrl || ""} onChange={(event) => update("baikeUrl", event.target.value)} placeholder="https://mzh.moegirl.org.cn/..." /></label>
            {baikeEnabled ? <a className="anime-edit-modal__baike" href={draft.baikeUrl.trim()} target="_blank" rel="noreferrer"><Icon name="book" /> 前往萌娘百科</a> : <span className="anime-edit-modal__baike is-disabled" aria-disabled="true"><Icon name="warning" /> 请输入有效的 HTTP(S) 地址</span>}
          </aside>

          <div className="anime-edit-modal__main">
            <div className="anime-edit-modal__facts anime-edit-modal__piece">
              <label>放送季度<input value={draft.period || ""} onChange={(event) => update("period", event.target.value)} placeholder="2026-1" /></label>
              <label>制作公司<input value={draft.studio || ""} onChange={(event) => update("studio", event.target.value)} /></label>
              <label>话数情况<input value={draft.episodes || ""} onChange={(event) => update("episodes", event.target.value)} /></label>
              <label>主观评分<input className="is-score" type="number" min="0" max="10" step="0.1" value={draft.score ?? ""} onChange={(event) => update("score", event.target.value)} placeholder="0.0 - 10.0" /></label>
              <label>观看状态<select className="is-status" value={draft.status || "planned"} onChange={(event) => updateStatus(event.target.value)}>{STATUS_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
              <button className="anime-edit-modal__history-trigger" type="button" onClick={toggleHistoryModule}><span>观看记录</span><strong><Icon name="history" /> {Number(draft.watchHistoryCount || watchHistory.length)} 条 · 去记录</strong></button>
            </div>
            <div className="entry-overview-source anime-edit-modal__piece"><span><Icon name="link" /> 外部资料来源</span><strong>{metadataSource ? `${metadataSource.provider === "bangumi" ? "Bangumi" : metadataSource.provider} · ${metadataSource.provider_title || metadataSource.metadata?.title || metadataSource.external_id}` : "尚未设置"}</strong><button type="button" onClick={() => selectHubTab("external")}>{metadataSource ? "管理" : "搜索并关联"} <Icon name="arrow-right" /></button></div>

            <section className="anime-edit-modal__editor anime-edit-modal__piece">
              {module === "tags" ? <div className="anime-edit-modal__tag-layout">
                <div className="anime-edit-modal__current-tags">
                  <div className="anime-edit-modal__section-heading"><span><Icon name="tags" /> 当前标签</span><strong>{draft.tags.length} 个</strong></div>
                  <div className="anime-edit-modal__tag-grid">{draft.tags.map((tag, index) => {
                    const selectedColor = tagColorKey(tag, draft.tagColors?.[tag], presetColors);
                    return <div className="anime-edit-modal__tag-card" style={tagColorStyle(tag, selectedColor, presetColors)} key={`${tag}-${index}`}><div><input value={tag} onChange={(event) => renameTag(index, event.target.value)} aria-label={`修改标签 ${tag}`} /><button type="button" onClick={() => removeTag(index)} aria-label={`删除标签 ${tag}`}><Icon name="close" /></button></div><select value={selectedColor} onChange={(event) => setTagColor(tag, event.target.value)} aria-label={`${tag}的颜色`}>{TAG_COLOR_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select></div>;
                  })}</div>
                  <div className="anime-edit-modal__custom-tag"><input value={customTag} onChange={(event) => setCustomTag(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addCustomTag(); } }} placeholder="自定义新标签" /><button type="button" onClick={addCustomTag}><Icon name="plus" /> 增加</button></div>
                </div>
                <div className="anime-edit-modal__presets"><div className="anime-edit-modal__preset-heading"><strong><Icon name="wand" /> 快捷预设</strong><p>点击即可加入，再在左侧选择专属颜色。</p></div><div className="anime-edit-modal__preset-list">{tagPresets.map((preset) => { const selected = draft.tags.includes(preset.name); return <button type="button" disabled={selected} onClick={() => addTag(preset.name, preset.color)} style={tagColorStyle(preset.name, draft.tagColors?.[preset.name] || preset.color, presetColors)} key={preset.id || preset.name}><Icon name={selected ? "check" : "plus"} /> {preset.name}</button>; })}</div><div className="anime-edit-modal__module-switch"><p>切换编辑模块</p><ModuleTabs module={module} onChange={selectModule} /></div></div>
              </div> : <div className={`anime-edit-modal__copy-panel is-${module}`}><ModuleTabs module={module} onChange={selectModule} /><textarea value={module === "intro" ? draft.description || "" : draft.review || ""} onChange={(event) => update(module === "intro" ? "description" : "review", event.target.value)} placeholder={module === "intro" ? "填写番剧剧情简介..." : "写下你的观看感受..."} aria-label={module === "intro" ? "剧情简介" : "个人评价"} /></div>}
            </section>
            {error && <p className="anime-edit-modal__error anime-edit-modal__piece" role="alert"><Icon name="warning" /> {error}</p>}
          </div>
        </div>}

        {hubTab === "history" && <section id="entry-hub-panel-history" role="tabpanel" aria-labelledby="entry-hub-tab-history" className="entry-hub-panel anime-edit-modal__history-panel">
          <div className="anime-edit-modal__history-heading"><span><Icon name="history" /> 观看记录 / WATCH HISTORY</span><strong>{Number(draft.watchHistoryCount || watchHistory.length)} 条</strong></div>
          <form className="anime-edit-modal__history-form" onSubmit={addWatchRecord}>
            <div className="anime-edit-modal__history-fields">
              <label>观看日期<input type="date" required value={manualHistory.watchedOn} onChange={(event) => updateManualHistory("watchedOn", event.target.value)} /></label>
              <label>刷次<input list="watch-history-brush-options" maxLength="20" value={manualHistory.brushLabel} onChange={(event) => updateManualHistory("brushLabel", event.target.value)} placeholder="首刷" /></label>
              <label>起始话数<input type="number" min="1" step="1" value={manualHistory.episodeStart} onChange={(event) => updateManualHistory("episodeStart", event.target.value)} placeholder="可选" /></label>
              <label>结束话数<input type="number" min="1" step="1" value={manualHistory.episodeEnd} onChange={(event) => updateManualHistory("episodeEnd", event.target.value)} placeholder="可选" /></label>
            </div>
            <datalist id="watch-history-brush-options"><option value="首刷" /><option value="二刷" /><option value="三刷" /><option value="四刷" /><option value="补番" /></datalist>
            <div className="anime-edit-modal__history-note-row"><label>备注<input maxLength="500" value={manualHistory.notes} onChange={(event) => updateManualHistory("notes", event.target.value)} placeholder="例如：和朋友一起补完" /></label><button type="submit" className="is-primary" disabled={historyLoading || historySaving}><Icon name={historyEditingKey ? "save" : "plus"} /> {historySaving ? "保存中" : historyEditingKey ? "保存修改" : "记录观看"}</button>{historyEditingKey && <button type="button" onClick={() => resetHistoryForm()} disabled={historySaving}>取消编辑</button>}</div>
            {(historyFormError || historyFormNotice) && <p className={historyFormError ? "is-error" : "is-notice"} role={historyFormError ? "alert" : "status"}>{historyFormError || historyFormNotice}</p>}
          </form>
          {historyLoading && !historyLoaded ? <p className="anime-edit-modal__history-empty"><Icon name="spinner" spin /> 正在读取观看记录...</p> : <WatchHistoryList records={watchHistory} editable={!historySaving} onEdit={editWatchRecord} onRemove={removeWatchRecord} emptyText="还没有观看记录，可以在上方记录第一次观看。" />}
          {historyNextPage && <button className="entry-hub-load-more" type="button" onClick={() => loadHistory(historyNextPage)} disabled={historyLoading}><Icon name={historyLoading ? "spinner" : "arrow-down"} spin={historyLoading} /> 加载更早记录</button>}
        </section>}

        {hubTab === "statistics" && <section id="entry-hub-panel-statistics" role="tabpanel" aria-labelledby="entry-hub-tab-statistics" className="entry-hub-panel entry-statistics">
          <div className="entry-hub-panel__heading"><div><span>ENTRY ANALYTICS</span><h2><Icon name="chart" /> 单作品统计</h2></div><p>仅使用 Core Watch History 中可可靠计算的数据。</p></div>
          {historyLoading && !historyLoaded ? <div className="entry-hub-loading"><Icon name="spinner" spin /> 正在计算...</div> : statistics.count ? <div className="entry-statistics__grid">
            <article><span>观看记录</span><strong>{statistics.count}</strong><small>共保存的 canonical 记录</small></article>
            <article><span>首次观看</span><strong>{friendlyDate(statistics.firstWatchedOn || draft.firstWatchedOn)}</strong><small>最早记录日期</small></article>
            <article><span>最近观看</span><strong>{friendlyDate(statistics.lastWatchedOn || draft.lastWatchedOn)}</strong><small>最近一次记录</small></article>
            <article><span>刷次记录</span><strong>{statistics.highestBrushNumber ? `最高第 ${statistics.highestBrushNumber} 刷` : `${statistics.count} 次观看`}</strong><small>{statistics.highestBrushNumber ? "依据明确 brush_number" : "未推断重刷次数"}</small></article>
            <article><span>已记录话数范围</span><strong>{statistics.episodeStart || statistics.episodeEnd ? formatEpisodeRange({ episode_start: statistics.episodeStart, episode_end: statistics.episodeEnd }) : "暂无范围"}</strong><small>不作为 canonical 追番进度</small></article>
          </div> : <div className="entry-hub-empty"><Icon name="chart" /><strong>数据还不够</strong><p>记录第一次观看后，这里会显示日期、刷次和集数范围。</p><button type="button" onClick={() => selectHubTab("history")}><Icon name="plus" /> 记录观看</button></div>}
        </section>}

        {hubTab === "external" && <section id="entry-hub-panel-external" role="tabpanel" aria-labelledby="entry-hub-tab-external" className="entry-hub-panel external-media-layout"><div className="entry-hub-panel__heading"><div><span>EXTERNAL DATA</span><h2><Icon name="link" /> 外部数据</h2></div><p>作品资料绑定与 Bangumi 收藏比较彼此独立。</p></div><ExternalMediaIdentityPanel draft={draft} setDraft={setDraft} onIdentityChange={onIdentityChange} onOpenExternalAccount={onOpenExternalAccount} isDemo={isDemo} /></section>}
      </div>

      <footer className="anime-edit-modal__footer anime-edit-modal__piece">
        {onDelete ? <button className="is-delete" type="button" onClick={onDelete} disabled={busy}><Icon name="trash" /> {busy ? "处理中..." : "删除私人记录"}</button> : <span />}
        <div><button className="is-save" type="button" onClick={onSave} disabled={busy}><Icon name="save" /> {busy ? "保存中..." : "保存全部修改"}</button><button type="button" onClick={onClose} disabled={busy}>关闭</button></div>
      </footer>
    </>
  );
}

function ModuleTabs({ module, onChange }) {
  return <div className="anime-edit-modal__tabs" role="tablist" aria-label="切换概览编辑模块">{MODULES.map(([value, label, icon]) => <button type="button" role="tab" aria-selected={module === value} className={module === value ? "is-active" : ""} onClick={() => onChange(value)} key={value}><Icon name={icon} /> {label}</button>)}</div>;
}
