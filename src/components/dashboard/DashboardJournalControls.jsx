import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../Icon.jsx";

const SHARE_STATES = {
  private: { label: "分享个人手账", icon: "share", className: "is-teal" },
  pending: { label: "审核中", icon: "hourglass", className: "is-yellow" },
  approved: { label: "取消分享", icon: "eye-slash", className: "is-coral" },
};

function useModalLock(open, busy, onClose, initialFocusRef) {
  useEffect(() => {
    if (!open) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusFrame = window.requestAnimationFrame(() => initialFocusRef.current?.focus({ preventScroll: true }));
    const handleKeyDown = (event) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [busy, initialFocusRef, onClose, open]);
}

export function DashboardShareControl({ publicStatus = "private", onChange }) {
  const [dialogMode, setDialogMode] = useState(null);
  const [busy, setBusy] = useState(false);
  const cancelRef = useRef(null);
  const state = SHARE_STATES[publicStatus] || SHARE_STATES.private;
  const dialogOpen = Boolean(dialogMode);
  const cancelMode = dialogMode === "cancel";

  useModalLock(dialogOpen, busy, () => setDialogMode(null), cancelRef);

  const openDialog = () => {
    if (busy || publicStatus === "pending") return;
    setDialogMode(publicStatus === "approved" ? "cancel" : "share");
  };

  const confirm = async () => {
    setBusy(true);
    try {
      await onChange?.(cancelMode ? "cancel" : "share");
      setDialogMode(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        className={`dashboard-arcade-button dashboard-share-button ${state.className}`}
        type="button"
        onClick={openDialog}
        disabled={busy || publicStatus === "pending"}
      >
        <Icon name={busy ? "spinner" : state.icon} className={busy ? "is-spinning" : undefined} />
        <span className="dashboard-arcade-button__label">{busy ? "处理中…" : state.label}</span>
      </button>

      {dialogOpen && createPortal(
        <div
          className="dashboard-journal-dialog-backdrop dashboard-share-dialog-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !busy) setDialogMode(null);
          }}
        >
          <section
            className="dashboard-share-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="dashboard-share-dialog-title"
          >
            <span className={`dashboard-share-dialog__signal ${cancelMode ? "is-cancel" : ""}`} aria-hidden="true">
              <Icon name={cancelMode ? "eye-slash" : "satellite-dish"} />
            </span>
            <span className={`dashboard-share-dialog__kicker ${cancelMode ? "is-cancel" : ""}`}>
              {cancelMode ? "PRIVACY SWITCH" : "PUBLIC JOURNAL PASS"}
            </span>
            <h2 id="dashboard-share-dialog-title">{cancelMode ? "确认取消分享？" : "申请分享个人手账？"}</h2>
            <p className={cancelMode ? "is-cancel" : ""}>
              {cancelMode
                ? "取消后，你的手账将立即从番剧共创宇宙中隐藏，其他访客无法继续进入查看。以后仍可重新提交分享申请。"
                : "审核通过后，其他访客可以查看你的番剧记录、评分和个人评价；账号邮箱、管理功能与私人操作不会公开。"}
            </p>
            <div className="dashboard-share-dialog__actions">
              <button ref={cancelRef} type="button" disabled={busy} onClick={() => setDialogMode(null)}>
                {cancelMode ? "继续公开" : "暂不分享"}
              </button>
              <button className={cancelMode ? "is-cancel" : "is-confirm"} type="button" disabled={busy} onClick={confirm}>
                {busy ? "正在处理…" : cancelMode ? "确认取消分享" : "确认申请"}
              </button>
            </div>
          </section>
        </div>,
        document.body,
      )}
    </>
  );
}

export function DashboardReturnHomeControl({ onConfirm }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const continueRef = useRef(null);

  useModalLock(open, busy, () => setOpen(false), continueRef);

  const confirm = async () => {
    setBusy(true);
    try {
      await onConfirm?.();
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button className="dashboard-arcade-button dashboard-return-home-cta" type="button" onClick={() => setOpen(true)}>
        <Icon name="arrow-left" />
        <span className="dashboard-arcade-button__label">返回主界面</span>
      </button>

      {open && createPortal(
        <div
          className="dashboard-journal-dialog-backdrop dashboard-return-dialog-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !busy) setOpen(false);
          }}
        >
          <section className="dashboard-return-dialog" role="dialog" aria-modal="true" aria-labelledby="dashboard-return-dialog-title">
            <div className="dashboard-return-dialog__stripe" aria-hidden="true" />
            <span className="dashboard-return-dialog__dots" aria-hidden="true">
              {Array.from({ length: 9 }, (_, index) => <i key={index} />)}
            </span>
            <div className="dashboard-return-dialog__content">
              <span className="dashboard-return-dialog__icon" aria-hidden="true"><Icon name="house" /></span>
              <span className="dashboard-return-dialog__kicker">Back To Showcase</span>
              <h2 id="dashboard-return-dialog-title">确定回到主界面吗？</h2>
              <p>返回展示主界面会登出当前账号，再次进入私人手账时需要重新登录。</p>
              <div className="dashboard-return-dialog__actions">
                <button ref={continueRef} type="button" disabled={busy} onClick={() => setOpen(false)}>继续整理</button>
                <button className="is-confirm" type="button" disabled={busy} onClick={confirm}>{busy ? "正在返回…" : "确认返回"}</button>
              </div>
            </div>
          </section>
        </div>,
        document.body,
      )}
    </>
  );
}

export function DashboardPreviewActions({ publicStatus = "private", onShareChange, onEdit, onReturnHome }) {
  return (
    <div className="dashboard-preview-actions">
      <div className="dashboard-preview-actions__top">
        <DashboardReturnHomeControl onConfirm={onReturnHome} />
        <button className="dashboard-arcade-button dashboard-edit-mode-cta is-yellow" type="button" onClick={onEdit}>
          <Icon name="edit" />
          <span className="dashboard-arcade-button__label">编辑模式</span>
        </button>
      </div>
      <div className="dashboard-preview-actions__share">
        <DashboardShareControl publicStatus={publicStatus} onChange={onShareChange} />
      </div>
    </div>
  );
}
