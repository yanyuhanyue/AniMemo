import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import gsap from "gsap";
import { Icon } from "./Icon.jsx";
import { RatingDisplay } from "./RatingDisplay.jsx";
import { TagChip } from "./TagChip.jsx";
import { EditAnimeRecordContent } from "./dashboard/EditAnimeRecordContent.jsx";
import { validateTrustedPosterUrl } from "../lib/posterSources.js";
import { buildPresetColorMap, resolveTagColors } from "../lib/tagPresets.js";

const statusOptions = [
  ["completed", "看过"],
  ["watching", "在看"],
  ["planned", "想看"],
];

function displayPeriod(period = "") {
  if (!period || period === "未定档") return "未定档";
  const [year, rawMonth] = period.split("-");
  const quarter = /^Q([1-4])$/i.exec(rawMonth || "");
  const month = quarter ? (Number(quarter[1]) - 1) * 3 + 1 : rawMonth;
  return `${year}年${month}月`;
}

function buildDeleteConfirmMessage(title = "") {
  const normalizedTitle = String(title).trim();
  const quotedTitle = /^《.*》$/.test(normalizedTitle) ? normalizedTitle : `《${normalizedTitle}》`;
  return `确定删除${quotedTitle}的私人追番记录吗？\n\n如果该作品已提交或已入选精选专栏，对应的精选专栏内容也会同步删除。\n\n此操作不可恢复。`;
}

function normalizeDraft(record) {
  if (!record) return record;
  return {
    ...record,
    tags: [...(record.tags || [])],
    tagColors: { ...(record.tagColors || {}) },
    watchHistory: (record.watchHistory || []).map((item) => ({ ...item, notes: [...(item.notes || [])] })),
  };
}

