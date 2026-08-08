import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { gsap } from "gsap";

import { api } from "../../lib/api.js";
import { Icon } from "../Icon.jsx";
import {
  auditActionInfo,
  auditFieldLabels,
  auditTargetLabel,
  auditTargetLabels,
  auditValueLabel,
  dateTimeLabel,
} from "./adminControlUtils.js";


function useAdminDialogMotion(dialogRef, onClose) {
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    const focusableSelector = "button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [href], [tabindex]:not([tabindex='-1'])";
    const focusDialog = () => {
      const target = dialog.querySelector("[autofocus]") || dialog.querySelector(focusableSelector) || dialog;
      target.focus({ preventScroll: true });
    };
    const focusFrame = window.requestAnimationFrame(focusDialog);
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...dialog.querySelectorAll(focusableSelector)].filter((element) => element.getClientRects().length > 0);
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus({ preventScroll: true });
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus({ preventScroll: true });
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus({ preventScroll: true });
      }
    };

    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);

    let context;
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const backdrop = dialog.parentElement;
      context = gsap.context(() => {
        gsap.fromTo(backdrop, { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.18, ease: "power1.out" });
        gsap.fromTo(dialog, { autoAlpha: 0, y: 14, scale: 0.985 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.28, ease: "power2.out", clearProps: "transform" });
      }, backdrop);
    }

    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      context?.revert();
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus({ preventScroll: true });
    };
  }, [dialogRef]);
}

export function AuditLogList({ entries, onOpen }) {
  return <div className="admin-audit-table">
    <div className="admin-audit-table__head" aria-hidden="true"><span>管理员</span><span>操作</span><span>操作对象</span><span>时间与来源</span><span>详情</span></div>
    <div className="admin-audit-table__body">
      {entries.map((item) => {
        const action = auditActionInfo(item.action);
        return <article className="admin-audit-row" key={`audit:${item.id}`}>
          <div className="admin-audit-row__actor"><i><Icon name="shield" /></i><span><strong>{item.actor || "system"}</strong><small>{item.actor === "system" ? "系统任务" : "管理员账号"}</small></span></div>
          <div className="admin-audit-row__action"><span className={`is-${action.tone}`}>{action.label}</span><code>{item.action}</code></div>
          <div className="admin-audit-row__target"><strong>{auditTargetLabel(item)}</strong><small>{auditTargetLabels[item.target_type] || item.target_type || "未知类型"}{item.target_id ? ` · ID ${item.target_id}` : ""}</small></div>
          <div className="admin-audit-row__request"><strong>{dateTimeLabel(item.created_at)}</strong><small>{item.ip_address || "未记录 IP"}</small></div>
          <button type="button" onClick={() => onOpen(item)}><Icon name="eye" /> 查看差异</button>
        </article>;
      })}
    </div>
  </div>;
}

