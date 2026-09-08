import { useEffect, useLayoutEffect, useRef } from "react";
import gsap from "gsap";
import { Icon } from "../Icon.jsx";

const STATUS_LABELS = {
  ready: "待导入",
  duplicate: "跳过重复",
  invalid: "格式错误",
};

const RESTORE_LABELS = {
  idle: "选择 JSON 数据包开始恢复",
  checking: "正在查询恢复会话",
  hashing: "正在核对文件摘要",
  uploading: "正在上传文件",
  receiving: "文件已保存，可继续上传",
  validating: "正在校验完整数据包",
  ready: "校验完成，等待确认恢复",
  committing: "正在恢复手账并保存收据",
  completed: "恢复完成",
  paused: "恢复已暂停，可查询状态后继续",
  cancelling: "正在取消恢复并确认结果",
  cancelled: "恢复已取消",
  failed: "恢复未完成",
  expired: "恢复会话已过期",
  authentication_required: "请重新登录后恢复",
};

function formatBytes(value) {
  return `${(Math.max(0, Number(value) || 0) / (1024 * 1024)).toFixed(2)} MiB`;
}

export function ImportJournalModal({ fileName, preview, busy = false, error = "", restore = null, demo = false, onClose, onConfirm, onResume, onCancel, onChooseFile }) {
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
      if (event.key === "Escape" && (!busy || restore)) onClose?.();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [busy, onClose, restore]);

  const items = preview?.items || [];
  const ready = Number(preview?.ready || 0);
  const duplicates = Number(preview?.skipped_duplicates || 0);
  const invalid = Number(preview?.invalid_count || preview?.errors?.length || 0);
  const phase = restore?.phase;
  const session = restore?.session;
  const done = session?.state === "completed" && Boolean(restore?.receipt);
  const ended = ["completed", "cancelled", "failed", "expired"].includes(session?.state) || phase === "cancelled";
  const progress = restore?.progress;
  const needsFile = session?.state === "receiving" && !restore?.hasFile && session.received_bytes < session.expected_bytes;
  const canResume = Boolean(restore) && !busy && !ended && (session || phase === "paused");
  const canCancel = Boolean(restore) && !ended && phase !== "cancelling" && (session || busy || restore.hasFile);

  return (
    <div className="dashboard-import-modal" ref={rootRef} role="dialog" aria-modal="true" aria-labelledby="dashboard-import-title">
      <button className="dashboard-import-modal__backdrop" type="button" aria-label="关闭导入预览" onClick={() => (!busy || restore) && onClose?.()} />
      <section className="dashboard-import-modal__panel" ref={panelRef}>
        <header className="dashboard-import-modal__head">
          <div>
            <span className="dashboard-modal-kicker">IMPORT JOURNAL</span>
            <h2 id="dashboard-import-title">{done ? "手账恢复完成" : restore ? "恢复手账数据包" : "确认导入手账"}</h2>
            <p title={fileName}>{fileName || "手账备份文件"}</p>
          </div>
          <button className="dashboard-square-button" type="button" onClick={onClose} disabled={busy && !restore} aria-label="关闭">
            <Icon name="close" />
          </button>
        </header>

        <div style={{ minHeight: 0, overflowY: "auto" }}>
        {restore && <div className="dashboard-import-modal__note" aria-live="polite" aria-atomic="true">
          <Icon name="bolt" />
          <div style={{ minWidth: 0 }}>
          <div>{RESTORE_LABELS[phase] || "请查询恢复状态"}</div>
          {progress && <div>
            <progress aria-label={phase === "hashing" ? "文件摘要进度" : "文件上传进度"} value={progress.loaded} max={Math.max(1, progress.total)} />
            <span> {formatBytes(progress.loaded)} / {formatBytes(progress.total)}</span>
          </div>}
          {!done && !ended && <p>JSON 数据包只恢复到空手账；全部校验通过后一次完成。关闭窗口后可在当前账号下继续，刷新后需重新选择原文件核对摘要。</p>}
          {ended && !done && <p>此会话已结束。需要恢复时，请重新选择 JSON 数据包创建新的会话。</p>}
          {session?.cleanup_pending && <p>{done ? "暂存文件等待清理，完成收据保持可查询。" : "暂存文件等待清理，可继续查询会话状态。"}</p>}
          </div>
        </div>}
        {demo && <div className="dashboard-import-modal__note"><Icon name="bolt" /> 演示模式仅在本机处理 2 MiB 以内的文件，不创建服务器恢复会话。</div>}
        {error && <div className="dashboard-import-modal__error" role="alert"><Icon name="warning" /> {error}</div>}
        {done && <div className="dashboard-import-modal__note" role="status">
          <div style={{ minWidth: 0 }}>
          <p>已完整恢复 <strong>{restore.receipt.created}</strong> 条记录。</p>
          <details>
            <summary>查看完成收据</summary>
            <p style={{ overflowWrap: "anywhere" }}>恢复会话：{session.id}</p>
            <p style={{ overflowWrap: "anywhere" }}>文件 SHA-256：{session.sha256}</p>
            <p>数据包版本：v1 · 恢复数量：{restore.receipt.total}</p>
          </details>
          </div>
        </div>}
        {preview && !done && !ended && <>
          <div className="dashboard-import-modal__summary" aria-label="导入统计">
            <div className="is-yellow"><strong>{ready}</strong><span>待导入</span></div>
            <div className="is-teal"><strong>{restore ? preview.total : duplicates}</strong><span>{restore ? "包内记录" : "重复跳过"}</span></div>
            <div className="is-coral"><strong>{invalid}</strong><span>错误行</span></div>
          </div>
          {!restore && <div className="dashboard-import-modal__note"><Icon name="bolt" /> 已有记录不会被覆盖；同名或同日文名会自动跳过。</div>}
          {restore && invalid > 0 && <div className="dashboard-import-modal__error" role="alert">当前手账不是空目标，暂不能恢复此数据包。</div>}
          <div className="dashboard-import-modal__rows" aria-label="导入预览">
            {items.slice(0, 12).map((item) => <div className={`dashboard-import-modal__row is-${item.status}`} key={`${item.row}-${item.title}`}><span>#{item.row}</span><strong title={item.title}>{item.title || "未命名记录"}</strong><em>{STATUS_LABELS[item.status] || item.status}</em></div>)}
            {(items.length > 12 || preview.items_truncated) && <p className="dashboard-import-modal__more">此处展示前 {Math.min(12, items.length)} 条预览；文件共 {preview.total} 条记录{restore ? "，恢复时将处理完整数据包。" : "。"}</p>}
            {!items.length && !busy && <p className="dashboard-import-modal__empty">文件中没有可导入的记录。</p>}
          </div>
        </>}
        {!preview && !busy && !done && <div className="dashboard-import-modal__rows"><p className="dashboard-import-modal__empty">{needsFile ? "选择原始 JSON 文件，核对摘要后从已上传位置继续。" : session && !ended ? "上传进度已保留；数据包校验尚未完成，可查询状态并继续恢复。" : "请选择 JSON 数据包或 2 MiB 以内的 CSV 文件。"}</p></div>}
        </div>

        <footer className="dashboard-import-modal__actions" style={{ flexWrap: "wrap" }}>
          <button className="brutal-button white compact" type="button" onClick={onClose} disabled={busy && !restore}>{restore ? done || ended ? "关闭" : "稍后继续" : "取消"}</button>
          {!busy && <button className="brutal-button white compact" type="button" onClick={onChooseFile}>{needsFile ? "选择原文件" : ended ? "选择新文件" : "选择文件"}</button>}
          {canCancel && <button className="brutal-button white compact" type="button" onClick={onCancel}>取消恢复</button>}
          {canResume && (session?.state !== "ready" || error) && <button className="brutal-button yellow compact" type="button" onClick={onResume}>{needsFile ? "继续上传" : "查询并继续"}</button>}
          {!done && (!restore || session?.state === "ready") && <button className="brutal-button yellow compact" type="button" onClick={onConfirm} disabled={busy || Boolean(error) || (restore ? session?.state !== "ready" || invalid > 0 : ready < 1)}>
            {busy ? "正在导入..." : `确认${restore ? "恢复" : "导入"} ${ready} 条`} <Icon name="arrow-right" />
          </button>}
        </footer>
      </section>
    </div>
  );
}
