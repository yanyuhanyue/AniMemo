import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "../Icon.jsx";
import { WatchHistoryList } from "../WatchHistoryList.jsx";
import { buildPresetColorMap, FALLBACK_TAG_PRESETS, isCustomTagColor, TAG_COLOR_OPTIONS, tagColorKey, tagColorStyle } from "../../lib/tagPresets.js";
import { normalizeTrustedPosterHosts, validateTrustedPosterUrl } from "../../lib/posterSources.js";
import { ExternalMediaIdentityPanel } from "./ExternalMediaIdentityPanel.jsx";
import { api, readableApiError } from "../../lib/api.js";

const STATUS_OPTIONS = [
  ["planned", "想看"],
  ["watching", "在看"],
  ["completed", "看过"],
  ["on_hold", "搁置"],
  ["dropped", "弃番"],
];

const MODULES = [
  ["tags", "标签管理", "tags"],
  ["intro", "剧情简介", "list"],
  ["review", "个人评价", "edit"],
  ["external", "外部资料", "link"],
];

const BRUSH_NUMBERS = { 首刷: 1, 一刷: 1, 二刷: 2, 三刷: 3, 四刷: 4, 五刷: 5, 六刷: 6, 七刷: 7, 八刷: 8, 九刷: 9, 十刷: 10 };

function localDateValue() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function blankWatchHistoryRecord() {
  return { watchedOn: localDateValue(), brushLabel: "首刷", episodeStart: "", episodeEnd: "", notes: "" };
}

function formatWatchDate(value) {
  const [year, month, day] = String(value || "").split("-").map(Number);
  return year && month && day ? `${year}年${month}月${day}日` : "";
}