export function AdminDetailDialog({ kind, detail, loading, viewer, onClose, onAsk, onRun }) {
  const dialogRef = useRef(null);
  useAdminDialogMotion(dialogRef, onClose);
  const auditAction = auditActionInfo(detail.action);
  const dialogTitle = kind === "audit" ? `${auditAction.label} · ${auditTargetLabel(detail)}` : detail.title || detail.nickname || detail.target_label || detail.username;
  const changeUser = (payload, label) => onAsk({
    title: label,
    message: `即将修改 ${detail.nickname || detail.username} 的账号权限。`,
    confirmLabel: "确认修改",
    requiresReauth: detail.is_staff || detail.is_superuser || Object.hasOwn(payload, "is_staff") || Object.hasOwn(payload, "is_superuser"),
    onConfirm: (_reason, reauth) => onRun(() => api.patch(`staff/users/${detail.id}/permissions/`, { ...payload, ...reauth }), "账号权限已更新"),
  });
  const forceLogout = () => onAsk({
    title: "强制退出全部设备",
    message: "该用户现有的访问令牌、刷新令牌与后台会话都会失效。",
    confirmLabel: "强制退出",
    requiresReauth: detail.is_staff || detail.is_superuser,
    onConfirm: (_reason, reauth) => onRun(() => api.post(`staff/users/${detail.id}/force-logout/`, reauth), "用户会话已失效"),
  });
  return createPortal(<div className="admin-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="admin-detail-dialog" ref={dialogRef} tabIndex="-1" role="dialog" aria-modal="true" aria-label="后台详情">
      <header><div><span>{kind === "audit" ? "AUDIT DETAIL" : "ADMIN DETAIL"}</span><h3>{dialogTitle}</h3></div><button type="button" onClick={onClose} aria-label="关闭"><Icon name="close" /></button></header>
      {loading ? <div className="admin-dashboard-loading"><span /><span /><span /> 正在读取详情</div> : <div className="admin-detail-dialog__body">
        {kind === "audit" && <>
          <div className="admin-audit-detail-summary">
            <span><b>执行管理员</b>{detail.actor || "system"}</span>
            <span><b>操作类型</b>{auditAction.label}<small>{detail.action}</small></span>
            <span><b>操作对象</b>{auditTargetLabel(detail)}<small>{auditTargetLabels[detail.target_type] || detail.target_type || "未知类型"}</small></span>
            <span><b>操作时间</b>{dateTimeLabel(detail.created_at)}<small>{detail.ip_address || "未记录 IP"}</small></span>
          </div>
          <AuditDiff before={detail.before} after={detail.after} />
          <DetailBlock title="附加信息" value={detail.metadata} emptyLabel="本次操作没有附加信息" />
          <DetailBlock title="浏览器与设备" value={detail.user_agent || "未记录浏览器信息"} />
        </>}
        {kind === "columns" && <><p className="admin-detail-lead">{detail.summary || "未填写专栏摘要"}</p><DetailBlock title="专栏正文" value={detail.body || "未填写正文"} /><EntryPreview entries={detail.entries} /></>}
        {kind === "journals" && <><p className="admin-detail-lead">{detail.showcase_subtitle}</p>{detail.review_reason && <div className="admin-detail-reason">审核反馈：{detail.review_reason}</div>}<EntryPreview entries={detail.entries} /></>}
        {kind === "entries" && <><div className="admin-detail-anime">{detail.poster && <img src={detail.poster} alt="" />}<div><b>{detail.japanese_title}</b><p>{detail.description || "未填写简介"}</p><small>{detail.airing_period} · {detail.studio} · {detail.episodes}</small></div></div><DetailBlock title="个人评价" value={detail.review || "未填写评价"} /></>}
        {kind === "users" && <>
          <div className="admin-user-detail-grid"><span><b>邮箱验证</b>{detail.email_verified ? "已验证" : "未验证"}</span><span><b>最后登录</b>{dateTimeLabel(detail.last_login)}</span><span><b>两步验证</b>{detail.two_factor_enabled ? "已启用" : "未启用"}</span><span><b>内容数量</b>{detail.entry_count} 条记录 / {detail.column_count} 个专栏</span></div>
          <div className="admin-detail-actions">
            <button type="button" onClick={forceLogout}><Icon name="logout" /> 强制退出</button>
            {!detail.email_verified && <button type="button" onClick={() => onRun(() => api.post(`staff/users/${detail.id}/resend-activation/`), "激活邮件已发送")}><Icon name="envelope" /> 重发激活邮件</button>}
            {detail.can_manage !== false && !detail.is_superuser && <button type="button" className={detail.is_active ? "is-danger" : "is-safe"} onClick={() => changeUser({ is_active: !detail.is_active }, detail.is_active ? "停用账号" : "启用账号")}><Icon name={detail.is_active ? "user-slash" : "check"} /> {detail.is_active ? "停用账号" : "启用账号"}</button>}
          </div>
          {detail.can_manage === false && <p className="admin-detail-permission-note">该账号由更高权限的管理员管理。</p>}
          {viewer.is_superuser && detail.can_manage !== false && !detail.is_superuser && <label className="admin-role-editor"><span>后台角色</span><select value={detail.staff_role === "user" || !detail.staff_role ? "unassigned" : detail.staff_role} onChange={(event) => onAsk({ title: "调整后台角色", message: "保存后权限立即生效，需要重新验证当前密码和两步验证码。", confirmLabel: "保存角色", requiresReauth: true, onConfirm: (_reason, reauth) => onRun(() => api.post(`staff/users/${detail.id}/role/`, { role: event.target.value, ...reauth }), "后台角色已更新") })}><option value="unassigned">未分配</option><option value="reviewer">内容审核员</option><option value="user_manager">用户管理员</option><option value="operator">系统运维员</option><option value="administrator">后台管理员</option></select></label>}
          <section className="admin-login-history"><h4>最近登录与安全事件</h4>{detail.login_events?.length ? detail.login_events.map((event) => <div key={event.id}><span className={event.success ? "is-success" : "is-failure"}>{event.event_display}</span><b>{event.ip_address || "未知 IP"}</b><small>{dateTimeLabel(event.created_at)}</small></div>) : <p>暂无安全事件</p>}</section>
          <EntryPreview entries={detail.entries} title="最近番剧记录" />
        </>}
      </div>}
    </section>
  </div>, document.body);
}