export function AnimeModal({ record, returnFocus, onClose, editable = false, onSave, onDelete, onIdentityChange, onOpenExternalAccount, initialTab = "overview", isDemo = false, tagPresets, trustedPosterHosts }) {
  const rootRef = useRef(null);
  const panelRef = useRef(null);
  const backdropRef = useRef(null);
  const closeRef = useRef(null);
  const closingRef = useRef(false);
  const closeFinishedRef = useRef(false);
  const openFinishedRef = useRef(false);
  const openingContextRef = useRef(null);
  const openingTimelineRef = useRef(null);
  const openingFallbackRef = useRef(null);
  const closingFallbackRef = useRef(null);
  const [tab, setTab] = useState("intro");
  const [draft, setDraft] = useState(() => normalizeDraft(record));
  const [phase, setPhase] = useState("opening");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [posterFile, setPosterFile] = useState(null);

  useEffect(() => {
    setDraft(normalizeDraft(record));
    setTab(editable ? "tags" : "intro");
    setPhase("opening");
    setBusy(false);
    setError("");
    setPosterFile(null);
  }, [editable, record]);

  const finishClose = useCallback(() => {
    if (closeFinishedRef.current) return;
    closeFinishedRef.current = true;
    window.clearTimeout(closingFallbackRef.current);
    document.body.classList.remove("modal-open");
    returnFocus?.focus?.({ preventScroll: true });
    onClose?.();
  }, [onClose, returnFocus]);

  const finishOpen = useCallback(() => {
    if (openFinishedRef.current || closingRef.current) return;
    openFinishedRef.current = true;
    window.clearTimeout(openingFallbackRef.current);
    openingTimelineRef.current?.kill();
    const root = rootRef.current;
    const panel = panelRef.current;
    const backdrop = backdropRef.current;
    if (!root || !panel || !backdrop) return;

    root.style.visibility = "visible";
    document.body.classList.add("modal-open");
    gsap.set([panel, backdrop], { clearProps: "transform,opacity,boxShadow" });
    gsap.set(root.querySelectorAll("[data-modal-reveal], [data-modal-stage], .anime-edit-modal__piece"), { clearProps: "transform,opacity,visibility" });
    setPhase("opened");
    closeRef.current?.focus({ preventScroll: true });
  }, []);

  const requestClose = useCallback(() => {
    if (closingRef.current || !panelRef.current) return;
    closingRef.current = true;
    openFinishedRef.current = true;
    window.clearTimeout(openingFallbackRef.current);
    setPhase("closing");
    const panel = panelRef.current;
    const backdrop = backdropRef.current;
    const revealTargets = rootRef.current?.querySelectorAll(editable ? ".anime-edit-modal__piece" : "[data-modal-stage]") || [];
    openingTimelineRef.current?.kill();
    gsap.killTweensOf([panel, backdrop, ...revealTargets]);
    gsap.set(revealTargets, { clearProps: "transform,opacity" });
    gsap.set(panel, { x: 0, y: 0, scale: 1, scaleX: 1, scaleY: 1, rotation: 0, autoAlpha: 1 });
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      finishClose();
      return;
    }

    const timeline = gsap.timeline({ onComplete: finishClose, onInterrupt: finishClose });
    if (editable) {
      timeline.to(panel, {
        y: 50,
        scale: .82,
        rotation: 2,
        autoAlpha: 0,
        duration: .24,
        ease: "power3.in",
      }, 0).to(backdrop, { opacity: 0, duration: .12, ease: "power2.in" }, "-=0.08");
    } else {
      timeline.to(backdrop, { opacity: 0, duration: .2, ease: "power2.in" }, 0);
      timeline.to(panel, {
        y: 12,
        opacity: 0,
        boxShadow: "5px 5px 0 #000",
        duration: .22,
        ease: "power3.in",
      }, 0);
    }
    gsap.ticker.wake();
    closingFallbackRef.current = window.setTimeout(finishClose, 520);
  }, [editable, finishClose]);

  useEffect(() => {
    if (!record) return undefined;
    document.body.classList.add("modal-open");
    return () => document.body.classList.remove("modal-open");
  }, [record]);

  useLayoutEffect(() => {
    if (!record || !rootRef.current || !panelRef.current) return undefined;
    closingRef.current = false;
    closeFinishedRef.current = false;
    openFinishedRef.current = false;
    window.clearTimeout(openingFallbackRef.current);
    window.clearTimeout(closingFallbackRef.current);
    document.body.classList.add("modal-open");
    const root = rootRef.current;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const panel = panelRef.current;
    const backdrop = backdropRef.current;
    const revealTargets = root.querySelectorAll(editable ? ".anime-edit-modal__piece" : "[data-modal-stage]");
    const shellTargets = root.querySelectorAll('[data-modal-stage="shell"]');
    const metaTargets = root.querySelectorAll('[data-modal-stage="meta"]');
    const navigationTargets = root.querySelectorAll('[data-modal-stage="navigation"]');
    const copyTargets = root.querySelectorAll('[data-modal-stage="copy"]');
    root.style.visibility = "visible";
    openingTimelineRef.current?.kill();
    openingContextRef.current?.revert();
    const context = gsap.context(() => {
      if (reduced) {
        gsap.set([panel, backdrop], { clearProps: "all" });
        gsap.set(revealTargets, { clearProps: "transform,opacity" });
        finishOpen();
        return;
      }
      gsap.set(backdrop, { opacity: 0 });
      if (editable) {
        gsap.set(revealTargets, {
          autoAlpha: 0,
          y: -16,
          scale: .96,
        });
        gsap.set(panel, {
          autoAlpha: 0,
          scale: .7,
          y: -50,
          rotation: -2,
          transformOrigin: "50% 50%",
        });
        openingTimelineRef.current = gsap.timeline({ onComplete: finishOpen })
          .to(backdrop, { opacity: 1, duration: .14, ease: "power1.out" }, 0)
          .to(panel, {
            y: 0,
            scale: 1,
            rotation: 0,
            autoAlpha: 1,
            duration: .62,
            ease: "back.out(1.5)",
            clearProps: "transform,opacity,visibility",
          }, 0)
          .to(revealTargets, {
            y: 0,
            scale: 1,
            autoAlpha: 1,
            duration: .3,
            stagger: .04,
            ease: "back.out(1.5)",
            clearProps: "transform,opacity,visibility",
          }, .18);
      } else {
        gsap.set(panel, { y: 14, opacity: 0, boxShadow: "5px 5px 0 #000" });
        gsap.set(shellTargets, { y: 8, opacity: 0 });
        gsap.set(metaTargets, { y: 12, opacity: 0 });
        gsap.set(navigationTargets, { y: 10, opacity: 0 });
        gsap.set(copyTargets, { y: 10, opacity: 0 });
        openingTimelineRef.current = gsap.timeline({ onComplete: finishOpen })
          .to(backdrop, { opacity: 1, duration: .2, ease: "power2.out" }, 0)
          .to(panel, {
            y: 0,
            opacity: 1,
            boxShadow: "12px 12px 0 #000",
            duration: .24,
            ease: "power3.out",
          }, .02)
          .to(shellTargets, {
            y: 0,
            opacity: 1,
            duration: .16,
            stagger: .025,
            ease: "power2.out",
          }, .08)
          .to(metaTargets, {
            y: 0,
            opacity: 1,
            duration: .23,
            stagger: .04,
            ease: "back.out(1.12)",
          }, .16)
          .to(navigationTargets, {
            y: 0,
            opacity: 1,
            duration: .2,
            stagger: .035,
            ease: "power3.out",
          }, .29)
          .to(copyTargets, {
            y: 0,
            opacity: 1,
            duration: .18,
            ease: "power2.out",
          }, .36);
      }
      gsap.ticker.wake();
      openingFallbackRef.current = window.setTimeout(finishOpen, 1600);
    }, rootRef);
    openingContextRef.current = context;
    return () => {
      window.clearTimeout(openingFallbackRef.current);
      openingTimelineRef.current?.kill();
      context.revert();
    };
  }, [editable, finishOpen, record]);

  useEffect(() => {
    if (phase !== "opened") return undefined;
    const frame = window.requestAnimationFrame(() => closeRef.current?.focus({ preventScroll: true }));
    return () => window.cancelAnimationFrame(frame);
  }, [phase]);

  useEffect(() => {
    if (!record) return undefined;
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        requestClose();
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = [...panelRef.current.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [record, requestClose]);

  if (!record || !draft) return null;
  const posterUrl = draft.posterOriginal || draft.poster;
  const update = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const save = async () => {
    setBusy(true);
    setError("");
    try {
      if (!draft.title.trim() || !String(draft.period || "").trim()) throw new Error("番剧中文名和放送季度不能为空。");
      const posterUrlError = posterFile ? "" : validateTrustedPosterUrl(draft.customPosterUrl, trustedPosterHosts);
      if (posterUrlError) throw new Error(posterUrlError);
      const score = draft.score === "" || draft.score === null ? null : Number(draft.score);
      if (score !== null && (!Number.isFinite(score) || score < 0 || score > 10)) throw new Error("评分必须是 0 到 10 之间的数字。");
      const tags = [...new Set((draft.tags || []).map((tag) => tag.trim()).filter(Boolean))];
      const tagColors = resolveTagColors(tags, draft.tagColors, buildPresetColorMap(tagPresets));
      await onSave?.({ ...draft, score, tags, tagColors, posterFile });
      requestClose();
    } catch (saveError) {
      setError(saveError?.message || "保存失败，请检查填写内容。");
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!window.confirm(buildDeleteConfirmMessage(draft.title))) return;
    setBusy(true);
    setError("");
    try {
      await onDelete?.(record.id);
      requestClose();
    } catch (deleteError) {
      setError(deleteError?.message || "删除失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`anime-modal is-${phase}`} ref={rootRef} role="dialog" aria-modal="true" aria-labelledby="anime-modal-title">
      <button ref={backdropRef} className="anime-modal__backdrop" aria-label="关闭详情" onClick={requestClose} />
      <section className={`anime-modal__panel${editable ? " anime-modal__panel--editable anime-edit-modal" : " anime-modal__panel--catalog"}`} ref={panelRef}>
        {editable ? (
          <EditAnimeRecordContent
            draft={draft}
            setDraft={setDraft}
            posterFile={posterFile}
            setPosterFile={setPosterFile}
            busy={busy}
            error={error}
            closeRef={closeRef}
            onSave={save}
            onDelete={onDelete ? remove : null}
            onClose={requestClose}
            tagPresets={tagPresets}
            trustedPosterHosts={trustedPosterHosts}
            onIdentityChange={onIdentityChange}
            onOpenExternalAccount={onOpenExternalAccount}
            initialTab={initialTab}
            isDemo={isDemo}
          />
        ) : <>
          <div className="anime-modal__stripe" aria-hidden="true"><span /><span /><span /></div>
          <div className="anime-modal__body">
          <header className="anime-modal__header">
            <div data-modal-reveal data-modal-stage="shell" data-reveal-x="-42" data-reveal-y="0">
              <span className="micro-label">ANIME FILE / 番剧档案</span>
              {editable ? <input id="anime-modal-title" className="modal-title-input" value={draft.title} onChange={(event) => update("title", event.target.value)} aria-label="番剧名称" /> : <h2 id="anime-modal-title">{draft.title}</h2>}
              <p>{draft.japaneseTitle}</p>
            </div>
            <button ref={closeRef} className="square-icon-button coral" type="button" onClick={requestClose} aria-label="关闭详情" data-modal-reveal data-modal-stage="shell" data-reveal-x="28" data-reveal-y="0"><Icon name="close" /></button>
          </header>

          <div className="anime-modal__content">
            <aside className="anime-modal__poster-column" data-modal-reveal data-modal-stage="shell" data-reveal-x="-48" data-reveal-y="0">
              <div className="anime-modal__poster-frame anime-modal__poster-viewer">
                <img src={draft.poster} alt={`${draft.title} 海报`} />
                {posterUrl && (
                  <a className="anime-modal__poster-action" href={posterUrl} target="_blank" rel="noreferrer" aria-label={`查看${draft.title}原始海报`}>
                    <Icon name="arrow-up-right" />
                    <span>查看原图</span>
                  </a>
                )}
              </div>
              <p className="source-label">当前海报来源 · {editable ? "R2 / URL" : "本地样例"}</p>
              {editable ? <input className="brutal-input small" value={draft.poster} onChange={(event) => update("poster", event.target.value)} aria-label="海报地址" /> : <div className="source-url">{draft.poster}</div>}
              <a className="external-card" href={draft.baikeUrl} target="_blank" rel="noreferrer"><span><Icon name="book" /> 前往萌娘百科查看完整信息</span><strong>点击跳转 <Icon name="arrow-right" /></strong></a>
            </aside>

            <div className="anime-modal__details">
              <div className="fact-grid">
                <label className="fact-card yellow" data-modal-reveal data-modal-stage="meta" data-reveal-x="34" data-reveal-y="0"><span>放送季度</span>{editable ? <input value={draft.period} onChange={(event) => update("period", event.target.value)} /> : <strong>{displayPeriod(draft.period)}</strong>}</label>
                <label className="fact-card pink" data-modal-reveal data-modal-stage="meta" data-reveal-x="34" data-reveal-y="0"><span>综合评分</span>{editable ? <input type="number" min="0" max="10" step="0.1" value={draft.score ?? ""} onChange={(event) => update("score", event.target.value)} /> : <RatingDisplay score={draft.score} compact />}</label>
                <label className="fact-card teal" data-modal-reveal data-modal-stage="meta" data-reveal-x="34" data-reveal-y="0"><span>制作公司</span>{editable ? <input value={draft.studio} onChange={(event) => update("studio", event.target.value)} /> : <strong>{draft.studio}</strong>}</label>
                <label className="fact-card white" data-modal-reveal data-modal-stage="meta" data-reveal-x="34" data-reveal-y="0"><span>话数情况</span>{editable ? <input value={draft.episodes} onChange={(event) => update("episodes", event.target.value)} /> : <strong>{draft.episodes}</strong>}</label>
              </div>
              {editable && <label className="modal-status-field" data-modal-stage="navigation"><span>观看状态</span><select value={draft.status} onChange={(event) => { const option = statusOptions.find(([value]) => value === event.target.value); setDraft((current) => ({ ...current, status: event.target.value, statusLabel: option?.[1] || "想看" })); }}>{statusOptions.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>}
              <div className="modal-tags" data-modal-reveal data-modal-stage="navigation" data-reveal-x="28" data-reveal-y="0">
                <span>标签分类 / TAGS</span>
                {editable && <input className="brutal-input small tag-editor-input" value={draft.tags.join("，")} onChange={(event) => update("tags", event.target.value.split(/[，,]/).map((tag) => tag.trim()).filter(Boolean))} aria-label="标签，使用逗号分隔" placeholder="日常，治愈，冒险" />}
                <div>{draft.tags.map((tag) => <TagChip key={tag} tag={tag} color={draft.tagColors?.[tag]} />)}</div>
                {editable && draft.tags.length > 0 && <div className="tag-color-editor">{draft.tags.map((tag) => <label key={tag}><input type="color" value={draft.tagColors?.[tag] || "#ffe66d"} onChange={(event) => update("tagColors", { ...(draft.tagColors || {}), [tag]: event.target.value })} /><span>{tag}</span></label>)}</div>}
              </div>
              <div className="modal-copy-tabs" role="tablist" aria-label="详情内容" data-modal-reveal data-modal-stage="navigation" data-reveal-y="20">
                <button type="button" className={tab === "intro" ? "active" : ""} onClick={() => setTab("intro")} role="tab" aria-selected={tab === "intro"}><Icon name="list" /> 剧情简介</button>
                <button type="button" className={tab === "review" ? "active" : ""} onClick={() => setTab("review")} role="tab" aria-selected={tab === "review"}><Icon name="edit" /> 个人评价</button>
              </div>
              {editable ? <textarea className="modal-copy" value={tab === "intro" ? draft.description : draft.review} onChange={(event) => update(tab === "intro" ? "description" : "review", event.target.value)} aria-label={tab === "intro" ? "剧情简介" : "个人评价"} data-modal-reveal data-modal-stage="copy" data-reveal-y="24" /> : <div className="modal-copy" data-modal-reveal data-modal-stage="copy" data-reveal-y="24">{tab === "intro" ? draft.description : draft.review || "还没有写下个人评价。"}</div>}
            </div>
          </div>

          <footer className="anime-modal__footer" data-modal-reveal data-modal-stage="shell" data-reveal-y="28">
            {editable && onDelete && <button className="brutal-button coral compact" type="button" onClick={remove} disabled={busy}><Icon name="trash" /> 删除记录</button>}
            <span />
            {editable && <button className="brutal-button yellow compact" type="button" onClick={save} disabled={busy}><Icon name="save" /> {busy ? "正在保存..." : "保存修改"}</button>}
            <button className="brutal-button white compact" type="button" onClick={requestClose} disabled={busy}>关闭</button>
          </footer>
          </div>
        </>}
      </section>
    </div>
  );
}