function optionalPositiveNumber(value) {
  if (value === "" || value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function watchHistoryKey(record) {
  return [record.watched_on, record.brush_label, record.episode_start ?? "", record.episode_end ?? ""].join("|");
}

function sortWatchHistory(records) {
  return [...records].sort((left, right) => String(right.watched_on || "").localeCompare(String(left.watched_on || "")));
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
  isDemo = false,
}) {
  const fileRef = useRef(null);
  const editorRef = useRef(null);
  const previousModuleRef = useRef("tags");
  const [module, setModule] = useState("tags");
  const [customTag, setCustomTag] = useState("");
  const [manualHistory, setManualHistory] = useState(blankWatchHistoryRecord);
  const [historyFormError, setHistoryFormError] = useState("");
  const [historyFormNotice, setHistoryFormNotice] = useState("");
  const [historyLoaded, setHistoryLoaded] = useState(isDemo);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historySaving, setHistorySaving] = useState(false);
  const presetColors = useMemo(() => buildPresetColorMap(tagPresets), [tagPresets]);

  useEffect(() => {
    setModule("tags");
    previousModuleRef.current = "tags";
    setCustomTag("");
    setManualHistory(blankWatchHistoryRecord());
    setHistoryFormError("");
    setHistoryFormNotice("");
    setHistoryLoaded(isDemo);
    setHistoryLoading(false);
    setHistorySaving(false);
    if (fileRef.current) fileRef.current.value = "";
  }, [draft.id]);

  const update = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const selectModule = (value) => {
    if (value !== "history") previousModuleRef.current = value;
    setModule(value);
    window.requestAnimationFrame(() => editorRef.current?.scrollIntoView({ block: "start" }));
  };
  const loadHistory = async () => {
    if (historyLoaded || historyLoading || isDemo || !Number.isFinite(Number(draft.id))) return;
    setHistoryLoading(true);
    setHistoryFormError("");
    try {
      const response = await api.get(`entries/${draft.id}/watch-history/`);
      const records = Array.isArray(response.data?.results) ? response.data.results : [];
      setDraft((current) => ({ ...current, watchHistory: records, watchHistoryCount: records.length }));
      setHistoryLoaded(true);
    } catch (requestError) {
      setHistoryFormError(readableApiError(requestError, "观看记录读取失败，请稍后重试。"));
    } finally {
      setHistoryLoading(false);
    }
  };
  const toggleHistoryModule = () => {
    const opening = module !== "history";
    setModule(opening ? "history" : previousModuleRef.current);
    if (opening) loadHistory();
    window.requestAnimationFrame(() => editorRef.current?.scrollIntoView({ block: "start" }));
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
    setDraft((current) => {
      if (current.tags.includes(tag)) return current;
      return {
        ...current,
        tags: [...current.tags, tag],
        tagColors: { ...(current.tagColors || {}), [tag]: tagColorKey(tag, current.tagColors?.[tag] || requestedColor, presetColors) },
      };
    });
  };
  const addCustomTag = () => {
    addTag(customTag);
    setCustomTag("");
  };
  const selectPoster = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!/^image\/(jpeg|png|webp)$/.test(file.type) || file.size > 5 * 1024 * 1024) {
      event.target.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setPosterFile(file);
      setDraft((current) => ({
        ...current,
        poster: String(reader.result),
        customPosterUrl: "",
        clearCustomPoster: false,
        posterSource: "upload",
      }));
    };
    reader.readAsDataURL(file);
  };

  const updatePosterUrl = (value) => {
    setPosterFile(null);
    if (fileRef.current) fileRef.current.value = "";
    const validationError = validateTrustedPosterUrl(value, trustedPosterHosts);
    setDraft((current) => ({
      ...current,
      customPosterUrl: value,
      clearCustomPoster: false,
      poster: value.trim() && !validationError ? value.trim() : current.posterUrl || current.poster,
      posterSource: value.trim() && !validationError ? "trusted_url" : current.posterSource,
    }));
  };

  const restoreDefaultPoster = () => {
    setPosterFile(null);
    if (fileRef.current) fileRef.current.value = "";
    setDraft((current) => ({
      ...current,
      poster: current.posterUrl || "/assets/posters/poster-01.webp",
      customPosterUrl: "",
      clearCustomPoster: true,
      posterSource: current.posterUrl ? "default_url" : "none",
    }));
  };

  const baikeEnabled = isHttpUrl(draft.baikeUrl);
  const poster = draft.poster || "/assets/posters/poster-01.webp";
  const posterUrlError = posterFile ? "" : validateTrustedPosterUrl(draft.customPosterUrl, trustedPosterHosts);
  const trustedHostLabel = normalizeTrustedPosterHosts(trustedPosterHosts).join("、");
  const hasCustomPoster = Boolean(posterFile || draft.customPosterUrl || draft.posterSource === "upload" || draft.posterSource === "trusted_url") && !draft.clearCustomPoster;
  const watchHistory = Array.isArray(draft.watchHistory) ? draft.watchHistory : [];

  const updateManualHistory = (key, value) => {
    setManualHistory((current) => ({ ...current, [key]: value }));
    setHistoryFormError("");
    setHistoryFormNotice("");
  };

  const addWatchRecord = async (event) => {
    event.preventDefault();
    const watchedOn = manualHistory.watchedOn;
    const brushLabel = manualHistory.brushLabel.trim() || "首刷";
    const episodeStart = optionalPositiveNumber(manualHistory.episodeStart);
    const episodeEnd = optionalPositiveNumber(manualHistory.episodeEnd);
    if (!watchedOn) {
      setHistoryFormError("请选择观看日期。");
      return;
    }
    if (manualHistory.episodeStart !== "" && episodeStart === null) {
      setHistoryFormError("起始话数必须是正整数。");
      return;
    }
    if (manualHistory.episodeEnd !== "" && episodeEnd === null) {
      setHistoryFormError("结束话数必须是正整数。");
      return;
    }
    if (episodeStart !== null && episodeEnd !== null && episodeEnd < episodeStart) {
      setHistoryFormError("结束话数不能小于起始话数。");
      return;
    }

    const nextRecord = {
      watched_on: watchedOn,
      watched_label: formatWatchDate(watchedOn),
      brush_number: BRUSH_NUMBERS[brushLabel] || null,
      brush_label: brushLabel,
      episode_start: episodeStart,
      episode_end: episodeEnd,
      notes: manualHistory.notes.trim() ? [manualHistory.notes.trim()] : [],
    };
    setHistorySaving(true);
    try {
      const saved = isDemo
        ? nextRecord
        : (await api.post(`entries/${draft.id}/watch-history/`, nextRecord)).data?.record;
      if (!saved) throw new Error("观看记录响应无效。");
      setDraft((current) => {
        const currentHistory = Array.isArray(current.watchHistory) ? current.watchHistory : [];
        const duplicateIndex = currentHistory.findIndex((record) => watchHistoryKey(record) === watchHistoryKey(saved));
        const nextHistory = duplicateIndex >= 0
          ? currentHistory.map((record, index) => index === duplicateIndex ? saved : record)
          : [saved, ...currentHistory];
        return { ...current, watchHistory: sortWatchHistory(nextHistory), watchHistoryCount: nextHistory.length };
      });
      setManualHistory((current) => ({ ...blankWatchHistoryRecord(), watchedOn: current.watchedOn }));
      setHistoryFormError("");
      setHistoryFormNotice(isDemo ? "已加入演示记录。" : "观看记录已保存。");
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
      setDraft((current) => {
        const nextHistory = (Array.isArray(current.watchHistory) ? current.watchHistory : []).filter((_, itemIndex) => itemIndex !== index);
        return { ...current, watchHistory: nextHistory, watchHistoryCount: nextHistory.length };
      });
      setHistoryFormError("");
      setHistoryFormNotice(isDemo ? "已从演示记录移除。" : "观看记录已删除。");
    } catch (requestError) {
      setHistoryFormError(readableApiError(requestError, "观看记录删除失败，请稍后重试。"));
    } finally {
      setHistorySaving(false);
    }
  };

  return (
    <>
      <header className="anime-edit-modal__header anime-edit-modal__piece">
        <div className="anime-edit-modal__header-fields">
          <span className="anime-edit-modal__kicker">EDIT RECORD</span>
          <div className="anime-edit-modal__title-grid">
            <label htmlFor="anime-modal-title">番剧名称（中文）
              <input id="anime-modal-title" value={draft.title} onChange={(event) => update("title", event.target.value)} />
            </label>
            <label htmlFor="anime-modal-japanese-title">番剧名称（日文）
              <input id="anime-modal-japanese-title" value={draft.japaneseTitle} onChange={(event) => update("japaneseTitle", event.target.value)} />
            </label>
          </div>
        </div>
        <button ref={closeRef} className="anime-edit-modal__close" type="button" onClick={onClose} disabled={busy} aria-label="关闭"><Icon name="close" /></button>
      </header>

      <div className="anime-edit-modal__body">
        <div className="anime-edit-modal__workspace">
          <aside className="anime-edit-modal__poster-column anime-edit-modal__piece">
            <div className="anime-edit-modal__poster"><img src={poster} alt={`${draft.title} 海报`} /></div>
            <label className="anime-edit-modal__upload">
              <Icon name="upload" /> {posterFile ? "已选择：更换文件" : "从本地上传海报"}
              <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" onChange={selectPoster} />
            </label>
            {posterFile && <p className="anime-edit-modal__file-name">{posterFile.name}</p>}
            <label className={`anime-edit-modal__source-field${posterUrlError ? " has-error" : ""}`} htmlFor="anime-modal-poster-url">受信任图片 URL
              <input id="anime-modal-poster-url" type="url" value={draft.customPosterUrl || ""} onChange={(event) => updatePosterUrl(event.target.value)} placeholder="https://lain.bgm.tv/..." aria-invalid={Boolean(posterUrlError)} aria-describedby="anime-modal-poster-help" />
            </label>
            <p id="anime-modal-poster-help" className={`anime-edit-modal__source-help${posterUrlError ? " has-error" : ""}`}>{posterUrlError || `允许域名：${trustedHostLabel}`}</p>
            <button className="anime-edit-modal__restore-poster" type="button" onClick={restoreDefaultPoster} disabled={!hasCustomPoster || busy}><Icon name="reset" /> 恢复公共 / Bangumi 封面</button>
            <label className="anime-edit-modal__source-field" htmlFor="anime-modal-baike-url">萌娘百科 URL
              <input id="anime-modal-baike-url" type="url" value={draft.baikeUrl || ""} onChange={(event) => update("baikeUrl", event.target.value)} placeholder="https://mzh.moegirl.org.cn/..." />
            </label>
            {baikeEnabled ? (
              <a className="anime-edit-modal__baike" href={draft.baikeUrl.trim()} target="_blank" rel="noreferrer"><Icon name="book" /> 前往萌娘百科</a>
            ) : (
              <span className="anime-edit-modal__baike is-disabled" aria-disabled="true"><Icon name="warning" /> 请输入有效的 HTTP(S) 地址</span>
            )}
          </aside>

          <div className="anime-edit-modal__main">
            <div className="anime-edit-modal__facts anime-edit-modal__piece">
              <label>放送季度<input value={draft.period || ""} onChange={(event) => update("period", event.target.value)} placeholder="2026-1" /></label>
              <label>制作公司<input value={draft.studio || ""} onChange={(event) => update("studio", event.target.value)} /></label>
              <label>话数情况<input value={draft.episodes || ""} onChange={(event) => update("episodes", event.target.value)} /></label>
              <label>主观评分<input className="is-score" type="number" min="0" max="10" step="0.1" value={draft.score ?? ""} onChange={(event) => update("score", event.target.value)} placeholder="0.0 - 10.0" /></label>
              <label>观看状态<select className="is-status" value={draft.status || "planned"} onChange={(event) => updateStatus(event.target.value)}>{STATUS_OPTIONS.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
              <button className={`anime-edit-modal__history-trigger${module === "history" ? " is-active" : ""}`} type="button" onClick={toggleHistoryModule} aria-pressed={module === "history"}>
                <span>{module === "history" ? "返回编辑" : "观看情况"}</span><strong><Icon name={module === "history" ? "arrow-left" : "history"} /> {module === "history" ? "返回上一项" : `${historyLoaded ? watchHistory.length : Number(draft.watchHistoryCount || 0)} 条记录`}</strong>
              </button>
            </div>

            <section className="anime-edit-modal__editor anime-edit-modal__piece" ref={editorRef}>
              {module === "tags" ? (
                <div className="anime-edit-modal__tag-layout">
                  <div className="anime-edit-modal__current-tags">
                    <div className="anime-edit-modal__section-heading"><span><Icon name="tags" /> 当前标签</span><strong>{draft.tags.length} 个</strong></div>
                    <div className="anime-edit-modal__tag-grid">
                      {draft.tags.map((tag, index) => {
                        const selectedColor = tagColorKey(tag, draft.tagColors?.[tag], presetColors);
                        return (
                          <div className="anime-edit-modal__tag-card" style={tagColorStyle(tag, selectedColor, presetColors)} key={`${tag}-${index}`}>
                            <div><input value={tag} onChange={(event) => renameTag(index, event.target.value)} aria-label={`修改标签 ${tag}`} /><button type="button" onClick={() => removeTag(index)} aria-label={`删除标签 ${tag}`}><Icon name="close" /></button></div>
                            <select value={selectedColor} onChange={(event) => setTagColor(tag, event.target.value)} aria-label={`${tag}的颜色`}>{TAG_COLOR_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</select>
                          </div>
                        );
                      })}
                    </div>
                    <div className="anime-edit-modal__custom-tag"><input value={customTag} onChange={(event) => setCustomTag(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addCustomTag(); } }} placeholder="自定义新标签" /><button type="button" onClick={addCustomTag}><Icon name="plus" />{"增加"}</button></div>
                  </div>

                  <div className="anime-edit-modal__presets">
                    <div className="anime-edit-modal__preset-heading"><strong><Icon name="wand" /> 快捷预设</strong><p>点击即可加入，再在左侧选择专属颜色。</p></div>
                    <div className="anime-edit-modal__preset-list">{tagPresets.map((preset) => {
                      const selected = draft.tags.includes(preset.name);
                      return <button type="button" disabled={selected} onClick={() => addTag(preset.name, preset.color)} style={tagColorStyle(preset.name, draft.tagColors?.[preset.name] || preset.color, presetColors)} key={preset.id || preset.name}><Icon name={selected ? "check" : "plus"} /> {preset.name}</button>;
                    })}</div>
                    <div className="anime-edit-modal__module-switch"><p>切换编辑模块</p><ModuleTabs module={module} onChange={selectModule} /></div>
                  </div>
                </div>
              ) : module === "history" ? (
                <div className="anime-edit-modal__history-panel">
                  <div className="anime-edit-modal__history-heading"><span><Icon name="history" /> 观看记录 / WATCH HISTORY</span><strong>{watchHistory.length} 次</strong></div>
                  <form className="anime-edit-modal__history-form" onSubmit={addWatchRecord}>
                    <div className="anime-edit-modal__history-fields">
                      <label>观看日期<input type="date" required value={manualHistory.watchedOn} onChange={(event) => updateManualHistory("watchedOn", event.target.value)} /></label>
                      <label>刷次<input list="watch-history-brush-options" maxLength="20" value={manualHistory.brushLabel} onChange={(event) => updateManualHistory("brushLabel", event.target.value)} placeholder="首刷" /></label>
                      <label>起始话数<input type="number" min="1" step="1" value={manualHistory.episodeStart} onChange={(event) => updateManualHistory("episodeStart", event.target.value)} placeholder="可选" /></label>
                      <label>结束话数<input type="number" min="1" step="1" value={manualHistory.episodeEnd} onChange={(event) => updateManualHistory("episodeEnd", event.target.value)} placeholder="可选" /></label>
                    </div>
                    <datalist id="watch-history-brush-options"><option value="首刷" /><option value="二刷" /><option value="三刷" /><option value="四刷" /><option value="补番" /></datalist>
                    <div className="anime-edit-modal__history-note-row">
                      <label>备注<input maxLength="500" value={manualHistory.notes} onChange={(event) => updateManualHistory("notes", event.target.value)} placeholder="例如：和朋友一起补完" /></label>
                      <button type="submit" disabled={historyLoading || historySaving}><Icon name="plus" /> {historySaving ? "保存中" : "添加观看记录"}</button>
                    </div>
                    {(historyFormError || historyFormNotice) && <p className={historyFormError ? "is-error" : "is-notice"} role={historyFormError ? "alert" : "status"}>{historyFormError || historyFormNotice}</p>}
                  </form>
                  {historyLoading ? <p className="anime-edit-modal__history-empty">正在读取观看记录...</p> : <WatchHistoryList records={watchHistory} editable={!historySaving} onRemove={removeWatchRecord} emptyText="还没有观看记录，可以在上方手动添加。" />}
                  <ModuleTabs module={module} onChange={selectModule} />
                </div>
              ) : module === "external" ? (
                <div className="external-media-layout">
                  <ModuleTabs module={module} onChange={selectModule} />
                  <ExternalMediaIdentityPanel draft={draft} setDraft={setDraft} onIdentityChange={onIdentityChange} isDemo={isDemo} />
                </div>
              ) : (
                <div className={`anime-edit-modal__copy-panel is-${module}`}>
                  <ModuleTabs module={module} onChange={selectModule} />
                  <textarea value={module === "intro" ? draft.description || "" : draft.review || ""} onChange={(event) => update(module === "intro" ? "description" : "review", event.target.value)} placeholder={module === "intro" ? "填写番剧剧情简介..." : "写下你的观看感受..."} aria-label={module === "intro" ? "剧情简介" : "个人评价"} />
                </div>
              )}
            </section>
            {error && <p className="anime-edit-modal__error anime-edit-modal__piece" role="alert"><Icon name="warning" /> {error}</p>}
          </div>
        </div>
      </div>

      <footer className="anime-edit-modal__footer anime-edit-modal__piece">
        {onDelete ? <button className="is-delete" type="button" onClick={onDelete} disabled={busy}><Icon name="trash" /> {busy ? "处理中..." : "删除私人记录"}</button> : <span />}
        <div><button className="is-save" type="button" onClick={onSave} disabled={busy}><Icon name="save" /> {busy ? "保存中..." : "保存全部修改"}</button><button type="button" onClick={onClose} disabled={busy}>关闭</button></div>
      </footer>
    </>
  );
}

function ModuleTabs({ module, onChange }) {
  return (
    <div className="anime-edit-modal__tabs" role="tablist" aria-label="切换编辑模块">
      {MODULES.map(([value, label, icon]) => <button type="button" role="tab" aria-selected={module === value} className={module === value ? "is-active" : ""} onClick={() => onChange(value)} key={value}><Icon name={icon} /> {label}</button>)}
    </div>
  );
}