function AuditDiff({ before, after }) {
  const previous = before && typeof before === "object" ? before : {};
  const current = after && typeof after === "object" ? after : {};
  const keys = [...new Set([...Object.keys(previous), ...Object.keys(current)])]
    .filter((key) => JSON.stringify(previous[key]) !== JSON.stringify(current[key]));
  return <section className="admin-audit-diff">
    <header><div><span>CHANGESET</span><h4>字段修改差异</h4></div><b>{keys.length} 项</b></header>
    {keys.length === 0 ? <p className="admin-audit-diff__empty">该操作没有字段级修改记录。</p> : <div className="admin-audit-diff__list">{keys.map((key) => {
      const beforeValue = auditValueLabel(previous[key]);
      const afterValue = auditValueLabel(current[key]);
      return <article className="is-changed" key={key}>
        <div><strong>{auditFieldLabels[key] || key}</strong><code>{key}</code></div>
        <pre>{beforeValue}</pre>
        <i><Icon name="arrow-right" /></i>
        <pre>{afterValue}</pre>
      </article>;
    })}</div>}
  </section>;
}

function DetailBlock({ title, value, emptyLabel = "暂无内容" }) {
  const isEmptyObject = value && typeof value === "object" && Object.keys(value).length === 0;
  return <section className="admin-detail-block"><h4>{title}</h4>{isEmptyObject ? <p className="admin-detail-block__empty">{emptyLabel}</p> : typeof value === "object" ? <pre>{JSON.stringify(value, null, 2)}</pre> : <p>{value || emptyLabel}</p>}</section>;
}

function EntryPreview({ entries = [], title = "关联番剧" }) {
  return <section className="admin-entry-preview"><h4>{title} · {entries.length}</h4>{entries.length ? entries.map((entry) => <article key={entry.id}>{entry.poster && <img src={entry.poster} alt="" />}<div><strong>{entry.title}</strong><small>{entry.japanese_title || entry.status}</small></div><b>{entry.score ?? "—"}</b></article>) : <p>暂无记录</p>}</section>;
}

export function AdminConfirmDialog({ value, onChange, onClose }) {
  const [busy, setBusy] = useState(false);
  const [reauth, setReauth] = useState({ current_password: "", otp: "", recovery_code: "" });
  const dialogRef = useRef(null);
  useAdminDialogMotion(dialogRef, onClose);
  const confirm = async () => {
    if (value.reasonRequired && !value.reason.trim()) return;
    setBusy(true);
    try {
      await value.onConfirm(value.reason.trim(), value.requiresReauth ? reauth : {});
      onClose();
    } finally {
      setBusy(false);
    }
  };
  return createPortal(<div className="admin-modal-backdrop admin-confirm-backdrop" role="presentation">
    <section className="admin-confirm-dialog" ref={dialogRef} tabIndex="-1" role="alertdialog" aria-modal="true" aria-label={value.title}>
      <span><Icon name="warning" /> CONFIRM ACTION</span><h3>{value.title}</h3><p>{value.message}</p>
      {value.reasonRequired && <label><span>操作原因</span><textarea autoFocus value={value.reason} onChange={(event) => onChange({ ...value, reason: event.target.value })} placeholder="填写具体、可反馈的原因" rows="4" /></label>}
      {value.requiresReauth && <div className="admin-confirm-reauth"><strong>高风险操作需要重新验证</strong><label><span>当前密码</span><input type="password" autoComplete="current-password" value={reauth.current_password} onChange={(event) => setReauth((current) => ({ ...current, current_password: event.target.value }))} /></label><label><span>两步验证码或恢复码</span><input inputMode="numeric" autoComplete="one-time-code" value={reauth.otp || reauth.recovery_code} onChange={(event) => { const value = event.target.value; setReauth((current) => ({ ...current, otp: /^\d{0,6}$/.test(value) ? value : "", recovery_code: /^\d/.test(value) ? "" : value.toUpperCase() })); }} /></label></div>}
      <div><button type="button" onClick={onClose} disabled={busy}>取消</button><button type="button" className="is-confirm" onClick={confirm} disabled={busy || (value.reasonRequired && !value.reason.trim()) || (value.requiresReauth && (!reauth.current_password || (!reauth.otp && !reauth.recovery_code)))}>{busy ? "正在执行..." : value.confirmLabel}</button></div>
    </section>
  </div>, document.body);
}

