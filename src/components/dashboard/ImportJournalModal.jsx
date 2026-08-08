import { useEffect, useLayoutEffect, useRef } from "react";
import gsap from "gsap";
import { Icon } from "../Icon.jsx";

const STATUS_LABELS = {
  ready: "待导入",
  duplicate: "跳过重复",
  invalid: "格式错误",
};

export function ImportJournalModal({ fileName, preview, busy = false, error = "", onClose, onConfirm }) {
  const rootRef = useRef(null);
  const panelRef = useRef(null);
  const timelineRef = useRef(null);

  useLayoutEffect(() => {
    const root = rootRef.current;
    const panel = panelRef.current;
    if (!root || !panel) return undefined;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return undefined;
    timelineRef.current = gsap.fromTo(
      panel,
      { autoAlpha: 0, y: -24, rotation: -1.2, scale: .96 },
      { autoAlpha: 1, y: 0, rotation: 0, scale: 1, duration: .38, ease: "back.out(1.2)", clearProps: "transform,opacity,visibility" },
    );
    return () => timelineRef.current?.kill();
  }, []);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event) => {
      if (event.key === "Escape" && !busy) onClose?.();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [busy, onClose]);

  const items = preview?.items || [];
  const ready = Number(preview?.ready || 0);
  const duplicates = Number(preview?.skipped_duplicates || 0);
  const invalid = Number(preview?.errors?.length || 0);

  return (
    <div className="dashboard-import-modal" ref={rootRef} role="dialog" aria-modal="true" aria-labelledby="dashboard-import-title">
      <button className="dashboard-import-modal__backdrop" type="button" aria-label="关闭导入预览" onClick={() => !busy && onClose?.()} />
      <section className="dashboard-import-modal__panel" ref={panelRef}>
        <header className="dashboard-import-modal__head">
          <div>
            <span className="dashboard-modal-kicker">IMPORT JOURNAL</span>
            <h2 id="dashboard-import-title">确认导入手账</h2>
            <p title={fileName}>{fileName || "手账备份文件"}</p>
          </div>
          <button className="dashboard-square-button" type="button" onClick={onClose} disabled={busy} aria-label="关闭">
            <Icon name="close" />
          </button>
        </header>

        {error ? <div className="dashboard-import-modal__error" role="alert"><Icon name="warning" /> {error}</div> : <>
          <div className="dashboard-import-modal__summary" aria-label="导入统计">
            <div className="is-yellow"><strong>{ready}</strong><span>待导入</span></div>
            <div className="is-teal"><strong>{duplicates}</strong><span>重复跳过</span></div>
            <div className="is-coral"><strong>{invalid}</strong><span>错误行</span></div>
          </div>
          <div className="dashboard-import-modal__note"><Icon name="bolt" /> 已有记录不会被覆盖；同名或同日文名会自动跳过。</div>
          <div className="dashboard-import-modal__rows" aria-label="导入预览">
            {items.slice(0, 12).map((item) => <div className={`dashboard-import-modal__row is-${item.status}`} key={`${item.row}-${item.title}`}><span>#{item.row}</span><strong title={item.title}>{item.title || "未命名记录"}</strong><em>{STATUS_LABELS[item.status] || item.status}</em></div>)}
            {items.length > 12 && <p className="dashboard-import-modal__more">还有 {items.length - 12} 条记录，确认后继续处理。</p>}
            {!items.length && <p className="dashboard-import-modal__empty">文件中没有可导入的记录。</p>}
          </div>
        </>}

        <footer className="dashboard-import-modal__actions">
          <button className="brutal-button white compact" type="button" onClick={onClose} disabled={busy}>取消</button>
          <button className="brutal-button yellow compact" type="button" onClick={onConfirm} disabled={busy || Boolean(error) || ready < 1}>
            {busy ? "正在导入..." : `确认导入 ${ready} 条`} <Icon name="arrow-right" />
          </button>
        </footer>
      </section>
    </div>
  );
}
